"""Art-mode switch for Samsung Frame TV — the clickable art⇄watching toggle."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MAC
from .coordinator import FrameConfigEntry, FrameCoordinator
from .entity import FrameEntity
from .models import TvMode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameArtModeSwitch(entry.runtime_data)])


class FrameArtModeSwitch(FrameEntity, SwitchEntity):
    """On = art mode, off = watching. Unavailable while the TV is off."""

    _attr_translation_key = "art_mode_switch"
    _attr_name = "Art mode switch"
    _attr_icon = "mdi:palette"

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_MAC]}_art_mode_switch"
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data.tv_mode
            in (TvMode.WATCHING, TvMode.ART_MODE)
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.art_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        try:
            await self.coordinator.device.async_set_artmode(on)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to switch art mode") from err
        await self.coordinator.async_request_refresh()
