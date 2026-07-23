# tests/test_remote.py
from unittest.mock import call, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_tv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)

ENTITY = "remote.samsung_frame_tv"


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


async def test_remote_reflects_power(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    await _setup(hass, mock_device)
    assert hass.states.get(ENTITY).state == "on"


async def test_remote_send_command_repeats(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "remote", "send_command",
        {"entity_id": ENTITY, "command": ["KEY_RIGHT", "KEY_ENTER"],
         "num_repeats": 2, "delay_secs": 0},
        blocking=True,
    )
    assert mock_device.async_send_key.await_args_list == [
        call("KEY_RIGHT"), call("KEY_ENTER"),
        call("KEY_RIGHT"), call("KEY_ENTER"),
    ]


async def test_remote_send_command_hold(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "remote", "send_command",
        {"entity_id": ENTITY, "command": ["KEY_POWER"], "hold_secs": 3},
        blocking=True,
    )
    mock_device.async_hold_key.assert_awaited_once_with("KEY_POWER", 3)
    mock_device.async_send_key.assert_not_awaited()


async def test_remote_turn_on_off(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "remote", "turn_on", {"entity_id": ENTITY}, blocking=True,
    )
    mock_device.async_turn_on.assert_awaited_once()
    await hass.services.async_call(
        "remote", "turn_off", {"entity_id": ENTITY}, blocking=True,
    )
    mock_device.async_turn_off.assert_awaited_once()
