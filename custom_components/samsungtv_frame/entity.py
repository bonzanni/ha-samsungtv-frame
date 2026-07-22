"""Shared base entity for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MAC, CONF_MODEL, DOMAIN
from .coordinator import FrameCoordinator
from .models import ArtSettingKey, TvMode


def optional_art_state_fresh(coordinator: FrameCoordinator) -> bool:
    """Return whether optional Art state is authoritative right now."""
    return (
        coordinator.device.art_ready
        and coordinator.data.optional_art_generation
        == coordinator.device.art_generation
        and coordinator.data.tv_mode in (TvMode.WATCHING, TvMode.ART_MODE)
    )


def art_setting_available(
    coordinator: FrameCoordinator,
    key: ArtSettingKey,
    value: object | None,
) -> bool:
    """Return whether one advertised setting has a current valid value."""
    settings = coordinator.data.art_settings
    return (
        optional_art_state_fresh(coordinator)
        and settings is not None
        and key in settings.supported
        and value is not None
    )


class FrameEntity(CoordinatorEntity[FrameCoordinator]):
    """Base for all Frame entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        mac = entry.data[CONF_MAC]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={(CONNECTION_NETWORK_MAC, mac)},
            manufacturer="Samsung",
            model=entry.data.get(CONF_MODEL, "The Frame"),
            name="Samsung Frame TV",
        )
