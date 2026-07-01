"""Data update coordinator for a Samsung Frame TV."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_HEARTBEAT, DOMAIN, LOGGER, OFF_DEBOUNCE_COUNT
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

    def _last_stable(self) -> TvMode:
        if self.data is not None and self.data.tv_mode is not TvMode.UNKNOWN:
            return self.data.tv_mode
        return TvMode.UNKNOWN

    async def _async_update_data(self) -> FrameData:
        info = await self.device.async_device_info()
        reachable = info is not None
        power_state = info.get("PowerState") if info else None
        current_art: str | None = None

        if reachable:
            self._unreachable_count = 0
            self._art_mode = await self.device.async_get_artmode()
        else:
            self._unreachable_count += 1

        # OFF debounce: only declare OFF after N consecutive unreachable polls.
        if not reachable and self._unreachable_count < OFF_DEBOUNCE_COUNT:
            mode = self._last_stable()
        else:
            mode = derive_tv_mode(reachable, self._art_mode, power_state)
            if mode is TvMode.UNKNOWN:
                mode = self._last_stable()

        return FrameData(
            reachable=reachable,
            power_state=power_state,
            art_mode=self._art_mode,
            tv_mode=mode,
            current_art=current_art,
        )

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
