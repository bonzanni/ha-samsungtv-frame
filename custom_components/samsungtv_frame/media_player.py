"""Media player entity for Samsung Frame TV (state + power + basic controls)."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_ENABLED,
    ATTR_KEY,
    CONF_MAC,
    SERVICE_SEND_KEY,
    SERVICE_SET_ART_MODE,
)
from .coordinator import FrameConfigEntry, FrameCoordinator
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
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SEND_KEY,
        {vol.Required(ATTR_KEY): cv.string},
        "async_send_key_service",
    )
    platform.async_register_entity_service(
        SERVICE_SET_ART_MODE,
        {vol.Required(ATTR_ENABLED): cv.boolean},
        "async_set_art_mode_service",
    )
    async_add_entities([FrameMediaPlayer(entry.runtime_data)])


class FrameMediaPlayer(FrameEntity, MediaPlayerEntity):
    """Standard media_player surface; art state lives in the sensors, never here."""

    _attr_name = None  # main feature of the device
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data[CONF_MAC]

    @property
    def state(self) -> MediaPlayerState | None:
        return _MODE_TO_STATE.get(self.coordinator.data.tv_mode)

    @property
    def source_list(self) -> list[str] | None:
        if self.coordinator.app_map is None:
            return None
        return sorted(self.coordinator.app_map)

    async def async_turn_on(self) -> None:
        await self.coordinator.device.async_turn_on()
        self.coordinator.async_notify_turn_on()

    async def async_turn_off(self) -> None:
        await self.coordinator.device.async_turn_off()

    async def async_volume_up(self) -> None:
        await self._async_send_key("KEY_VOLUP")

    async def async_volume_down(self) -> None:
        await self._async_send_key("KEY_VOLDOWN")

    async def async_mute_volume(self, mute: bool) -> None:
        # The TV only exposes a mute toggle key; there is no absolute setter.
        await self._async_send_key("KEY_MUTE")

    async def async_media_play(self) -> None:
        await self._async_send_key("KEY_PLAY")

    async def async_media_pause(self) -> None:
        await self._async_send_key("KEY_PAUSE")

    async def async_select_source(self, source: str) -> None:
        app_map = self.coordinator.app_map or {}
        app = app_map.get(source)
        if app is None:
            raise ServiceValidationError(
                f"Unknown source '{source}'; the TV reports "
                f"{len(app_map)} installed apps"
            )
        app_type = "DEEP_LINK" if app.get("app_type") == 2 else "NATIVE_LAUNCH"
        try:
            await self.coordinator.device.async_launch_app(app["appId"], app_type)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Failed to launch {source}") from err

    async def async_send_key_service(self, key: str) -> None:
        await self._async_send_key(key)

    async def async_set_art_mode_service(self, enabled: bool) -> None:
        try:
            await self.coordinator.device.async_set_artmode(enabled)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to set art mode on the TV") from err
        await self.coordinator.async_request_refresh()

    async def _async_send_key(self, key: str) -> None:
        try:
            await self.coordinator.device.async_send_key(key)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Failed to send {key} to the TV") from err
