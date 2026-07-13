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
    DOMAIN,
    LOGGER,
    PORT_REST,
    PORT_WS,
)
from .art_session import (
    ArtSession,
    ArtSessionState,
    ArtSessionTrigger,
    StateCallback,
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
        self._task_factory = task_factory
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
        self._art_session = ArtSession(
            self._art,
            task_factory=task_factory,
        )
        # Once stopped (entry unload), no listener may be (re)started — an
        # in-flight restart task finishing after unload would otherwise
        # resurrect a connection nothing will ever close.
        self._stopped = False
        self._stop_task: asyncio.Task[None] | None = None
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
    def art_ready(self) -> bool:
        """Return whether the supervised Art receiver is ready."""
        return self._art_session.ready

    @property
    def art_generation(self) -> int:
        """Return the current successful Art transport generation."""
        return self._art_session.generation

    @property
    def art_session_state(self) -> ArtSessionState:
        """Return the supervised Art lifecycle state."""
        return self._art_session.state

    def observe_art_power(
        self,
        reachable: bool,
        power_state: str | None,
        reachable_edge: bool,
    ) -> None:
        """Synchronously pass a power observation to the Art session."""
        self._art_session.observe_power(
            reachable, power_state, reachable_edge
        )

    def set_art_session_state_callback(
        self, callback: StateCallback | None
    ) -> None:
        """Replace the Art session state callback."""
        self._art_session.set_state_callback(callback)

    async def async_start_art_session(self) -> None:
        """Arm supervised Art recovery without opening the transport."""
        await self._art_session.async_start()

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

    async def _async_art_read(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run one background read only on an already-ready session."""
        if self._stopped or not self._art_session.ready:
            return None
        try:
            return await operation()
        except Exception as err:  # noqa: BLE001
            await self._art_session.async_connection_failed(err)
            return None

    async def _async_art_mutation(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run one user mutation after one session-owned readiness probe."""
        if self._stopped:
            raise ConnectionFailure("Art device is stopped")
        if not await self._art_session.async_ensure_ready(
            ArtSessionTrigger.USER
        ):
            raise ConnectionFailure("Art session is unavailable")
        try:
            return await operation()
        except Exception as err:  # noqa: BLE001
            await self._art_session.async_connection_failed(err)
            raise

    async def async_get_artmode(self) -> bool | None:
        """Return Art Mode state without opening or retrying the session."""
        value = await self._async_art_read(self._art.get_artmode)
        if value is None:
            return None
        return value == "on"

    async def async_set_artmode(self, on: bool) -> None:
        await self._async_art_mutation(lambda: self._art.set_artmode(on))

    async def async_get_current_art(self) -> str | None:
        """Content id of the artwork currently selected, or None."""
        current = await self._async_art_read(self._art.get_current)
        if isinstance(current, dict):
            return current.get("content_id")
        return None

    async def async_get_art_brightness(self) -> int | None:
        value = await self._async_art_read(self._art.get_brightness)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def async_set_art_brightness(self, value: int) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_brightness(value)
        )

    async def async_select_art(self, content_id: str, show: bool) -> None:
        await self._async_art_mutation(
            lambda: self._art.select_image(content_id, None, show)
        )

    async def async_upload_art(
        self, data: bytes, file_type: str, matte: str
    ) -> str:
        """Upload image bytes; returns the TV-assigned content id."""
        return await self._async_art_mutation(
            lambda: self._art.upload(data, file_type, matte)
        )

    async def async_delete_art(self, content_id: str) -> None:
        await self._async_art_mutation(
            lambda: self._art.delete(content_id)
        )

    async def async_get_art_thumbnail(self, content_id: str) -> bytes | None:
        """JPEG thumbnail bytes for an artwork, or None.

        Store artworks (SAM-*) are DRM-refused by the TV and yield None.
        """
        if self._stopped or not self._art_session.ready:
            return None
        try:
            return await self._art.get_thumbnail(content_id)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("thumbnail fetch failed for %s: %s", content_id, err)
            return None

    async def async_change_matte(self, content_id: str, matte_id: str) -> None:
        await self._async_art_mutation(
            lambda: self._art.change_matte(content_id, matte_id)
        )

    async def async_set_photo_filter(self, content_id: str, filter_id: str) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_photo_filter(content_id, filter_id)
        )

    async def async_set_favourite(self, content_id: str, favourite: bool) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_favourite(content_id, favourite)
        )

    async def async_get_color_temperature(self) -> int | None:
        value = await self._async_art_read(
            self._art.get_color_temperature
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def async_set_color_temperature(self, value: int) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_color_temperature(value)
        )

    async def async_set_slideshow(
        self, duration: int, shuffle: bool, category_id: str
    ) -> None:
        """Configure the art slideshow."""
        await self._async_art_mutation(
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

    async def _async_close_remote(self) -> None:
        """Close the remote without allowing shutdown to wedge indefinitely."""
        async with asyncio.timeout(ART_CLOSE_DEADLINE):
            await self._remote.close()

    async def async_stop(self) -> None:
        """Join the one task-factory-owned terminal shutdown."""
        while True:
            task = self._stop_task
            if task is not None and task.done() and (
                task.cancelled() or task.exception() is not None
            ):
                self._stop_task = None
                task = None
            if task is None:
                coroutine = self._async_stop_once()
                try:
                    task = self._task_factory(
                        coroutine, f"{DOMAIN}-device-stop"
                    )
                except BaseException:
                    coroutine.close()
                    raise
                self._stop_task = task
                self._stopped = True
            try:
                await asyncio.shield(task)
                return
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if task.cancelled():
                    if self._stop_task is task:
                        self._stop_task = None
                    continue
                raise

    async def _async_stop_once(self) -> None:
        """Stop the Art session and remote exactly once."""
        self._stopped = True
        completion = asyncio.gather(
            self._art_session.async_stop(),
            self._async_close_remote(),
            return_exceptions=True,
        )
        while True:
            try:
                await asyncio.shield(completion)
                return
            except asyncio.CancelledError:
                if completion.cancelled():
                    raise
