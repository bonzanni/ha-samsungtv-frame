"""Shared base entity for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MODEL, DOMAIN
from .coordinator import FrameCoordinator


class FrameEntity(CoordinatorEntity[FrameCoordinator]):
    """Base for all Frame entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        mac = entry.data["mac"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={("mac", mac)},
            manufacturer="Samsung",
            model=entry.data.get(CONF_MODEL, "The Frame"),
            name="Samsung Frame TV",
        )
