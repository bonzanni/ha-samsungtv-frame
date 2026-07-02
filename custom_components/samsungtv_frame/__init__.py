"""The Samsung Frame TV integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .art_listener import make_art_bridge
from .const import CONF_HOST, CONF_MAC, CONF_TOKEN, PLATFORMS
from .coordinator import FrameConfigEntry, FrameCoordinator
from .device import FrameDevice


async def async_setup_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Set up Samsung Frame TV from a config entry."""
    device = FrameDevice(
        hass,
        host=entry.data[CONF_HOST],
        mac=entry.data[CONF_MAC],
        token=entry.data.get(CONF_TOKEN),
    )
    coordinator = FrameCoordinator(hass, entry, device)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Start push art listener (best-effort; poll heartbeat is the fallback).
    bridge_callback = make_art_bridge(hass, coordinator)
    try:
        await device.async_start_art_listener(bridge_callback)
    except Exception:  # noqa: BLE001 - listener is an enhancement, not required
        pass

    # Same callback is reused so a post-power-cycle restart wires up the
    # identical bridge (see coordinator._async_update_data edge detection).
    async def _restart_listener() -> None:
        await device.async_restart_art_listener(bridge_callback)

    coordinator.restart_listener = _restart_listener

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.device.async_stop()
    return unloaded
