# tests/test_device.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from samsungtvws.exceptions import ConnectionFailure

from custom_components.samsungtv_frame.device import FrameDevice


@pytest.fixture
def device(hass):
    return FrameDevice(
        hass,
        host="1.2.3.4",
        mac="A0:D0:5B:86:CE:B7",
        token="tok",
        ssl_context=MagicMock(),
        task_factory=lambda coro, name: asyncio.create_task(coro, name=name),
    )


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
    device._art.get_artmode = AsyncMock(return_value="on")
    assert await device.async_get_artmode() is True


async def test_turn_on_sends_magic_packet(hass, device):
    with patch("custom_components.samsungtv_frame.device.send_magic_packet") as smp:
        await device.async_turn_on()
    smp.assert_called_once()
    assert smp.call_args.args[0] == "A0:D0:5B:86:CE:B7"


async def test_get_artmode_retries_stale_failure_once(device):
    device._art.get_artmode = AsyncMock(side_effect=[OSError("stale"), "on"])
    device._art.close = AsyncMock()
    assert await device.async_get_artmode() is True
    device._art.close.assert_awaited_once()


async def test_get_artmode_does_not_retry_when_stop_lands_during_failure(device):
    first_call_started = asyncio.Event()
    release_first_call = asyncio.Event()

    async def get_artmode():
        if device._art.get_artmode.await_count == 1:
            first_call_started.set()
            await release_first_call.wait()
            raise OSError("shutdown closed the socket")
        await device._art.start_listening()
        return "on"

    device._art.get_artmode = AsyncMock(side_effect=get_artmode)
    device._art.start_listening = AsyncMock()
    device._art.close = AsyncMock()
    device._remote.close = AsyncMock()

    getter = asyncio.create_task(device.async_get_artmode())
    await first_call_started.wait()
    await device.async_stop()
    release_first_call.set()

    assert await getter is None
    device._art.get_artmode.assert_awaited_once()
    device._art.start_listening.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "args", "delegate"),
    [
        ("async_get_artmode", (), "get_artmode"),
        ("async_get_current_art", (), "get_current"),
        ("async_get_art_brightness", (), "get_brightness"),
        ("async_get_art_thumbnail", ("MY_F0001",), "get_thumbnail"),
        ("async_get_color_temperature", (), "get_color_temperature"),
    ],
)
async def test_art_getters_return_none_without_opening_after_stop(
    device, method, args, delegate
):
    device._stopped = True
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    assert await getattr(device, method)(*args) is None
    operation.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "args", "delegate"),
    [
        ("async_set_artmode", (True,), "set_artmode"),
        ("async_set_art_brightness", (6,), "set_brightness"),
        ("async_select_art", ("MY_F0001", True), "select_image"),
        ("async_upload_art", (b"image", "jpg", "none"), "upload"),
        ("async_delete_art", ("MY_F0001",), "delete"),
        ("async_change_matte", ("MY_F0001", "none"), "change_matte"),
        ("async_set_photo_filter", ("MY_F0001", "ink"), "set_photo_filter"),
        ("async_set_favourite", ("MY_F0001", True), "set_favourite"),
        ("async_set_color_temperature", (4,), "set_color_temperature"),
        ("async_set_slideshow", (60, False, "MY-C0002"), "set_slideshow"),
    ],
)
async def test_art_mutations_fail_without_opening_after_stop(
    device, method, args, delegate
):
    device._stopped = True
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    with pytest.raises(ConnectionFailure, match="stopped"):
        await getattr(device, method)(*args)
    operation.assert_not_awaited()


async def test_get_artmode_does_not_retry_timeout(device):
    device._art.get_artmode = AsyncMock(side_effect=TimeoutError)
    device._art.close = AsyncMock()
    assert await device.async_get_artmode() is None
    assert device._art.get_artmode.await_count == 1
    device._art.close.assert_awaited_once()


async def test_get_artmode_single_attempt_when_requested(hass, device):
    # attempts=1 is used while the TV reports standby (shutting down): the
    # art socket hangs until timeout there, so a retry only adds latency.
    device._art.get_artmode = AsyncMock(side_effect=OSError("hanging socket"))
    device._art.close = AsyncMock()
    assert await device.async_get_artmode(attempts=1) is None
    assert device._art.get_artmode.await_count == 1
    device._art.close.assert_awaited_once()


async def test_set_artmode_failure_resets_connection_and_reraises(hass, device):
    device._art.set_artmode = AsyncMock(side_effect=OSError("dead socket"))
    device._art.close = AsyncMock()
    with pytest.raises(OSError):
        await device.async_set_artmode(True)
    # One reset after the initial failure, one after the failed retry.
    assert device._art.close.await_count == 2
    assert device._art.set_artmode.await_count == 2


async def test_set_artmode_retries_once_on_stale_connection(hass, device):
    device._art.set_artmode = AsyncMock(side_effect=[OSError("stale"), None])
    device._art.close = AsyncMock()
    await device.async_set_artmode(True)
    assert device._art.set_artmode.await_count == 2
    device._art.close.assert_awaited_once()


async def test_newest_token_none_when_unchanged(hass, device):
    # All clients were constructed with the stored token ("tok").
    assert device.newest_token is None


async def test_newest_token_surfaces_library_issued_token(hass, device):
    device._art.token = "fresh-token"
    assert device.newest_token == "fresh-token"


async def test_newest_token_reads_remote(hass, device):
    device._art.token = "tok"
    device._remote.token = "fresh-token"
    assert device.newest_token == "fresh-token"


async def test_update_token_is_used_by_both_persistent_clients(hass, device):
    device.update_token("fresh-token")
    assert device._art.token == "fresh-token"
    assert device._remote.token == "fresh-token"


async def test_art_callback_and_start_are_loop_native(hass, device):
    callback = AsyncMock()
    device._art.set_event_callback = MagicMock()
    device._art.start_listening = AsyncMock()
    with patch.object(hass, "async_add_executor_job") as executor:
        device.set_art_event_callback(callback)
        await device.async_start_art_listener()
    device._art.set_event_callback.assert_called_once_with(callback)
    device._art.start_listening.assert_awaited_once()
    executor.assert_not_called()


async def test_listener_alive_delegates_to_art(device):
    device._art.is_alive = MagicMock(return_value=True)
    assert device.listener_alive is True
    device._art.is_alive.assert_called_once_with()


async def test_listener_not_restarted_after_stop(hass, device):
    """An in-flight restart finishing after unload must not resurrect a
    listener nothing will ever close."""
    device._remote.close = AsyncMock()
    device._art.close = AsyncMock()
    device._art.start_listening = AsyncMock()
    await device.async_stop()
    await device.async_restart_art_listener()
    await device.async_start_art_listener()
    device._art.start_listening.assert_not_awaited()


async def test_restart_art_listener_reopens_same_adapter(device):
    art = device._art
    art.close = AsyncMock()
    art.start_listening = AsyncMock()
    await device.async_restart_art_listener()
    assert device._art is art
    art.close.assert_awaited_once()
    art.start_listening.assert_awaited_once()


async def test_restart_does_not_start_if_stop_lands_while_closing(device):
    close_started = asyncio.Event()
    release_restart_close = asyncio.Event()

    async def close_art():
        if not close_started.is_set():
            close_started.set()
            await release_restart_close.wait()

    device._art.close = AsyncMock(side_effect=close_art)
    device._art.start_listening = AsyncMock()
    device._remote.close = AsyncMock()

    restart = asyncio.create_task(device.async_restart_art_listener())
    await close_started.wait()
    stop = asyncio.create_task(device.async_stop())
    while not device._stopped:
        await asyncio.sleep(0)
    release_restart_close.set()
    await asyncio.gather(restart, stop)

    device._art.start_listening.assert_not_awaited()


async def test_stop_is_bounded_and_idempotent(device):
    device._remote.close = AsyncMock(side_effect=[TimeoutError, None])
    device._art.close = AsyncMock(side_effect=[TimeoutError, None])
    await asyncio.wait_for(device.async_stop(), timeout=0.1)
    await asyncio.wait_for(device.async_stop(), timeout=0.1)
    assert device._stopped is True
    assert device._remote.close.await_count == 2
    assert device._art.close.await_count == 2


async def test_stop_bounds_wedged_remote_close_and_is_idempotent(device):
    never_finishes = asyncio.Event()

    async def close_remote():
        await never_finishes.wait()

    device._remote.close = AsyncMock(side_effect=close_remote)
    device._art.close = AsyncMock()

    with patch(
        "custom_components.samsungtv_frame.device.ART_CLOSE_DEADLINE",
        0.01,
        create=True,
    ):
        await asyncio.wait_for(device.async_stop(), timeout=0.1)
        await asyncio.wait_for(device.async_stop(), timeout=0.1)

    assert device._stopped is True
    assert device._remote.close.await_count == 2
    assert device._art.close.await_count == 2


async def test_send_key_clicks_remote(hass, device):
    remote = MagicMock()
    remote.send_commands = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")
    cmds = remote.send_commands.call_args.args[0]
    assert cmds[0].params["DataOfCmd"] == "KEY_HOME"
    assert cmds[0].params["Cmd"] == "Click"


async def test_rejected_remote_token_falls_back_to_tokenless(hass, device):
    """An ms.channel.timeOut reply to a token-carrying connect means the
    token is invalid for the remote channel; the client must be recreated
    without a token so the on-TV Allow prompt can render."""
    remote = MagicMock()
    remote.app_list = AsyncMock(
        side_effect=Exception("ConnectionFailure: {'event': 'ms.channel.timeOut'}")
    )
    with patch.object(device, "_remote", remote):
        assert await device.async_app_list() is None
        assert device._remote is not remote  # replaced with a fresh client
        assert device._remote.token is None
    assert device._remote_tokenless is True


async def test_other_remote_errors_keep_token(hass, device):
    remote = MagicMock()
    remote.app_list = AsyncMock(side_effect=OSError("network down"))
    with patch.object(device, "_remote", remote):
        assert await device.async_app_list() is None
    assert device._remote_tokenless is False


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


async def test_set_slideshow_delegates_to_art(hass, device):
    device._art.set_slideshow = AsyncMock()
    await device.async_set_slideshow(60, True, "MY-C0002")
    device._art.set_slideshow.assert_awaited_once_with(60, True, "MY-C0002")


async def test_upload_art_returns_content_id(hass, device):
    device._art.upload = AsyncMock(return_value="MY_F0100")
    result = await device.async_upload_art(b"bytes", "jpg", "none")
    assert result == "MY_F0100"
    device._art.upload.assert_awaited_once_with(b"bytes", "jpg", "none")


async def test_upload_is_never_retried(device):
    device._art.upload = AsyncMock(side_effect=OSError("partial"))
    device._art.close = AsyncMock()
    with pytest.raises(OSError):
        await device.async_upload_art(b"image", "jpg", "none")
    device._art.upload.assert_awaited_once()
    device._art.close.assert_awaited_once()


async def test_get_current_art_returns_content_id(hass, device):
    device._art.get_current = AsyncMock(
        return_value={"content_id": "MY_F0034", "matte_id": "none"}
    )
    assert await device.async_get_current_art() == "MY_F0034"


@pytest.mark.parametrize(
    ("getter", "legacy_request", "value"),
    [
        ("async_get_art_brightness", "get_brightness", 5),
        ("async_get_color_temperature", "get_color_temperature", 2),
    ],
)
async def test_numeric_art_getter_malformed_settings_falls_back_without_reset(
    device, getter, legacy_request, value
):
    device._art.request = AsyncMock(
        side_effect=[{"data": "not-json"}, {"value": value}]
    )
    device._art.close = AsyncMock()

    assert await getattr(device, getter)() == value
    assert device._art.request.await_args_list == [
        (("get_artmode_settings",), {}),
        ((legacy_request,), {}),
    ]
    device._art.close.assert_not_awaited()


async def test_thumbnail_d2d_failure_does_not_reset_or_retry(device, caplog):
    device._art.request = AsyncMock(
        return_value={
            "conn_info": {
                "ip": "10.0.0.8",
                "port": 4321,
                "secured": False,
            }
        }
    )
    device._art._open_d2d = AsyncMock(side_effect=OSError("D2D unavailable"))
    device._art.close = AsyncMock()

    assert await device.async_get_art_thumbnail("MY_F0001") is None
    device._art.request.assert_awaited_once()
    device._art._open_d2d.assert_awaited_once()
    device._art.close.assert_not_awaited()
    assert "thumbnail fetch failed for MY_F0001" in caplog.text


async def test_all_art_operations_stay_off_executor(hass, device):
    device._art.get_artmode = AsyncMock(return_value="off")
    device._art.set_artmode = AsyncMock()
    device._art.get_current = AsyncMock(return_value={"content_id": "MY_F0001"})
    device._art.get_brightness = AsyncMock(return_value=5)
    device._art.set_brightness = AsyncMock()
    device._art.select_image = AsyncMock()
    device._art.upload = AsyncMock(return_value="MY_F0002")
    device._art.delete = AsyncMock()
    device._art.get_thumbnail = AsyncMock(return_value=b"jpeg")
    device._art.change_matte = AsyncMock()
    device._art.set_photo_filter = AsyncMock()
    device._art.set_favourite = AsyncMock()
    device._art.get_color_temperature = AsyncMock(return_value=3)
    device._art.set_color_temperature = AsyncMock()
    device._art.set_slideshow = AsyncMock()

    with patch.object(hass, "async_add_executor_job") as executor:
        await device.async_get_artmode()
        await device.async_set_artmode(True)
        await device.async_get_current_art()
        await device.async_get_art_brightness()
        await device.async_set_art_brightness(6)
        await device.async_select_art("MY_F0001", True)
        await device.async_upload_art(b"image", "jpg", "none")
        await device.async_delete_art("MY_F0001")
        await device.async_get_art_thumbnail("MY_F0001")
        await device.async_change_matte("MY_F0001", "shadowbox_polar")
        await device.async_set_photo_filter("MY_F0001", "ink")
        await device.async_set_favourite("MY_F0001", True)
        await device.async_get_color_temperature()
        await device.async_set_color_temperature(4)
        await device.async_set_slideshow(60, False, "MY-C0002")

    executor.assert_not_called()


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
