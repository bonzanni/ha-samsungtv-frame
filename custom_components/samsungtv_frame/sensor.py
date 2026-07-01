"""TV-mode ENUM sensor for Samsung Frame TV — automation source of truth."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FrameConfigEntry
from .entity import FrameEntity
from .models import TvMode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameTvModeSensor(entry.runtime_data)])


class FrameTvModeSensor(FrameEntity, SensorEntity):
    """off / watching / art_mode — the entity automations trigger on."""

    _attr_translation_key = "tv_mode"
    _attr_name = "TV mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [TvMode.OFF, TvMode.WATCHING, TvMode.ART_MODE]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['mac']}_tv_mode"

    @property
    def native_value(self) -> str | None:
        mode = self.coordinator.data.tv_mode
        return mode if mode in self._attr_options else None
