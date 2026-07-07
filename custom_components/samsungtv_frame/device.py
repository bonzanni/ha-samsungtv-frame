"""Async facade over the samsungtvws library for a Frame TV."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

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

    async def async_device_info(self) -> dict[str, Any] | None:
        if self._rest is None:
            session = async_get_clientsession(self._hass)
            self._rest = SamsungTVAsyncRest(
                self._host, session=session, port=PORT_REST, timeout=8
            )
        try:
            info = await self._rest.rest_device_info()
        except Exception as err:  # noqa: BLE001 - library raises broad connection types
            LOGGER.debug("REST device info failed for %s: %s", self._host, err)
            return None
        return info.get("device") if info else None

    async def _async_art_call(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run a sync art-client call in the executor, serialized."""
        async with self._art_lock:
            return await self._hass.async_add_executor_job(func, *args)

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
        try:
            await self._async_art_call(self._art.set_artmode, on)
        except Exception:
            await self._async_reset_art_connection()
            raise

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
        await self._async_art_call(self._art.set_brightness, value)

    async def async_select_art(self, content_id: str, show: bool) -> None:
        await self._async_art_call(self._art.select_image, content_id, None, show)

    async def async_upload_art(
        self, data: bytes, file_type: str, matte: str
    ) -> str:
        """Upload image bytes; returns the TV-assigned content id."""

        def _upload() -> str:
            return self._art.upload(
                data, matte=matte, portrait_matte=matte, file_type=file_type
            )

        async with self._art_lock:
            return await self._hass.async_add_executor_job(_upload)

    async def async_delete_art(self, content_id: str) -> None:
        await self._async_art_call(self._art.delete, content_id)

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

        async with self._art_lock:
            await self._hass.async_add_executor_job(_set)

    async def _async_reset_art_connection(self) -> None:
        """Close the (likely dead) art websocket so the next call reopens fresh.

        The samsungtvws library reuses ``self.connection`` on the sync
        ``SamsungTVArt`` client and never invalidates it on failure, so a stale
        connection (e.g. after the TV power-cycles) would otherwise fail
        forever. Closing it here forces a fresh connection on the next call.
        """
        try:
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

    async def async_launch_app(self, app_id: str, app_type: str) -> None:
        await self._async_remote_commands(
            [ChannelEmitCommand.launch_app(app_id, app_type)]
        )

    async def async_app_list(self) -> list[dict[str, Any]] | None:
        """Installed apps, or None (not supported on all TVs / TV not ready)."""
        try:
            return await self._remote.app_list()
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("app_list failed: %s", err)
            return None

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
            try:
                await self._remote.close()
            except Exception:  # noqa: BLE001
                pass
            await self._remote.send_commands(commands)

    async def async_start_art_listener(
        self, callback: Callable[[str, Any], None]
    ) -> None:
        await self._hass.async_add_executor_job(self._art_listener.start_listening, callback)

    async def async_restart_art_listener(
        self, callback: Callable[[str, Any], None]
    ) -> None:
        """Rebuild the art push listener from scratch and restart it.

        Called when the coordinator sees the TV go unreachable -> reachable
        again (e.g. after a power cycle). The listener thread may have died
        (uncaught socket error) and a crashed instance holds stale connection
        state, so we replace it with a fresh ``SamsungTVArt`` before restarting.
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
        try:
            await self._hass.async_add_executor_job(self._art_listener.close)
        except Exception:  # noqa: BLE001
            pass
