# tests/test_binary_sensor.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)


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


async def test_art_binary_sensor_on(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    await _setup(hass, mock_device)
    state = hass.states.get("binary_sensor.samsung_frame_tv_art_mode")
    assert state is not None
    assert state.state == "on"
