# tests/test_device.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.samsungtv_frame.device import FrameDevice


@pytest.fixture
def device(hass):
    return FrameDevice(hass, host="1.2.3.4", mac="A0:D0:5B:86:CE:B7", token="tok")


async def test_device_info_returns_device_dict(hass, device):
    rest = MagicMock()
    rest.rest_device_info = AsyncMock(return_value={"device": {"PowerState": "on"}})
    with patch.object(device, "_rest", rest):
        info = await device.async_device_info()
    assert info == {"PowerState": "on"}


async def test_device_info_none_when_unreachable(hass, device):
    rest = MagicMock()
    rest.rest_device_info = AsyncMock(side_effect=OSError("timeout"))
    with patch.object(device, "_rest", rest):
        assert await device.async_device_info() is None


async def test_get_artmode_true(hass, device):
    art = MagicMock()
    art.get_artmode.return_value = "on"
    with patch.object(device, "_art", art):
        assert await device.async_get_artmode() is True


async def test_turn_on_sends_magic_packet(hass, device):
    with patch("custom_components.samsungtv_frame.device.send_magic_packet") as smp:
        await device.async_turn_on()
    smp.assert_called_once()
    assert smp.call_args.args[0] == "A0:D0:5B:86:CE:B7"


async def test_listener_uses_separate_connection(hass, device):
    # The listener must use _art_listener, not _art, so that a prior
    # get_artmode() call (which opens _art's websocket) doesn't prevent
    # start_listening from running.
    assert device._art is not device._art_listener

    mock_art = MagicMock()
    mock_listener = MagicMock()
    device._art = mock_art
    device._art_listener = mock_listener

    await device.async_start_art_listener(lambda e, d: None)

    mock_listener.start_listening.assert_called_once()
    mock_art.start_listening.assert_not_called()


async def test_get_artmode_failure_resets_connection(hass, device):
    art = MagicMock()
    art.get_artmode.side_effect = OSError("dead socket")
    with patch.object(device, "_art", art):
        result = await device.async_get_artmode()
    assert result is None
    # One reset per attempt (initial call + the retry).
    assert art.close.call_count == 2


async def test_get_artmode_single_attempt_when_requested(hass, device):
    # attempts=1 is used while the TV reports standby (shutting down): the
    # art socket hangs until timeout there, so a retry only adds latency.
    art = MagicMock()
    art.get_artmode.side_effect = OSError("hanging socket")
    with patch.object(device, "_art", art):
        assert await device.async_get_artmode(attempts=1) is None
    assert art.get_artmode.call_count == 1
    art.close.assert_called_once()


async def test_get_artmode_retries_once_after_reset(hass, device):
    # First call hits the stale post-power-cycle connection; the retry (on a
    # fresh connection) must resolve art mode within the same poll.
    art = MagicMock()
    art.get_artmode.side_effect = [OSError("stale socket"), "on"]
    with patch.object(device, "_art", art):
        assert await device.async_get_artmode() is True
    art.close.assert_called_once()


async def test_set_artmode_failure_resets_connection_and_reraises(hass, device):
    art = MagicMock()
    art.set_artmode.side_effect = OSError("dead socket")
    with patch.object(device, "_art", art):
        with pytest.raises(OSError):
            await device.async_set_artmode(True)
    # One reset after the initial failure, one after the failed retry.
    assert art.close.call_count == 2
    assert art.set_artmode.call_count == 2


async def test_set_artmode_retries_once_on_stale_connection(hass, device):
    art = MagicMock()
    art.set_artmode.side_effect = [OSError("stale"), None]
    with patch.object(device, "_art", art):
        await device.async_set_artmode(True)
    assert art.set_artmode.call_count == 2
    art.close.assert_called_once()


async def test_listener_socket_has_no_timeout(hass, device):
    assert device._art_listener.timeout is None


async def test_newest_token_none_when_unchanged(hass, device):
    # All clients were constructed with the stored token ("tok").
    assert device.newest_token is None


async def test_newest_token_surfaces_library_issued_token(hass, device):
    device._art.token = "fresh-token"
    assert device.newest_token == "fresh-token"


async def test_update_token_used_for_fresh_listener(hass, device):
    device.update_token("fresh-token")
    with patch(
        "custom_components.samsungtv_frame.device.SamsungTVArt"
    ) as mock_cls:
        await device.async_restart_art_listener(lambda e, d: None)
    assert mock_cls.call_args.kwargs["token"] == "fresh-token"


async def test_listener_not_restarted_after_stop(hass, device):
    """An in-flight restart finishing after unload must not resurrect a
    listener nothing will ever close."""
    await device.async_stop()
    with patch(
        "custom_components.samsungtv_frame.device.SamsungTVArt"
    ) as mock_cls:
        await device.async_restart_art_listener(lambda e, d: None)
        await device.async_start_art_listener(lambda e, d: None)
    mock_cls.assert_not_called()


async def test_restart_art_listener_creates_fresh_instance(hass, device):
    old = device._art_listener
    with patch(
        "custom_components.samsungtv_frame.device.SamsungTVArt"
    ) as mock_cls:
        mock_new = MagicMock()
        mock_cls.return_value = mock_new
        await device.async_restart_art_listener(lambda e, d: None)
    assert device._art_listener is not old
    assert device._art_listener is mock_new
    mock_new.start_listening.assert_called_once()


async def test_send_key_clicks_remote(hass, device):
    remote = MagicMock()
    remote.send_commands = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")
    cmds = remote.send_commands.call_args.args[0]
    assert cmds[0].params["DataOfCmd"] == "KEY_HOME"
    assert cmds[0].params["Cmd"] == "Click"


async def test_remote_command_retries_on_stale_connection(hass, device):
    remote = MagicMock()
    remote.send_commands = AsyncMock(side_effect=[OSError("stale"), None])
    remote.close = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_VOLUP")
    assert remote.send_commands.await_count == 2
    remote.close.assert_awaited_once()


async def test_launch_app_emits_channel_command(hass, device):
    remote = MagicMock()
    remote.send_commands = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_launch_app("11101200001", "DEEP_LINK")
    cmds = remote.send_commands.call_args.args[0]
    assert cmds[0].params["event"] == "ed.apps.launch"
    assert cmds[0].params["data"]["appId"] == "11101200001"


async def test_app_list_failure_returns_none(hass, device):
    remote = MagicMock()
    remote.app_list = AsyncMock(side_effect=OSError("nope"))
    with patch.object(device, "_remote", remote):
        assert await device.async_app_list() is None


async def test_set_slideshow_falls_back_to_legacy_request(hass, device):
    from samsungtvws.exceptions import ResponseError

    art = MagicMock()
    art.set_auto_rotation_status.side_effect = ResponseError("unsupported")
    with patch.object(device, "_art", art):
        await device.async_set_slideshow(60, True, "MY-C0002")
    art.set_auto_rotation_status.assert_called_once_with(
        duration=60, type=True, category_id="MY-C0002"
    )
    art.set_slideshow_status.assert_called_once_with(
        duration=60, type=True, category_id="MY-C0002"
    )


async def test_upload_art_returns_content_id(hass, device):
    art = MagicMock()
    art.upload.return_value = "MY_F0100"
    with patch.object(device, "_art", art):
        result = await device.async_upload_art(b"bytes", "jpg", "none")
    assert result == "MY_F0100"
    art.upload.assert_called_once_with(
        b"bytes", matte="none", portrait_matte="none", file_type="jpg"
    )


async def test_get_current_art_returns_content_id(hass, device):
    art = MagicMock()
    art.get_current.return_value = {"content_id": "MY_F0034", "matte_id": "none"}
    with patch.object(device, "_art", art):
        assert await device.async_get_current_art() == "MY_F0034"


def _mock_rendering_control(actions: dict) -> MagicMock:
    rc = MagicMock()
    rc.action.side_effect = lambda name: actions[name]
    return rc


def _mock_action(result) -> MagicMock:
    action = MagicMock()
    action.async_call = AsyncMock(return_value=result)
    return action


async def test_get_volume_via_upnp(hass, device):
    rc = _mock_rendering_control({
        "GetVolume": _mock_action({"CurrentVolume": 23}),
        "GetMute": _mock_action({"CurrentMute": False}),
    })
    with patch.object(device, "_async_rendering_control", AsyncMock(return_value=rc)):
        vol, mute = await device.async_get_volume()
    assert vol == 0.23
    assert mute is False


async def test_get_volume_failure_resets_upnp_device(hass, device):
    device._upnp_device = MagicMock()
    with patch.object(
        device, "_async_rendering_control", AsyncMock(side_effect=OSError("down"))
    ):
        assert await device.async_get_volume() == (None, None)
    assert device._upnp_device is None


async def test_set_volume_scales_to_percent(hass, device):
    set_action = _mock_action({})
    rc = _mock_rendering_control({"SetVolume": set_action})
    with patch.object(device, "_async_rendering_control", AsyncMock(return_value=rc)):
        await device.async_set_volume(0.4)
    set_action.async_call.assert_awaited_once_with(
        InstanceID=0, Channel="Master", DesiredVolume=40
    )


async def test_turn_off_holds_power_key(hass, device):
    remote = MagicMock()
    remote.send_commands = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_turn_off()
    remote.send_commands.assert_awaited_once()
    cmd = remote.send_commands.call_args.args[0]
    # Verify the command is a 3-second hold of KEY_POWER
    assert isinstance(cmd, list) and len(cmd) == 3
    assert cmd[0].params["DataOfCmd"] == "KEY_POWER"
    assert cmd[0].params["Cmd"] == "Press"
    assert cmd[1].delay == 3
    assert cmd[2].params["DataOfCmd"] == "KEY_POWER"
    assert cmd[2].params["Cmd"] == "Release"
