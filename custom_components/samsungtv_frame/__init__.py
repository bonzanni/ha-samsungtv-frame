"""The Samsung Frame TV integration."""
from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from samsungtvws.helper import get_ssl_context

from .const import (
    ART_CLOSE_DEADLINE,
    ART_CONNECT_DEADLINE,
    CONF_HOST,
    CONF_MAC,
    CONF_TOKEN,
    LOGGER,
    PLATFORMS,
)
from .coordinator import FrameConfigEntry, FrameCoordinator
from .device import FrameDevice


async def _async_stop_after_setup_failure(device: FrameDevice) -> None:
    """Bound failed-setup cleanup without replacing the setup error."""
    try:
        async with asyncio.timeout(
            ART_CONNECT_DEADLINE + ART_CLOSE_DEADLINE
        ):
            await device.async_stop()
    except asyncio.CancelledError:
        raise
    except BaseException:  # noqa: BLE001 - preserve the setup error
        LOGGER.warning("Device cleanup after setup failure did not finish")


async def async_setup_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Set up Samsung Frame TV from a config entry."""
    if not entry.data.get(CONF_TOKEN):
        raise ConfigEntryAuthFailed("Authentication required")
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
    try:
        device.set_art_event_callback(coordinator.handle_art_event)
        device.set_art_session_state_callback(
            coordinator.handle_art_session_state
        )
        device.set_remote_token_callback(coordinator.handle_remote_token)
        device.set_remote_reauth_callback(coordinator.handle_remote_reauth)
        await device.async_start_art_session()
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(
            entry, PLATFORMS
        )
    except BaseException:
        callbacks = (
            ("Art session", device.set_art_session_state_callback),
            ("remote token", device.set_remote_token_callback),
            ("remote reauthorization", device.set_remote_reauth_callback),
        )
        for name, setter in callbacks:
            try:
                setter(None)
            except BaseException:  # noqa: BLE001 - preserve setup error
                LOGGER.warning(
                    "Could not clear %s callback after setup failure",
                    name,
                )
        await _async_stop_after_setup_failure(device)
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data

    def _restore_callbacks() -> None:
        coordinator.device.set_art_session_state_callback(
            coordinator.handle_art_session_state
        )
        coordinator.device.set_remote_token_callback(
            coordinator.handle_remote_token
        )
        coordinator.device.set_remote_reauth_callback(
            coordinator.handle_remote_reauth
        )

    try:
        # Stop new remote work and drain in-flight work before callbacks can
        # disappear. This boundary remains reversible until platforms unload.
        await coordinator.device.async_quiesce_remote()
        coordinator.device.set_art_session_state_callback(None)
        coordinator.device.set_remote_token_callback(None)
        coordinator.device.set_remote_reauth_callback(None)
        unloaded = await hass.config_entries.async_unload_platforms(
            entry, PLATFORMS
        )
    except BaseException:
        _restore_callbacks()
        coordinator.device.resume_remote()
        raise
    if not unloaded:
        _restore_callbacks()
        coordinator.device.resume_remote()
        return False
    await coordinator.device.async_stop()
    return True
