# tests/test_media_player.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)


async def _setup(hass, mock_device):
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


async def test_media_player_reports_playing_when_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    state = hass.states.get("media_player.samsung_frame_tv")
    assert state is not None
    assert state.state == "playing"


async def test_turn_on_sends_wol_and_kicks_wake_probe(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    entry = await _setup(hass, mock_device)
    coordinator = entry.runtime_data
    with patch.object(coordinator, "async_notify_turn_on") as notify:
        await hass.services.async_call(
            "media_player", "turn_on",
            {"entity_id": "media_player.samsung_frame_tv"}, blocking=True,
        )
    mock_device.async_turn_on.assert_awaited_once()
    notify.assert_called_once()


async def test_turn_off_calls_device(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "media_player", "turn_off",
        {"entity_id": "media_player.samsung_frame_tv"}, blocking=True,
    )
    mock_device.async_turn_off.assert_awaited_once()
