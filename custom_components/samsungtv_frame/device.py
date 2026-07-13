"""Async facade over the samsungtvws library for a Frame TV."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client import UpnpDevice, UpnpService
from async_upnp_client.client_factory import UpnpFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.command import SamsungTVCommand
from samsungtvws.exceptions import ConnectionFailure
from samsungtvws.remote import ChannelEmitCommand, SendRemoteKey
from wakeonlan import send_magic_packet

from .const import (
    ART_CLOSE_DEADLINE,
    CLIENT_NAME,
    LOGGER,
    PORT_REST,
    PORT_WS,
)
from .frame_art import ArtEventCallback, FrameArt, TaskFactory

_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
_DMR_URL = "http://{host}:9197/dmr"


class FrameDevice:
    """Clean async surface the coordinator talks to."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        mac: str,
        token: str | None,
        *,
        ssl_context: Any,
        task_factory: TaskFactory,
    ) -> None:
        self._hass = hass
        self._host = host
        self._mac = mac
        self._token = token
        # _rest is created lazily on first async_device_info call: aiohttp's connector
        # requires a running event loop, so we cannot create it here in __init__ (which
        # may be called from a sync context, e.g. in tests).
        self._rest: SamsungTVAsyncRest | None = None
        self._remote = SamsungTVWSAsyncRemote(
            host, token=token, port=PORT_WS, name=CLIENT_NAME, timeout=8
        )
        self._art = FrameArt(
            host,
            token=token,
            ssl_context=ssl_context,
            task_factory=task_factory,
            event_callback=None,
        )
        # Once stopped (entry unload), no listener may be (re)started — an
        # in-flight restart task finishing after unload would otherwise
        # resurrect a connection nothing will ever close.
        self._stopped = False
        # UPnP DMR device (RenderingControl) — created lazily, dropped on
        # failure so a TV power cycle just triggers a fresh description fetch.
        self._upnp_device: UpnpDevice | None = None
        # Set once the remote channel rejects the stored token (see
        # _maybe_drop_rejected_remote_token); stays tokenless until a real
        # remote token is granted and captured.
        self._remote_tokenless = False
        # True after any successful remote-channel operation this run. The
        # background app-list fetch is gated on it: a remote connect can make
        # the TV pop an authorization prompt (e.g. after a power cycle wiped
        # the grant), and only user-initiated actions should ever do that.
        self.remote_confirmed = False

    @property
    def host(self) -> str:
        return self._host

    @property
    def newest_token(self) -> str | None:
        """A token the TV issued that differs from the one we hold, if any.

        Pairing on this TV granted access by client name without issuing a
        token, but a token can still appear later on either persistent
        connection; surface it so the coordinator can persist it.
        """
        for client in (self._art, self._remote):
            token = getattr(client, "token", None)
            if token and token != self._token:
                return token
        return None

    def update_token(self, token: str) -> None:
        """Adopt a newly issued token for all future (re)connections."""
        self._token = token
        self._art.token = token
        self._remote.token = token

    def _ensure_rest(self) -> SamsungTVAsyncRest:
        if self._rest is None:
            session = async_get_clientsession(self._hass)
            self._rest = SamsungTVAsyncRest(
                self._host, session=session, port=PORT_REST, timeout=8
            )
        return self._rest

    async def async_device_info(self) -> dict[str, Any] | None:
        try:
            info = await self._ensure_rest().rest_device_info()
        except Exception as err:  # noqa: BLE001 - library raises broad connection types
            LOGGER.debug("REST device info failed for %s: %s", self._host, err)
            return None
        return info.get("device") if info else None

    async def _async_rendering_control(self) -> UpnpService:
        if self._upnp_device is None:
            session = async_get_clientsession(self._hass)
            factory = UpnpFactory(AiohttpSessionRequester(session), non_strict=True)
            self._upnp_device = await factory.async_create_device(
                _DMR_URL.format(host=self._host)
            )
        return self._upnp_device.service(_RENDERING_CONTROL)

    async def async_get_volume(self) -> tuple[float | None, bool | None]:
        """(volume_level 0-1, muted) via UPnP, or (None, None)."""
        try:
            rc = await self._async_rendering_control()
            vol = await rc.action("GetVolume").async_call(
                InstanceID=0, Channel="Master"
            )
            mute = await rc.action("GetMute").async_call(
                InstanceID=0, Channel="Master"
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("UPnP volume query failed: %s", err)
            self._upnp_device = None
            return None, None
        return vol["CurrentVolume"] / 100, bool(mute["CurrentMute"])

    async def async_set_volume(self, level: float) -> None:
        try:
            rc = await self._async_rendering_control()
            await rc.action("SetVolume").async_call(
                InstanceID=0, Channel="Master", DesiredVolume=round(level * 100)
            )
        except Exception:
            self._upnp_device = None
            raise

    async def async_set_mute(self, mute: bool) -> None:
        try:
            rc = await self._async_rendering_control()
            await rc.action("SetMute").async_call(
                InstanceID=0, Channel="Master", DesiredMute=mute
            )
        except Exception:
            self._upnp_device = None
            raise

    async def async_app_status(self, app_id: str) -> dict[str, Any] | None:
        """Status payload for one app ({visible, running, ...}), or None."""
        try:
            return await self._ensure_rest().rest_app_status(app_id)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("REST app status failed for %s: %s", app_id, err)
            return None

    async def _async_art_command(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        retry: bool = True,
    ) -> Any:
        """Run an Art operation, retrying one ordinary stale failure."""
        if self._stopped:
            raise ConnectionFailure("Art device is stopped")
        try:
            return await operation()
        except TimeoutError:
            await self._art.close()
            raise
        except Exception:
            await self._art.close()
            if not retry:
                raise
            if self._stopped:
                raise ConnectionFailure("Art device is stopped")
            try:
                return await operation()
            except Exception:
                await self._art.close()
                raise

    async def async_get_artmode(self, attempts: int = 2) -> bool | None:
        # Two attempts by default: the first call after a TV power cycle hits
        # the stale cached connection and fails; the reset makes the retry
        # reconnect, so the poll still resolves art mode in the same cycle.
        # Callers pass attempts=1 when the TV is shutting down (its art socket
        # hangs until timeout, so a retry only doubles the poll latency).
        for attempt in range(1, attempts + 1):
            if self._stopped:
                return None
            try:
                value = await self._art.get_artmode()
            except TimeoutError:
                LOGGER.debug("get_artmode hit the deadline (attempt %s)", attempt)
                await self._art.close()
                return None
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("get_artmode failed (attempt %s): %s", attempt, err)
                await self._art.close()
                continue
            return value == "on"
        return None

    async def async_set_artmode(self, on: bool) -> None:
        await self._async_art_command(lambda: self._art.set_artmode(on))

    async def async_get_current_art(self) -> str | None:
        """Content id of the artwork currently selected, or None."""
        try:
            current = await self._async_art_command(self._art.get_current)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_current failed: %s", err)
            return None
        if isinstance(current, dict):
            return current.get("content_id")
        return None

    async def async_get_art_brightness(self) -> int | None:
        try:
            value = await self._async_art_command(self._art.get_brightness)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_brightness failed: %s", err)
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def async_set_art_brightness(self, value: int) -> None:
        await self._async_art_command(lambda: self._art.set_brightness(value))

    async def async_select_art(self, content_id: str, show: bool) -> None:
        await self._async_art_command(
            lambda: self._art.select_image(content_id, None, show)
        )

    async def async_upload_art(
        self, data: bytes, file_type: str, matte: str
    ) -> str:
        """Upload image bytes; returns the TV-assigned content id."""

        # No auto-retry: a retry after a partially-completed upload could
        # duplicate the artwork. Reset the connection so the next call heals.
        return await self._async_art_command(
            lambda: self._art.upload(data, file_type, matte), retry=False
        )

    async def async_delete_art(self, content_id: str) -> None:
        await self._async_art_command(lambda: self._art.delete(content_id))

    async def async_get_art_thumbnail(self, content_id: str) -> bytes | None:
        """JPEG thumbnail bytes for an artwork, or None.

        Store artworks (SAM-*) are DRM-refused by the TV and yield None.
        """
        if self._stopped:
            return None
        try:
            return await self._art.get_thumbnail(content_id)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("thumbnail fetch failed for %s: %s", content_id, err)
            return None

    async def async_change_matte(self, content_id: str, matte_id: str) -> None:
        await self._async_art_command(
            lambda: self._art.change_matte(content_id, matte_id)
        )

    async def async_set_photo_filter(self, content_id: str, filter_id: str) -> None:
        await self._async_art_command(
            lambda: self._art.set_photo_filter(content_id, filter_id)
        )

    async def async_set_favourite(self, content_id: str, favourite: bool) -> None:
        await self._async_art_command(
            lambda: self._art.set_favourite(content_id, favourite)
        )

    async def async_get_color_temperature(self) -> int | None:
        try:
            value = await self._async_art_command(
                self._art.get_color_temperature
            )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_color_temperature failed: %s", err)
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def async_set_color_temperature(self, value: int) -> None:
        await self._async_art_command(
            lambda: self._art.set_color_temperature(value)
        )

    async def async_set_slideshow(
        self, duration: int, shuffle: bool, category_id: str
    ) -> None:
        """Configure the art slideshow."""
        await self._async_art_command(
            lambda: self._art.set_slideshow(duration, shuffle, category_id)
        )

    async def async_turn_on(self) -> None:
        await self._hass.async_add_executor_job(
            lambda: send_magic_packet(self._mac, ip_address="255.255.255.255")
        )

    async def async_turn_off(self) -> None:
        # Single press only toggles art mode; a 3 s hold truly powers a Frame off.
        await self._async_remote_commands(SendRemoteKey.hold("KEY_POWER", 3))

    async def async_send_key(self, key: str) -> None:
        await self._async_remote_commands([SendRemoteKey.click(key)])

    async def async_hold_key(self, key: str, seconds: float) -> None:
        await self._async_remote_commands(SendRemoteKey.hold(key, seconds))

    async def async_launch_app(
        self, app_id: str, app_type: str, meta_tag: str = ""
    ) -> None:
        await self._async_remote_commands(
            [ChannelEmitCommand.launch_app(app_id, app_type, meta_tag)]
        )

    async def async_app_list(self) -> list[dict[str, Any]] | None:
        """Installed apps, or None (not supported on all TVs / TV not ready)."""
        try:
            apps = await self._remote.app_list()
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("app_list failed: %s", err)
            self._maybe_drop_rejected_remote_token(err)
            return None
        self.remote_confirmed = True
        return apps

    def _maybe_drop_rejected_remote_token(self, err: Exception) -> None:
        """Fall back to a tokenless remote client when the token is rejected.

        The remote channel answers a connect carrying a token it does not
        recognize (e.g. one issued on the art channel) with an instant
        ``ms.channel.timeOut`` and never shows the on-TV Allow prompt.
        Reconnecting without a token makes the prompt render (while the TV
        shows normal content); once granted, the TV issues a proper remote
        token which the coordinator's capture persists into the entry.
        """
        if self._remote_tokenless or "ms.channel.timeOut" not in str(err):
            return
        self._remote_tokenless = True
        LOGGER.warning(
            "The TV rejected the stored token on the remote-control channel; "
            "retrying without a token — accept the Allow prompt on the TV "
            "(it only renders while the TV is showing normal content)"
        )
        # Generous timeout: each tokenless connect holds the on-TV Allow
        # prompt open for this long, giving the user a real chance to react.
        self._remote = SamsungTVWSAsyncRemote(
            self._host, token=None, port=PORT_WS, name=CLIENT_NAME, timeout=30
        )

    async def _async_remote_commands(self, commands: list[SamsungTVCommand]) -> None:
        """Send on the persistent remote; reset once if the connection is stale.

        Like the art client, the remote keeps a cached connection that is never
        invalidated when the TV power-cycles; the first send after a cycle
        fails, so close and retry once before giving up.
        """
        try:
            await self._remote.send_commands(commands)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("remote send failed, retrying on a fresh connection: %s", err)
            self._maybe_drop_rejected_remote_token(err)
            try:
                await self._remote.close()
            except Exception:  # noqa: BLE001
                pass
            await self._remote.send_commands(commands)
        self.remote_confirmed = True

    def set_art_event_callback(self, callback: ArtEventCallback) -> None:
        """Set the loop-native callback receiving unsolicited Art events."""
        self._art.set_event_callback(callback)

    @property
    def listener_alive(self) -> bool:
        """Whether the native Art receiver is currently running."""
        return self._art.is_alive()

    async def async_start_art_listener(self) -> None:
        if self._stopped:
            return
        await self._art.start_listening()

    async def async_restart_art_listener(self) -> None:
        """Close and restart the same native Art adapter."""
        if self._stopped:
            return
        await self._art.close()
        if self._stopped:
            return
        await self._art.start_listening()

    async def _async_close_remote(self) -> None:
        """Close the remote without allowing shutdown to wedge indefinitely."""
        async with asyncio.timeout(ART_CLOSE_DEADLINE):
            await self._remote.close()

    async def async_stop(self) -> None:
        self._stopped = True
        self._art.stop()
        await asyncio.gather(
            self._async_close_remote(),
            self._art.close(),
            return_exceptions=True,
        )
