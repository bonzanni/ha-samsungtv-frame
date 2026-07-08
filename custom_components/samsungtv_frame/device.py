"""Async facade over the samsungtvws library for a Frame TV."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client import UpnpDevice, UpnpService
from async_upnp_client.client_factory import UpnpFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from samsungtvws.art import SamsungTVArt
from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.command import SamsungTVCommand
from samsungtvws.exceptions import ResponseError
from samsungtvws.remote import ChannelEmitCommand, SendRemoteKey
from wakeonlan import send_magic_packet

from .const import CLIENT_NAME, LOGGER, PORT_REST, PORT_WS

_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
_DMR_URL = "http://{host}:9197/dmr"


class FrameDevice:
    """Clean async surface the coordinator talks to."""

    def __init__(
        self, hass: HomeAssistant, host: str, mac: str, token: str | None
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
        # Sync art client — its calls are executor-wrapped; its listener runs its own thread.
        self._art = SamsungTVArt(
            host, token=token, port=PORT_WS, name=CLIENT_NAME, timeout=8
        )
        # The sync client is not thread-safe: serialize all executor calls on
        # it (heartbeat poll vs entity services would otherwise interleave
        # frames on one websocket).
        self._art_lock = asyncio.Lock()
        # Serializes create/start/close of the listener instance: concurrent
        # restarts (reachable-edge storms on a flapping TV) would otherwise
        # race on self._art_listener and leak orphaned sockets + recv threads.
        self._listener_lock = asyncio.Lock()
        # Once stopped (entry unload), no listener may be (re)started — an
        # in-flight restart task finishing after unload would otherwise
        # resurrect a connection nothing will ever close.
        self._stopped = False
        # UPnP DMR device (RenderingControl) — created lazily, dropped on
        # failure so a TV power cycle just triggers a fresh description fetch.
        self._upnp_device: UpnpDevice | None = None
        # Guards against stacking thumbnail fetches if one wedges.
        self._thumb_busy = False
        # Set once the remote channel rejects the stored token (see
        # _maybe_drop_rejected_remote_token); stays tokenless until a real
        # remote token is granted and captured.
        self._remote_tokenless = False
        # Dedicated second instance for start_listening — samsungtvws raises
        # ConnectionFailure if start_listening is called on a connection that
        # is already open (e.g. after get_artmode opened it during first refresh).
        # timeout=None => blocking recv(): the TV goes silent for long stretches
        # (e.g. idle art mode) and a finite timeout raises WebSocketTimeoutException
        # inside the library's listener thread, which is uncaught and kills the
        # thread permanently. A real socket error (e.g. TV power-cycle) still
        # ends the thread, which async_restart_art_listener recovers from.
        self._art_listener = SamsungTVArt(
            host, token=token, port=PORT_WS, name=CLIENT_NAME, timeout=None
        )

    @property
    def host(self) -> str:
        return self._host

    @property
    def newest_token(self) -> str | None:
        """A token the TV issued that differs from the one we hold, if any.

        Pairing on this TV granted access by client name without issuing a
        token, but a token can still appear later on any of the three
        connections; surface it so the coordinator can persist it.
        """
        for client in (self._art, self._art_listener, self._remote):
            token = getattr(client, "token", None)
            if token and token != self._token:
                return token
        return None

    def update_token(self, token: str) -> None:
        """Adopt a newly issued token for all future (re)connections."""
        self._token = token

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

    async def _async_art_call(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run a sync art-client call in the executor, serialized."""
        async with self._art_lock:
            return await self._hass.async_add_executor_job(func, *args)

    async def _async_art_command(self, func: Callable[..., Any], *args: Any) -> Any:
        """Art command with reset + one retry on a stale connection.

        The sync client caches its connection and never invalidates it on
        failure, so the first command after a TV power cycle fails spuriously;
        a retry on a fresh connection usually succeeds.
        """
        try:
            return await self._async_art_call(func, *args)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("art command failed, retrying on a fresh connection: %s", err)
            await self._async_reset_art_connection()
            try:
                return await self._async_art_call(func, *args)
            except Exception:
                await self._async_reset_art_connection()
                raise

    async def async_get_artmode(self, attempts: int = 2) -> bool | None:
        # Two attempts by default: the first call after a TV power cycle hits
        # the stale cached connection and fails; the reset makes the retry
        # reconnect, so the poll still resolves art mode in the same cycle.
        # Callers pass attempts=1 when the TV is shutting down (its art socket
        # hangs until timeout, so a retry only doubles the poll latency).
        for attempt in range(1, attempts + 1):
            try:
                value = await self._async_art_call(self._art.get_artmode)
            except Exception as err:  # noqa: BLE001
                LOGGER.debug("get_artmode failed (attempt %s): %s", attempt, err)
                await self._async_reset_art_connection()
                continue
            return value == "on"
        return None

    async def async_set_artmode(self, on: bool) -> None:
        await self._async_art_command(self._art.set_artmode, on)

    async def async_get_current_art(self) -> str | None:
        """Content id of the artwork currently selected, or None."""
        try:
            current = await self._async_art_call(self._art.get_current)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_current failed: %s", err)
            return None
        if isinstance(current, dict):
            return current.get("content_id")
        return None

    async def async_get_art_brightness(self) -> int | None:
        try:
            value = await self._async_art_call(self._art.get_brightness)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_brightness failed: %s", err)
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def async_set_art_brightness(self, value: int) -> None:
        await self._async_art_command(self._art.set_brightness, value)

    async def async_select_art(self, content_id: str, show: bool) -> None:
        await self._async_art_command(self._art.select_image, content_id, None, show)

    async def async_upload_art(
        self, data: bytes, file_type: str, matte: str
    ) -> str:
        """Upload image bytes; returns the TV-assigned content id."""

        def _upload() -> str:
            return self._art.upload(
                data, matte=matte, portrait_matte=matte, file_type=file_type
            )

        # No auto-retry: a retry after a partially-completed upload could
        # duplicate the artwork. Reset the connection so the next call heals.
        try:
            return await self._async_art_call(_upload)
        except Exception:
            await self._async_reset_art_connection()
            raise

    async def async_delete_art(self, content_id: str) -> None:
        await self._async_art_command(self._art.delete, content_id)

    async def async_get_art_thumbnail(self, content_id: str) -> bytes | None:
        """JPEG thumbnail bytes for an artwork, or None.

        Uses get_thumbnail_list (the TV answers it; the singular
        get_thumbnail request is ignored by 2022+ firmware and would spin
        forever) on a DEDICATED short-lived connection, so a misbehaving
        fetch can never block the shared art client the coordinator polls
        on. Store artworks (SAM-*) are DRM-refused by the TV and yield None.
        """
        if self._thumb_busy:
            LOGGER.debug("thumbnail fetch already in flight; skipping")
            return None

        def _fetch() -> bytes | None:
            art = SamsungTVArt(
                self._host, token=self._token, port=PORT_WS,
                name=CLIENT_NAME, timeout=8,
            )
            try:
                thumbs = art.get_thumbnail_list(content_id)
                if not thumbs:
                    return None
                return bytes(next(iter(thumbs.values())))
            finally:
                try:
                    art.close()
                except Exception:  # noqa: BLE001
                    pass

        self._thumb_busy = True
        try:
            return await self._hass.async_add_executor_job(_fetch)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("thumbnail fetch failed for %s: %s", content_id, err)
            return None
        finally:
            self._thumb_busy = False

    async def async_change_matte(self, content_id: str, matte_id: str) -> None:
        await self._async_art_command(self._art.change_matte, content_id, matte_id)

    async def async_set_photo_filter(self, content_id: str, filter_id: str) -> None:
        await self._async_art_command(
            self._art.set_photo_filter, content_id, filter_id
        )

    async def async_set_favourite(self, content_id: str, favourite: bool) -> None:
        await self._async_art_command(
            self._art.set_favourite, content_id, "on" if favourite else "off"
        )

    async def async_get_color_temperature(self) -> int | None:
        try:
            value = await self._async_art_call(self._art.get_color_temperature)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_color_temperature failed: %s", err)
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def async_set_color_temperature(self, value: int) -> None:
        await self._async_art_command(self._art.set_color_temperature, value)

    async def async_set_slideshow(
        self, duration: int, shuffle: bool, category_id: str
    ) -> None:
        """Configure the art slideshow.

        2021+ firmwares use auto_rotation; older ones the slideshow request.
        Try the modern one first and fall back on a response error.
        """

        def _set() -> None:
            try:
                self._art.set_auto_rotation_status(
                    duration=duration, type=shuffle, category_id=category_id
                )
            except ResponseError:
                self._art.set_slideshow_status(
                    duration=duration, type=shuffle, category_id=category_id
                )

        await self._async_art_command(_set)

    async def _async_reset_art_connection(self) -> None:
        """Close the (likely dead) art websocket so the next call reopens fresh.

        The samsungtvws library reuses ``self.connection`` on the sync
        ``SamsungTVArt`` client and never invalidates it on failure, so a stale
        connection (e.g. after the TV power-cycles) would otherwise fail
        forever. Closing it here forces a fresh connection on the next call.
        Runs under the art lock — closing while another caller is mid-request
        on the same socket must not interleave.
        """
        try:
            async with self._art_lock:
                await self._hass.async_add_executor_job(self._art.close)
        except Exception:  # noqa: BLE001
            pass

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
            return await self._remote.app_list()
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("app_list failed: %s", err)
            self._maybe_drop_rejected_remote_token(err)
            return None

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

    @property
    def listener_alive(self) -> bool:
        """Whether the push listener's recv thread is currently running.

        Reaches into the library's ``_recv_loop`` thread: the thread dies on
        any uncaught socket error, and that is the only reliable liveness
        signal (the cached connection object can still claim to be open).
        """
        thread = getattr(self._art_listener, "_recv_loop", None)
        return thread is not None and thread.is_alive()

    async def async_start_art_listener(
        self, callback: Callable[[str, Any], None]
    ) -> None:
        async with self._listener_lock:
            if self._stopped:
                return
            await self._hass.async_add_executor_job(
                self._art_listener.start_listening, callback
            )

    async def async_restart_art_listener(
        self, callback: Callable[[str, Any], None]
    ) -> None:
        """Rebuild the art push listener from scratch and restart it.

        Called when the coordinator sees the TV go unreachable -> reachable
        again (e.g. after a power cycle) or finds the recv thread dead. A
        crashed instance holds stale connection state, so we replace it with
        a fresh ``SamsungTVArt`` before restarting. Serialized so overlapping
        restarts cannot orphan a started listener, and refused after stop so
        an in-flight restart cannot outlive the config entry.
        """

        def _restart() -> None:
            try:
                self._art_listener.close()
            except Exception:  # noqa: BLE001
                pass
            self._art_listener = SamsungTVArt(
                self._host, token=self._token, port=PORT_WS, name=CLIENT_NAME, timeout=None
            )
            self._art_listener.start_listening(callback)

        async with self._listener_lock:
            if self._stopped:
                return
            await self._hass.async_add_executor_job(_restart)

    async def async_stop(self) -> None:
        try:
            await self._remote.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._hass.async_add_executor_job(self._art.close)
        except Exception:  # noqa: BLE001
            pass
        # Waits for any in-flight listener (re)start, then closes the final
        # instance; the flag stops any later restart from resurrecting it.
        async with self._listener_lock:
            self._stopped = True
            try:
                await self._hass.async_add_executor_job(self._art_listener.close)
            except Exception:  # noqa: BLE001
                pass
