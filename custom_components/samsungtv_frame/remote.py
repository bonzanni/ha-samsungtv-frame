"""Remote entity for Samsung Frame TV — arbitrary key sequences."""
from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_HOLD_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    DEFAULT_HOLD_SECS,
    RemoteEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MAC
from .coordinator import FrameConfigEntry, FrameCoordinator
from .entity import FrameEntity
from .models import TvMode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameRemote(entry.runtime_data)])


class FrameRemote(FrameEntity, RemoteEntity):
    """Sends remote key codes to the TV.

    Key list reference:
    https://github.com/jaruba/ha-samsungtv-tizen/blob/master/Key_codes.md
    """

    _attr_name = None

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data[CONF_MAC]}_remote"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.tv_mode is not TvMode.OFF

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.device.async_turn_on()
        self.coordinator.async_notify_turn_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.device.async_turn_off()

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        num_repeats: int = kwargs[ATTR_NUM_REPEATS]
        delay: float = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        hold: float = kwargs.get(ATTR_HOLD_SECS, DEFAULT_HOLD_SECS)
        keys = list(command)
        device = self.coordinator.device
        try:
            for repeat in range(num_repeats):
                for i, key in enumerate(keys):
                    if repeat or i:
                        await asyncio.sleep(delay)
                    if hold:
                        await device.async_hold_key(key, hold)
                    else:
                        await device.async_send_key(key)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Failed to send {keys} to the TV") from err
