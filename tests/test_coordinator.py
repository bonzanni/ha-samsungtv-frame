# tests/test_coordinator.py
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntry

from custom_components.samsungtv_frame.coordinator import FrameCoordinator
from custom_components.samsungtv_frame.models import FrameData, TvMode


def _make(hass, device) -> FrameCoordinator:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "abc"
    entry.options = {}

    # Run real coroutines handed to the mocked background-task API on the
    # test loop (so e.g. listener restarts actually execute); anything else
    # (a patched-out method returning a MagicMock/None) is just swallowed.
    def _bg_task(_hass, coro, _name, eager_start=True):
        import asyncio

        if asyncio.iscoroutine(coro):
            return hass.async_create_task(coro)
        return MagicMock()

    entry.async_create_background_task.side_effect = _bg_task
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


async def test_art_poll_fetches_current_art_and_brightness(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0034"
    mock_device.async_get_art_brightness.return_value = 7
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    assert data.current_art == "MY_F0034"
    assert data.art_brightness == 7


async def test_art_extras_skipped_when_watching_but_cache_held(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0034"
    mock_device.async_get_art_brightness.return_value = 7
    coord = _make(hass, mock_device)
    await coord._async_update_data()

    # Switch to watching: extras are not re-fetched but the cache persists
    # (the selected artwork is still the selected artwork).
    mock_device.async_get_artmode.return_value = False
    mock_device.async_get_current_art.reset_mock()
    data = await coord._async_update_data()
    mock_device.async_get_current_art.assert_not_awaited()
    assert data.current_art == "MY_F0034"


async def test_art_extras_cleared_when_off(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0034"
    mock_device.async_get_art_brightness.return_value = 7
    coord = _make(hass, mock_device)
    await coord._async_update_data()

    mock_device.async_device_info.return_value = None
    await coord._async_update_data()
    data = await coord._async_update_data()  # past OFF debounce
    assert data.tv_mode is TvMode.OFF
    assert data.current_art is None
    assert data.art_brightness is None


async def test_image_selected_push_updates_current_art(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True, power_state="on", art_mode=True,
        tv_mode=TvMode.ART_MODE, current_art="MY_F0001", art_brightness=5,
    )
    coord._art_mode = True
    coord.handle_art_event(
        "d2d_service_message",
        {"event": "image_selected", "content_id": "MY_F0042"},
    )
    assert coord.data.current_art == "MY_F0042"
    assert coord.data.tv_mode is TvMode.ART_MODE  # mode untouched
    assert coord.data.art_brightness == 5


async def test_running_app_detected_while_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False

    async def _status(app_id):
        return {"visible": app_id == "NETFLIX_ID", "running": True}

    mock_device.async_app_status.side_effect = _status
    coord = _make(hass, mock_device)
    coord.app_map = {
        "Netflix": {"appId": "NETFLIX_ID"},
        "YouTube": {"appId": "YT_ID"},
    }
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.WATCHING
    assert data.running_app == "Netflix"


async def test_running_app_none_when_no_visible_app(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_app_status.return_value = {"visible": False}
    coord = _make(hass, mock_device)
    coord.app_map = {"Netflix": {"appId": "NETFLIX_ID"}}
    data = await coord._async_update_data()
    assert data.running_app is None


async def test_running_app_not_swept_in_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    coord.app_map = {"Netflix": {"appId": "NETFLIX_ID"}}
    data = await coord._async_update_data()
    mock_device.async_app_status.assert_not_awaited()
    assert data.running_app is None


async def test_push_art_on_during_standby_shutdown_stays_off(hass, mock_device):
    """The dying art socket can push 'on' events during shutdown; with the
    learned trait and PowerState standby, OFF must win on the push path too."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    await coord._async_update_data()  # learn art+power-on trait

    # Shutdown begins: poll sees standby => OFF.
    mock_device.async_device_info.return_value = {"PowerState": "standby"}
    coord.data = await coord._async_update_data()
    assert coord.data.tv_mode is TvMode.OFF

    with patch.object(coord, "async_set_updated_data") as push:
        coord.handle_art_event(
            "d2d_service_message", {"event": "art_mode_changed", "value": "on"}
        )
        pushed = push.call_args.args[0]
    assert pushed.tv_mode is TvMode.OFF


async def test_dead_listener_thread_triggers_restart(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.listener_alive = False
    coord = _make(hass, mock_device)
    coord.restart_listener = AsyncMock()
    await coord._async_update_data()
    await hass.async_block_till_done()
    coord.restart_listener.assert_awaited_once()


async def test_listener_restart_deduped_while_in_flight(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.restart_listener = AsyncMock()
    coord._listener_task = MagicMock()
    coord._listener_task.done.return_value = False
    coord._async_kick_listener_restart()
    coord.config_entry.async_create_background_task.assert_not_called()


async def test_persistent_art_failure_becomes_unknown(hass, mock_device):
    """After N consecutive failed art queries the last-stable hold must end."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    coord.data = await coord._async_update_data()
    assert coord.data.tv_mode is TvMode.ART_MODE

    from custom_components.samsungtv_frame.const import ART_FAIL_UNKNOWN_COUNT

    mock_device.async_get_artmode.return_value = None  # art channel dead
    for _ in range(ART_FAIL_UNKNOWN_COUNT - 1):
        coord.data = await coord._async_update_data()
        assert coord.data.tv_mode is TvMode.ART_MODE  # held while transient
    coord.data = await coord._async_update_data()
    assert coord.data.tv_mode is TvMode.UNKNOWN  # bounded: surfaces as unknown


async def test_app_fetch_retries_after_failure(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_app_list.return_value = None  # booting TV: fetch fails
    coord = _make(hass, mock_device)
    await coord._async_update_data()
    await hass.async_block_till_done()
    assert coord.app_map is None

    mock_device.async_app_list.return_value = [
        {"name": "Netflix", "appId": "X", "app_type": 2}
    ]
    # Attempts are spaced APP_FETCH_POLL_SPACING polls apart.
    from custom_components.samsungtv_frame.const import APP_FETCH_POLL_SPACING

    for _ in range(APP_FETCH_POLL_SPACING):
        await coord._async_update_data()
    await hass.async_block_till_done()
    assert coord.app_map == {"Netflix": {"name": "Netflix", "appId": "X", "app_type": 2}}


async def test_volume_polled_when_powered_on(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    mock_device.async_get_volume.return_value = (0.15, True)
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    assert data.volume_level == 0.15
    assert data.is_muted is True


async def test_volume_not_polled_when_unreachable(hass, mock_device):
    mock_device.async_device_info.return_value = None
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    mock_device.async_get_volume.assert_not_awaited()
    assert data.volume_level is None


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
