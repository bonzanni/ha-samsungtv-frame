"""Media player entity for Samsung Frame TV (state + power + basic controls)."""
from __future__ import annotations

from pathlib import Path

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
    ATTR_CATEGORY_ID,
    ATTR_CONTENT_ID,
    ATTR_DURATION,
    ATTR_ENABLED,
    ATTR_KEY,
    ATTR_MATTE,
    ATTR_PATH,
    ATTR_SHOW,
    ATTR_SHUFFLE,
    CONF_MAC,
    SERVICE_DELETE_ART,
    SERVICE_SELECT_ART,
    SERVICE_SEND_KEY,
    SERVICE_SET_ART_MODE,
    SERVICE_SET_SLIDESHOW,
    SERVICE_UPLOAD_ART,
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
    platform.async_register_entity_service(
        SERVICE_SELECT_ART,
        {
            vol.Required(ATTR_CONTENT_ID): cv.string,
            vol.Optional(ATTR_SHOW, default=True): cv.boolean,
        },
        "async_select_art_service",
    )
    platform.async_register_entity_service(
        SERVICE_UPLOAD_ART,
        {
            vol.Required(ATTR_PATH): cv.string,
            vol.Optional(ATTR_MATTE, default="none"): cv.string,
            vol.Optional(ATTR_SHOW, default=True): cv.boolean,
        },
        "async_upload_art_service",
    )
    platform.async_register_entity_service(
        SERVICE_DELETE_ART,
        {vol.Required(ATTR_CONTENT_ID): cv.string},
        "async_delete_art_service",
    )
    platform.async_register_entity_service(
        SERVICE_SET_SLIDESHOW,
        {
            vol.Required(ATTR_DURATION): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=1440)
            ),
            vol.Optional(ATTR_SHUFFLE, default=True): cv.boolean,
            vol.Optional(ATTR_CATEGORY_ID, default="MY-C0002"): cv.string,
        },
        "async_set_slideshow_service",
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
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
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

    @property
    def source(self) -> str | None:
        data = self.coordinator.data
        if data.tv_mode is not TvMode.WATCHING:
            return None
        # No visible app while watching = live TV or an HDMI input.
        return data.running_app or "TV"

    @property
    def app_name(self) -> str | None:
        return self.coordinator.data.running_app

    @property
    def volume_level(self) -> float | None:
        return self.coordinator.data.volume_level

    @property
    def is_volume_muted(self) -> bool | None:
        return self.coordinator.data.is_muted

    async def async_turn_on(self) -> None:
        await self.coordinator.device.async_turn_on()
        self.coordinator.async_notify_turn_on()

    async def async_turn_off(self) -> None:
        await self.coordinator.device.async_turn_off()

    async def async_volume_up(self) -> None:
        await self._async_send_key("KEY_VOLUP")

    async def async_volume_down(self) -> None:
        await self._async_send_key("KEY_VOLDOWN")

    async def async_set_volume_level(self, volume: float) -> None:
        try:
            await self.coordinator.device.async_set_volume(volume)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to set volume on the TV") from err
        await self.coordinator.async_request_refresh()

    async def async_mute_volume(self, mute: bool) -> None:
        try:
            await self.coordinator.device.async_set_mute(mute)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to set mute on the TV") from err
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        await self._async_send_key("KEY_PLAY")

    async def async_media_pause(self) -> None:
        await self._async_send_key("KEY_PAUSE")

    async def async_media_stop(self) -> None:
        await self._async_send_key("KEY_STOP")

    async def async_media_next_track(self) -> None:
        await self._async_send_key("KEY_CHUP")

    async def async_media_previous_track(self) -> None:
        await self._async_send_key("KEY_CHDOWN")

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

    async def async_select_art_service(self, content_id: str, show: bool) -> None:
        try:
            await self.coordinator.device.async_select_art(content_id, show)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Failed to select artwork {content_id}") from err
        await self.coordinator.async_request_refresh()

    async def async_upload_art_service(
        self, path: str, matte: str, show: bool
    ) -> None:
        hass = self.hass
        if not hass.config.is_allowed_path(path):
            raise ServiceValidationError(
                f"Path '{path}' is not in allowlist_external_dirs"
            )
        file = Path(path)
        if not file.is_file():
            raise ServiceValidationError(f"No such file: {path}")
        file_type = file.suffix.lstrip(".").lower() or "png"
        data = await hass.async_add_executor_job(file.read_bytes)
        try:
            content_id = await self.coordinator.device.async_upload_art(
                data, file_type, matte
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Failed to upload {path} to the TV") from err
        if show:
            await self.async_select_art_service(content_id, True)

    async def async_delete_art_service(self, content_id: str) -> None:
        try:
            await self.coordinator.device.async_delete_art(content_id)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Failed to delete artwork {content_id}") from err
        await self.coordinator.async_request_refresh()

    async def async_set_slideshow_service(
        self, duration_minutes: int, shuffle: bool, category_id: str
    ) -> None:
        try:
            await self.coordinator.device.async_set_slideshow(
                duration_minutes, shuffle, category_id
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to configure the slideshow") from err

    async def _async_send_key(self, key: str) -> None:
        try:
            await self.coordinator.device.async_send_key(key)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Failed to send {key} to the TV") from err
