# tests/test_number.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)

ENTITY = "number.samsung_frame_tv_art_brightness"


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


async def test_art_brightness_reflects_data(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_art_brightness.return_value = 7
    await _setup(hass, mock_device)
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state == "7"


async def test_art_brightness_unknown_when_off(hass, mock_device):
    mock_device.async_device_info.return_value = None
    await _setup(hass, mock_device)
    state = hass.states.get(ENTITY)
    assert state.state == "unknown"


async def test_set_art_brightness(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_art_brightness.return_value = 5
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": ENTITY, "value": 8}, blocking=True,
    )
    mock_device.async_set_art_brightness.assert_awaited_once_with(8)


async def test_color_temperature_entity(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_color_temperature.return_value = -2
    await _setup(hass, mock_device)
    entity = "number.samsung_frame_tv_art_color_temperature"
    assert hass.states.get(entity).state == "-2"
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": entity, "value": 3}, blocking=True,
    )
    mock_device.async_set_color_temperature.assert_awaited_once_with(3)
