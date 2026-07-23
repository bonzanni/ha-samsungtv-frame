"""Device triggers for Samsung Frame TV — tv_mode transitions in the UI."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import state as state_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_FOR,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .models import TvMode

# Each trigger fires when the tv_mode ENUM sensor reaches the given state.
TRIGGER_TYPES: dict[str, str] = {
    "turned_off": TvMode.OFF,
    "started_watching": TvMode.WATCHING,
    "entered_art_mode": TvMode.ART_MODE,
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id_or_uuid,
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Optional(CONF_FOR): cv.positive_time_period_dict,
    }
)


def _tv_mode_entity(hass: HomeAssistant, device_id: str) -> er.RegistryEntry | None:
    registry = er.async_get(hass)
    for entry in er.async_entries_for_device(registry, device_id):
        if entry.platform == DOMAIN and entry.unique_id.endswith("_tv_mode"):
            return entry
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List device triggers for a Frame TV device."""
    entry = _tv_mode_entity(hass, device_id)
    if entry is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_ENTITY_ID: entry.id,
            CONF_TYPE: trigger,
        }
        for trigger in TRIGGER_TYPES
    ]


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """Support an optional 'for' duration on every trigger.

    Notably useful on started_watching: powering off from art mode passes
    through 'watching' for a few seconds, and a small 'for' filters that out.
    """
    return {
        "extra_fields": vol.Schema(
            {vol.Optional(CONF_FOR): cv.positive_time_period_dict}
        )
    }


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a state trigger on the tv_mode sensor."""
    state_config = {
        CONF_PLATFORM: "state",
        CONF_ENTITY_ID: config[CONF_ENTITY_ID],
        state_trigger.CONF_TO: TRIGGER_TYPES[config[CONF_TYPE]],
    }
    if CONF_FOR in config:
        state_config[CONF_FOR] = config[CONF_FOR]
    state_config = await state_trigger.async_validate_trigger_config(
        hass, state_config
    )
    return await state_trigger.async_attach_trigger(
        hass, state_config, action, trigger_info, platform_type="device"
    )
