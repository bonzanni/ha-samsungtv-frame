"""Art Mode setting selects for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MAC
from .coordinator import FrameConfigEntry, FrameCoordinator
from .entity import FrameEntity, art_setting_available
from .models import ArtSettingKey

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Art Mode setting selects."""
    async_add_entities(
        [
            FrameArtSleepAfterSelect(entry.runtime_data),
            FrameArtMotionSensitivitySelect(entry.runtime_data),
        ]
    )


class FrameArtSleepAfterSelect(FrameEntity, SelectEntity):
    """Select how long Art Mode waits before sleeping."""

    _attr_translation_key = "art_sleep_after"
    _attr_icon = "mdi:sleep"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["off", "5", "15", "30", "60", "120", "240"]

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_MAC]}_art_sleep_after"
        )

    @property
    def current_option(self) -> str | None:
        """Return the current sleep-after wire value."""
        settings = self.coordinator.data.art_settings
        return settings.motion_timer if settings is not None else None

    @property
    def available(self) -> bool:
        """Return whether the current sleep-after value is authoritative."""
        return super().available and art_setting_available(
            self.coordinator,
            ArtSettingKey.MOTION_TIMER,
        )

    async def async_select_option(self, option: str) -> None:
        """Set the sleep-after wire value."""
        try:
            await self.coordinator.device.async_set_motion_timer(option)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("Failed to set art sleep after") from err
        await self.coordinator.async_request_art_reconcile()


class FrameArtMotionSensitivitySelect(FrameEntity, SelectEntity):
    """Select one neutral motion-sensitivity protocol state."""

    _attr_translation_key = "art_motion_sensitivity"
    _attr_icon = "mdi:motion-sensor"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["1", "2", "3"]

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_MAC]}_art_motion_sensitivity"
        )

    @property
    def current_option(self) -> str | None:
        """Return the current motion-sensitivity wire value."""
        settings = self.coordinator.data.art_settings
        return settings.motion_sensitivity if settings is not None else None

    @property
    def available(self) -> bool:
        """Return whether the current sensitivity value is authoritative."""
        return super().available and art_setting_available(
            self.coordinator,
            ArtSettingKey.MOTION_SENSITIVITY,
        )

    async def async_select_option(self, option: str) -> None:
        """Set the motion-sensitivity wire value."""
        try:
            await self.coordinator.device.async_set_motion_sensitivity(option)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(
                "Failed to set art motion sensitivity"
            ) from err
        await self.coordinator.async_request_art_reconcile()
