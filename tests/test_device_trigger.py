# tests/test_device_trigger.py
from unittest.mock import patch

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_tv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)
from custom_components.samsung_tv_frame.device_trigger import async_get_triggers


async def _setup(hass, mock_device):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "t"},
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsung_tv_frame.FrameDevice", return_value=mock_device
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_get_triggers_lists_mode_transitions(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(
        identifiers={(DOMAIN, "A0:D0:5B:86:CE:B7")}
    )
    assert device is not None

    triggers = await async_get_triggers(hass, device.id)
    types = {t["type"] for t in triggers}
    assert types == {"turned_off", "started_watching", "entered_art_mode"}
    assert all(t["domain"] == DOMAIN for t in triggers)


async def test_get_triggers_empty_for_unknown_device(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    device_registry = dr.async_get(hass)
    other = device_registry.async_get_or_create(
        config_entry_id=list(hass.config_entries.async_entry_ids())[0],
        identifiers={("other_domain", "xyz")},
    )
    assert await async_get_triggers(hass, other.id) == []
