"""Media player entity for Samsung Frame TV (P1a: state + power)."""
from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FrameConfigEntry
from .entity import FrameEntity
from .models import TvMode

PARALLEL_UPDATES = 0

_MODE_TO_STATE = {
    TvMode.OFF: MediaPlayerState.OFF,
    TvMode.WATCHING: MediaPlayerState.PLAYING,
    TvMode.ART_MODE: MediaPlayerState.ON,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameMediaPlayer(entry.runtime_data)])


class FrameMediaPlayer(FrameEntity, MediaPlayerEntity):
    """Standard media_player surface; art lives in the sensors, never here."""

    _attr_name = None  # main feature of the device
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data["mac"]

    @property
    def state(self) -> MediaPlayerState | None:
        return _MODE_TO_STATE.get(self.coordinator.data.tv_mode)

    async def async_turn_on(self) -> None:
        await self.coordinator.device.async_turn_on()
        self.coordinator.async_notify_turn_on()

    async def async_turn_off(self) -> None:
        await self.coordinator.device.async_turn_off()
