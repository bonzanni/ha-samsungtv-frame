"""Switch entities for Samsung Frame TV."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MAC
from .coordinator import FrameConfigEntry, FrameCoordinator
from .entity import FrameEntity, art_setting_available
from .models import ArtSettingKey, TvMode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        [
            FrameArtModeSwitch(entry.runtime_data),
            FrameBrightnessSensorSwitch(entry.runtime_data),
        ]
    )


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


class FrameBrightnessSensorSwitch(FrameEntity, SwitchEntity):
    """Configure the Art Mode automatic brightness sensor."""

    _attr_translation_key = "art_brightness_sensor"
    _attr_icon = "mdi:brightness-auto"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_MAC]}_art_brightness_sensor"
        )

    @property
    def available(self) -> bool:
        return super().available and art_setting_available(
            self.coordinator,
            ArtSettingKey.BRIGHTNESS_SENSOR,
        )

    @property
    def is_on(self) -> bool | None:
        settings = self.coordinator.data.art_settings
        return (
            settings.brightness_sensor_enabled if settings is not None else None
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        try:
            await self.coordinator.device.async_set_brightness_sensor(enabled)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to set art brightness sensor") from err
        await self.coordinator.async_request_art_reconcile()
