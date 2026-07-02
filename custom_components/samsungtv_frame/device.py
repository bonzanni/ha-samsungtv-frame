"""Async facade over the samsungtvws library for a Frame TV."""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from samsungtvws.art import SamsungTVArt
from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.remote import SendRemoteKey
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

    async def async_get_artmode(self) -> bool | None:
        try:
            value = await self._hass.async_add_executor_job(self._art.get_artmode)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_artmode failed: %s", err)
            await self._async_reset_art_connection()
            return None
        return value == "on"

    async def async_set_artmode(self, on: bool) -> None:
        try:
            await self._hass.async_add_executor_job(self._art.set_artmode, on)
        except Exception:
            await self._async_reset_art_connection()
            raise

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
        await self._remote.send_commands(SendRemoteKey.hold("KEY_POWER", 3))

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
