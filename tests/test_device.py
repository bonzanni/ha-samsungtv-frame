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
