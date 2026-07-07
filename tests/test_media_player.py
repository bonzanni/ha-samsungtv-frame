# tests/test_media_player.py
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)

ENTITY = "media_player.samsung_frame_tv"


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


@pytest.mark.parametrize(
    ("service", "data", "expected_key"),
    [
        ("volume_up", {}, "KEY_VOLUP"),
        ("volume_down", {}, "KEY_VOLDOWN"),
        ("media_play", {}, "KEY_PLAY"),
        ("media_pause", {}, "KEY_PAUSE"),
        ("media_stop", {}, "KEY_STOP"),
        ("media_next_track", {}, "KEY_CHUP"),
        ("media_previous_track", {}, "KEY_CHDOWN"),
    ],
)
async def test_key_backed_controls(hass, mock_device, service, data, expected_key):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "media_player", service, {"entity_id": ENTITY, **data}, blocking=True,
    )
    mock_device.async_send_key.assert_awaited_once_with(expected_key)


async def test_volume_level_and_mute_reported(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_get_volume.return_value = (0.23, False)
    await _setup(hass, mock_device)
    state = hass.states.get(ENTITY)
    assert state.attributes["volume_level"] == 0.23
    assert state.attributes["is_volume_muted"] is False


async def test_set_volume_level(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "media_player", "volume_set",
        {"entity_id": ENTITY, "volume_level": 0.4}, blocking=True,
    )
    mock_device.async_set_volume.assert_awaited_once_with(0.4)


async def test_mute_uses_upnp_absolute_mute(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "media_player", "volume_mute",
        {"entity_id": ENTITY, "is_volume_muted": True}, blocking=True,
    )
    mock_device.async_set_mute.assert_awaited_once_with(True)


async def test_send_key_failure_raises_ha_error(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_send_key.side_effect = OSError("gone")
    await _setup(hass, mock_device)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "media_player", "volume_up", {"entity_id": ENTITY}, blocking=True,
        )


async def test_select_source_launches_app(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_app_list.return_value = [
        {"name": "Netflix", "appId": "11101200001", "app_type": 2},
        {"name": "YouTube", "appId": "111299001912", "app_type": 4},
    ]
    await _setup(hass, mock_device)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(ENTITY)
    assert state.attributes["source_list"] == ["Netflix", "YouTube"]

    await hass.services.async_call(
        "media_player", "select_source",
        {"entity_id": ENTITY, "source": "Netflix"}, blocking=True,
    )
    mock_device.async_launch_app.assert_awaited_once_with("11101200001", "DEEP_LINK")

    mock_device.async_launch_app.reset_mock()
    await hass.services.async_call(
        "media_player", "select_source",
        {"entity_id": ENTITY, "source": "YouTube"}, blocking=True,
    )
    mock_device.async_launch_app.assert_awaited_once_with(
        "111299001912", "NATIVE_LAUNCH"
    )


async def test_select_source_unknown_app_raises(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "media_player", "select_source",
            {"entity_id": ENTITY, "source": "Nope"}, blocking=True,
        )


async def test_no_source_list_when_app_list_unsupported(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_app_list.return_value = None
    await _setup(hass, mock_device)
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(ENTITY)
    assert "source_list" not in state.attributes


async def test_send_key_entity_service(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        DOMAIN, "send_key",
        {"entity_id": ENTITY, "key": "KEY_HOME"}, blocking=True,
    )
    mock_device.async_send_key.assert_awaited_once_with("KEY_HOME")


async def test_set_art_mode_entity_service(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        DOMAIN, "set_art_mode",
        {"entity_id": ENTITY, "enabled": True}, blocking=True,
    )
    mock_device.async_set_artmode.assert_awaited_once_with(True)


async def test_source_and_app_name_reflect_running_app(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_app_list.return_value = [
        {"name": "Netflix", "appId": "NETFLIX_ID", "app_type": 2},
    ]

    async def _status(app_id):
        return {"visible": True, "running": True}

    mock_device.async_app_status.side_effect = _status
    await _setup(hass, mock_device)
    # First poll ran before the app list existed; the next one sweeps.
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    await coordinator.async_refresh()
    state = hass.states.get(ENTITY)
    assert state.attributes["source"] == "Netflix"
    assert state.attributes["app_name"] == "Netflix"


async def test_source_falls_back_to_tv_when_no_app_visible(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_app_list.return_value = [
        {"name": "Netflix", "appId": "NETFLIX_ID", "app_type": 2},
    ]
    mock_device.async_app_status.return_value = {"visible": False}
    await _setup(hass, mock_device)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    await coordinator.async_refresh()
    state = hass.states.get(ENTITY)
    assert state.attributes["source"] == "TV"
    assert "app_name" not in state.attributes


async def test_select_art_service(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        DOMAIN, "select_art",
        {"entity_id": ENTITY, "content_id": "MY_F0034"}, blocking=True,
    )
    mock_device.async_select_art.assert_awaited_once_with("MY_F0034", True)


async def test_upload_art_service(hass, mock_device, tmp_path):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    img = tmp_path / "monet.jpg"
    img.write_bytes(b"fake-jpeg-bytes")
    hass.config.allowlist_external_dirs = {str(tmp_path)}
    await _setup(hass, mock_device)
    await hass.services.async_call(
        DOMAIN, "upload_art",
        {"entity_id": ENTITY, "path": str(img)}, blocking=True,
    )
    mock_device.async_upload_art.assert_awaited_once_with(
        b"fake-jpeg-bytes", "jpg", "none"
    )
    # show=True default selects the freshly uploaded content id
    mock_device.async_select_art.assert_awaited_once_with("MY_F9999", True)


async def test_upload_art_rejects_disallowed_path(hass, mock_device, tmp_path):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    img = tmp_path / "monet.jpg"
    img.write_bytes(b"x")
    await _setup(hass, mock_device)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "upload_art",
            {"entity_id": ENTITY, "path": str(img)}, blocking=True,
        )
    mock_device.async_upload_art.assert_not_awaited()


async def test_delete_art_service(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        DOMAIN, "delete_art",
        {"entity_id": ENTITY, "content_id": "MY_F0001"}, blocking=True,
    )
    mock_device.async_delete_art.assert_awaited_once_with("MY_F0001")


async def test_set_slideshow_service(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        DOMAIN, "set_slideshow",
        {"entity_id": ENTITY, "duration_minutes": 60, "shuffle": False,
         "category_id": "MY-C0004"},
        blocking=True,
    )
    mock_device.async_set_slideshow.assert_awaited_once_with(60, False, "MY-C0004")
