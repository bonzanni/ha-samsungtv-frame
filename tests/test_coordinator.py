# tests/test_coordinator.py
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntry

from custom_components.samsungtv_frame.coordinator import FrameCoordinator
from custom_components.samsungtv_frame.models import FrameData, TvMode


def _make(hass, device) -> FrameCoordinator:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "abc"
    entry.options = {}

    # Close coroutines handed to the mocked background-task API so they don't
    # emit "never awaited" warnings; tests drive them directly instead.
    def _swallow_task(_hass, coro, _name, eager_start=True):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    entry.async_create_background_task.side_effect = _swallow_task
    return FrameCoordinator(hass, entry, device)


async def test_update_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.WATCHING


async def test_update_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.ART_MODE


async def test_off_requires_debounce(hass, mock_device):
    mock_device.async_device_info.return_value = None  # unreachable
    coord = _make(hass, mock_device)
    coord.data = MagicMock(tv_mode=TvMode.WATCHING)  # last stable
    # First unreachable poll -> hold last stable, not OFF yet
    first = await coord._async_update_data()
    assert first.tv_mode is TvMode.WATCHING
    # Second consecutive unreachable -> OFF
    second = await coord._async_update_data()
    assert second.tv_mode is TvMode.OFF


async def test_art_event_enters_art_mode(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=False,
        tv_mode=TvMode.WATCHING,
        current_art=None,
    )
    coord.handle_art_event("d2d_service_message", {"event": "art_mode_changed", "value": "on"})
    assert coord.data.tv_mode is TvMode.ART_MODE
    assert coord.data.art_mode is True


async def test_art_event_reads_status_key(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=False,
        tv_mode=TvMode.WATCHING,
        current_art=None,
    )
    coord.handle_art_event("d2d_service_message", {"event": "artmode_status", "status": "on"})
    assert coord.data.tv_mode is TvMode.ART_MODE


async def test_art_event_unknown_subevent_no_push(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=False,
        tv_mode=TvMode.WATCHING,
        current_art=None,
    )
    with patch.object(coord, "async_set_updated_data") as push:
        coord.handle_art_event("d2d_service_message", {"event": "some_other_event"})
        push.assert_not_called()


async def test_off_resets_art_mode(hass, mock_device):
    # First poll: TV is on and in art mode.
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    first = await coord._async_update_data()
    assert first.tv_mode is TvMode.ART_MODE
    assert first.art_mode is True

    # Now simulate TV going unreachable (e.g. powered off).
    mock_device.async_device_info.return_value = None
    # First unreachable poll is held at last-stable by debounce.
    await coord._async_update_data()
    # Second unreachable poll crosses the OFF_DEBOUNCE_COUNT threshold.
    final = await coord._async_update_data()
    assert final.tv_mode is TvMode.OFF
    assert final.art_mode is False


async def test_reachable_edge_triggers_listener_restart(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.restart_listener = AsyncMock()

    # First poll: unreachable.
    mock_device.async_device_info.return_value = None
    await coord._async_update_data()

    # Second poll: reachable again -> crosses the unreachable->reachable edge.
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await coord._async_update_data()
    await hass.async_block_till_done()

    coord.restart_listener.assert_awaited_once()


async def test_reachable_to_reachable_does_not_restart_listener(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.restart_listener = AsyncMock()

    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await coord._async_update_data()
    await coord._async_update_data()
    await hass.async_block_till_done()

    coord.restart_listener.assert_not_awaited()


async def test_standby_wins_after_art_with_power_on_seen(hass, mock_device):
    """2022-24 Frames: art mode runs with PowerState 'on', so once that has
    been observed, standby + art-still-answering must mean shutdown => OFF
    in a single poll (the dying art socket answers 'on' for ~50 s)."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    first = await coord._async_update_data()
    assert first.tv_mode is TvMode.ART_MODE

    # Power-off: PowerState flips to standby, art socket still answers "on".
    mock_device.async_device_info.return_value = {"PowerState": "standby"}
    second = await coord._async_update_data()
    assert second.tv_mode is TvMode.OFF
    assert second.art_mode is False


async def test_standby_holds_art_when_trait_not_learned(hass, mock_device):
    """2025 Frames (#185) report standby during normal art mode; without the
    learned trait the art gate must keep winning."""
    mock_device.async_device_info.return_value = {"PowerState": "standby"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.ART_MODE


async def test_heartbeat_option_sets_update_interval(hass, mock_device):
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "abc"
    entry.options = {"heartbeat_seconds": 30}
    coord = FrameCoordinator(hass, entry, mock_device)
    assert coord.update_interval.total_seconds() == 30


async def test_poll_captures_newly_issued_token(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.newest_token = "fresh-token"
    coord = _make(hass, mock_device)
    coord.config_entry.data = {"host": "1.2.3.4", "token": None}
    with patch.object(hass.config_entries, "async_update_entry") as update:
        await coord._async_update_data()
    mock_device.update_token.assert_called_once_with("fresh-token")
    update.assert_called_once()
    assert update.call_args.kwargs["data"]["token"] == "fresh-token"


async def test_poll_no_token_update_when_none_issued(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    coord = _make(hass, mock_device)
    with patch.object(hass.config_entries, "async_update_entry") as update:
        await coord._async_update_data()
    update.assert_not_called()


async def test_wake_probe_refreshes_when_port_opens(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=False, power_state=None, art_mode=None,
        tv_mode=TvMode.OFF, current_art=None,
    )

    def _resolve():
        coord.data = FrameData(
            reachable=True, power_state="on", art_mode=True,
            tv_mode=TvMode.ART_MODE, current_art=None,
        )

    with (
        patch.object(
            coord, "_async_probe_port", AsyncMock(side_effect=[False, True])
        ) as probe,
        patch.object(
            coord, "async_request_refresh", AsyncMock(side_effect=_resolve)
        ) as refresh,
        patch("custom_components.samsungtv_frame.coordinator.WAKE_PROBE_DELAY", 0),
    ):
        await coord._wake_probe()

    assert probe.await_count == 2  # port closed while booting, then open
    refresh.assert_awaited_once()


async def test_wake_probe_exits_immediately_when_not_off(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True, power_state="on", art_mode=False,
        tv_mode=TvMode.WATCHING, current_art=None,
    )
    with patch.object(coord, "_async_probe_port", AsyncMock()) as probe:
        await coord._wake_probe()
    probe.assert_not_awaited()


async def test_notify_turn_on_spawns_probe_once(hass, mock_device):
    coord = _make(hass, mock_device)
    with patch.object(coord, "_wake_probe", MagicMock(return_value=None)):
        coord.async_notify_turn_on()
        coord.config_entry.async_create_background_task.assert_called_once()
        # While the first probe is still running, another turn_on is a no-op.
        coord._wake_task = MagicMock()
        coord._wake_task.done.return_value = False
        coord.async_notify_turn_on()
        coord.config_entry.async_create_background_task.assert_called_once()


async def test_art_event_go_to_standby_holds_but_refreshes(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=True,
        tv_mode=TvMode.ART_MODE,
        current_art=None,
    )
    with (
        patch.object(coord, "async_set_updated_data") as push,
        patch.object(coord, "async_request_refresh", AsyncMock()) as refresh,
    ):
        coord.handle_art_event("d2d_service_message", {"event": "go_to_standby"})
        # Destination is ambiguous -> never a state change by itself...
        push.assert_not_called()
        await hass.async_block_till_done()
        # ...but it must trigger an immediate poll to resolve OFF fast.
        refresh.assert_awaited_once()


async def test_poll_skips_art_retry_when_standby(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "standby"}
    coord = _make(hass, mock_device)
    await coord._async_update_data()
    assert mock_device.async_get_artmode.call_args.kwargs["attempts"] == 1


async def test_poll_uses_art_retry_when_on(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    await coord._async_update_data()
    assert mock_device.async_get_artmode.call_args.kwargs["attempts"] == 2
