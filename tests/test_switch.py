# tests/test_switch.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)

ENTITY = "switch.samsung_frame_tv_art_mode_switch"


async def _setup(hass, mock_device):
    mock_device.art_ready = True
    mock_device.art_generation = 1
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "t"},
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_switch_reflects_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    await _setup(hass, mock_device)
    assert hass.states.get(ENTITY).state == "on"


async def test_switch_off_when_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    assert hass.states.get(ENTITY).state == "off"


async def test_switch_unavailable_when_tv_off(hass, mock_device):
    mock_device.async_device_info.return_value = None
    await _setup(hass, mock_device)
    assert hass.states.get(ENTITY).state == "unavailable"


async def test_switch_turn_on_sets_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": ENTITY}, blocking=True,
    )
    mock_device.async_set_artmode.assert_awaited_once_with(True)


async def test_switch_turn_off_leaves_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": ENTITY}, blocking=True,
    )
    mock_device.async_set_artmode.assert_awaited_once_with(False)
