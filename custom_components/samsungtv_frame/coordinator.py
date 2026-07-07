"""Data update coordinator for a Samsung Frame TV."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_TOKEN,
    DEFAULT_HEARTBEAT,
    DOMAIN,
    LOGGER,
    OFF_DEBOUNCE_COUNT,
    PORT_REST,
    WAKE_PROBE_ATTEMPTS,
    WAKE_PROBE_DELAY,
    WAKE_PROBE_TIMEOUT,
)
from .device import FrameDevice
from .models import FrameData, TvMode, derive_tv_mode

type FrameConfigEntry = ConfigEntry[FrameCoordinator]


class FrameCoordinator(DataUpdateCoordinator[FrameData]):
    """Fan REST + art signals into one FrameData, with OFF debounce."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: FrameDevice
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_HEARTBEAT,
            config_entry=entry,
            always_update=False,
        )
        self.device = device
        self._unreachable_count = 0
        self._art_mode: bool | None = None
        self._was_reachable = True
        self._wake_task: asyncio.Task | None = None
        # Set by __init__.py once the initial art listener + bridge callback
        # exist, so a power-cycle recovery can restart the SAME listener.
        self.restart_listener: Callable[[], Awaitable[None]] | None = None

    def _last_stable(self) -> TvMode:
        if self.data is not None and self.data.tv_mode is not TvMode.UNKNOWN:
            return self.data.tv_mode
        return TvMode.UNKNOWN

    async def _async_update_data(self) -> FrameData:
        info = await self.device.async_device_info()
        reachable = info is not None
        power_state = info.get("PowerState") if info else None
        current_art: str | None = None

        was_reachable = self._was_reachable
        self._was_reachable = reachable
        if reachable and not was_reachable and self.restart_listener is not None:
            self.hass.async_create_task(self._restart_listener_safe())

        if reachable:
            self._unreachable_count = 0
            self._art_mode = await self.device.async_get_artmode()
            self._async_capture_token()
        else:
            self._unreachable_count += 1

        # OFF debounce: only declare OFF after N consecutive unreachable polls.
        if not reachable and self._unreachable_count < OFF_DEBOUNCE_COUNT:
            mode = self._last_stable()
        else:
            mode = derive_tv_mode(reachable, self._art_mode, power_state)
            if mode is TvMode.UNKNOWN:
                mode = self._last_stable()

        # When the TV is off we know art mode cannot be active, even if the
        # last cached value says otherwise.  Keep self._art_mode unchanged so
        # the cached state is still valid once the TV comes back.
        art_mode_out = False if mode is TvMode.OFF else self._art_mode
        return FrameData(
            reachable=reachable,
            power_state=power_state,
            art_mode=art_mode_out,
            tv_mode=mode,
            current_art=current_art,
        )

    @callback
    def _async_capture_token(self) -> None:
        """Persist a token the TV issued after pairing, if one appeared.

        Pairing granted access by client name without issuing a token; if the
        TV hands one out on any later connection, store it in the config entry
        so reconnects (and TV-side grant loss) don't depend on the name grant.
        """
        token = self.device.newest_token
        if token is None or token == self.config_entry.data.get(CONF_TOKEN):
            return
        self.device.update_token(token)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_TOKEN: token},
        )
        LOGGER.info("Captured a newly issued TV token into the config entry")

    @callback
    def async_notify_turn_on(self) -> None:
        """Kick a fast wake probe after turn_on (WoL) was sent.

        The heartbeat alone reacts slowly to wake-up: while the TV boots,
        each poll burns the full REST timeout before the next one is even
        scheduled, so consecutive polls end up ~18 s apart. Probing the REST
        port directly is cheap, so we can afford a tight loop and refresh
        the moment the TV answers.
        """
        if self._wake_task is not None and not self._wake_task.done():
            return
        self._wake_task = self.config_entry.async_create_background_task(
            self.hass, self._wake_probe(), f"{DOMAIN}-wake-probe"
        )

    async def _wake_probe(self) -> None:
        for _ in range(WAKE_PROBE_ATTEMPTS):
            if self.data is not None and self.data.tv_mode is not TvMode.OFF:
                return
            if await self._async_probe_port():
                # Debounced (immediate on first call), so the repeat calls
                # while the art socket isn't ready yet stay cheap.
                await self.async_request_refresh()
            await asyncio.sleep(WAKE_PROBE_DELAY)

    async def _async_probe_port(self) -> bool:
        try:
            async with asyncio.timeout(WAKE_PROBE_TIMEOUT):
                _, writer = await asyncio.open_connection(self.device.host, PORT_REST)
        except Exception:  # noqa: BLE001 - any failure just means "not up yet"
            return False
        writer.close()
        return True

    async def _restart_listener_safe(self) -> None:
        """Restart the art push listener, swallowing failures.

        Called as a fire-and-forget task off the unreachable->reachable edge;
        a failure here must never break the polling loop.
        """
        try:
            await self.restart_listener()
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Failed to restart art listener: %s", err)

    @callback
    def handle_art_event(self, event: str, data: Any) -> None:
        """Loop-safe handler for pushed art events (see Task 5 bridge)."""
        sub = data.get("event") if isinstance(data, dict) else None
        if sub in ("art_mode_changed", "artmode_status"):
            value = data.get("value") or data.get("status")
            self._art_mode = value == "on"
        else:
            return
        mode = derive_tv_mode(True, self._art_mode, "on")
        if mode is TvMode.UNKNOWN:
            mode = self._last_stable()
        current = self.data
        self.async_set_updated_data(
            FrameData(
                reachable=True,
                power_state=current.power_state if current else "on",
                art_mode=self._art_mode,
                tv_mode=mode,
                current_art=current.current_art if current else None,
            )
        )
