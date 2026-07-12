"""The Samsung Frame TV integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from samsungtvws.helper import get_ssl_context

from .const import CONF_HOST, CONF_MAC, CONF_TOKEN, PLATFORMS
from .coordinator import FrameConfigEntry, FrameCoordinator
from .device import FrameDevice


async def async_setup_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Set up Samsung Frame TV from a config entry."""
    ssl_context = await hass.async_add_executor_job(get_ssl_context)

    def task_factory(coroutine, name):
        return entry.async_create_background_task(hass, coroutine, name)

    device = FrameDevice(
        hass,
        host=entry.data[CONF_HOST],
        mac=entry.data[CONF_MAC],
        token=entry.data.get(CONF_TOKEN),
        ssl_context=ssl_context,
        task_factory=task_factory,
    )
    coordinator = FrameCoordinator(hass, entry, device)
    device.set_art_event_callback(coordinator.handle_art_event)
    coordinator.restart_listener = device.async_restart_art_listener
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.device.async_stop()
    return unloaded
