"""Art-mode brightness number entity for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MAC
from .coordinator import FrameConfigEntry, FrameCoordinator
from .entity import FrameEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameArtBrightnessNumber(entry.runtime_data)])


class FrameArtBrightnessNumber(FrameEntity, NumberEntity):
    """Brightness of the art-mode panel (Frame scale 0-10)."""

    _attr_translation_key = "art_brightness"
    _attr_name = "Art brightness"
    _attr_icon = "mdi:brightness-6"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 1

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_MAC]}_art_brightness"
        )

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.art_brightness

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self.coordinator.device.async_set_art_brightness(int(value))
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to set art brightness") from err
        await self.coordinator.async_request_refresh()
