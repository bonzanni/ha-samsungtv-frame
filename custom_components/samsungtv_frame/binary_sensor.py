"""Art-mode binary sensor for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FrameConfigEntry
from .entity import FrameEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameArtModeBinarySensor(entry.runtime_data)])


class FrameArtModeBinarySensor(FrameEntity, BinarySensorEntity):
    """True when the TV is displaying art mode."""

    _attr_translation_key = "art_mode"
    _attr_name = "Art mode"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['mac']}_art_mode"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.art_mode
