"""Sensor entities for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MAC
from .coordinator import FrameConfigEntry, FrameCoordinator
from .entity import FrameEntity, optional_art_state_fresh
from .models import SlideshowMode, TvMode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            FrameTvModeSensor(entry.runtime_data),
            FrameCurrentArtSensor(entry.runtime_data),
            FrameSlideshowSensor(entry.runtime_data),
        ]
    )


class FrameTvModeSensor(FrameEntity, SensorEntity):
    """off / watching / art_mode — the entity automations trigger on."""

    _attr_translation_key = "tv_mode"
    _attr_name = "TV mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [TvMode.OFF, TvMode.WATCHING, TvMode.ART_MODE]

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data[CONF_MAC]}_tv_mode"

    @property
    def native_value(self) -> str | None:
        mode = self.coordinator.data.tv_mode
        return mode if mode in self._attr_options else None


class FrameCurrentArtSensor(FrameEntity, SensorEntity):
    """Content id of the artwork currently selected on the TV."""

    _attr_translation_key = "current_art"
    _attr_name = "Current art"
    _attr_icon = "mdi:image-frame"

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data[CONF_MAC]}_current_art"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.current_art


class FrameSlideshowSensor(FrameEntity, SensorEntity):
    """Expose the current Art Mode slideshow state."""

    _attr_translation_key = "art_slideshow"
    _attr_name = "Art slideshow"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(SlideshowMode)

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_MAC]}_art_slideshow"
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and optional_art_state_fresh(self.coordinator)
            and self.coordinator.data.slideshow is not None
        )

    @property
    def native_value(self) -> SlideshowMode | None:
        state = self.coordinator.data.slideshow
        return state.mode if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, int | str | None] | None:
        state = self.coordinator.data.slideshow
        if state is None:
            return None
        return {
            "duration_minutes": state.duration_minutes,
            "category_id": state.category_id,
        }
