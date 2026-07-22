# tests/test_coordinator.py
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry, SOURCE_REAUTH
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.art_session import ArtSessionState
from custom_components.samsungtv_frame.const import (
    ART_FAIL_UNKNOWN_COUNT,
    ART_RECONCILE_SECONDS,
    CONF_HOST,
    CONF_MAC,
    CONF_TOKEN,
    DEFAULT_APP_MAP,
    DOMAIN,
)
from custom_components.samsungtv_frame.coordinator import FrameCoordinator
from custom_components.samsungtv_frame.device import FrameDevice
from custom_components.samsungtv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    FrameData,
    SlideshowMode,
    SlideshowState,
    TvMode,
)


SETTINGS = ArtSettingsSnapshot(
    supported=frozenset(
        {ArtSettingKey.BRIGHTNESS, ArtSettingKey.COLOR_TEMPERATURE}
    ),
    brightness=5,
    color_temperature=3,
)
SLIDESHOW = SlideshowState(
    mode=SlideshowMode.SEQUENTIAL,
    duration_minutes=15,
    category_id="MY-CATEGORY",
)
SETTINGS_ONE = ArtSettingsSnapshot(
    supported=frozenset({ArtSettingKey.BRIGHTNESS}),
    brightness=4,
)
SETTINGS_TWO = ArtSettingsSnapshot(
    supported=frozenset({ArtSettingKey.BRIGHTNESS}),
    brightness=8,
)
SLIDESHOW_ONE = SlideshowState(
    mode=SlideshowMode.SEQUENTIAL,
    duration_minutes=10,
)
SLIDESHOW_TWO = SlideshowState(
    mode=SlideshowMode.SHUFFLE,
    duration_minutes=30,
    category_id="MY-SECOND-CATEGORY",
)


class FakeClock:
    """Mutable coordinator clock for reconciliation-window tests."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _make(hass, device) -> FrameCoordinator:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "abc"
    entry.options = {}

    # Run real coroutines handed to the mocked background-task API on the
    # test loop; anything else (a patched-out method returning a
    # MagicMock/None) is just swallowed.
    def _bg_task(_hass, coro, _name, eager_start=True):
        import asyncio

        if asyncio.iscoroutine(coro):
            return hass.async_create_task(coro)
        return MagicMock()

    entry.async_create_background_task.side_effect = _bg_task
    return FrameCoordinator(hass, entry, device)


def _seed_ready_art(
    mock_device,
    coord: FrameCoordinator,
    *,
    generation: int,
    now: float,
    art_mode: bool | None,
    current_art: str | None,
    brightness: int | None,
    color_temperature: int | None,
) -> FakeClock:
    """Make every input to one READY reconciliation explicit."""
    clock = FakeClock(now)
    coord._clock = clock
    mock_device.art_ready = True
    mock_device.art_generation = generation
    mock_device.async_get_artmode.return_value = art_mode
    mock_device.async_get_current_art.return_value = current_art
    supported = frozenset(
        key
        for key, value in (
            (ArtSettingKey.BRIGHTNESS, brightness),
            (ArtSettingKey.COLOR_TEMPERATURE, color_temperature),
        )
        if value is not None
    )
    mock_device.async_get_art_settings.return_value = ArtSettingsSnapshot(
        supported=supported,
        brightness=brightness,
        color_temperature=color_temperature,
    )
    mock_device.async_get_slideshow_state.return_value = SLIDESHOW
    return clock


def _reset_art_getters(mock_device) -> None:
    mock_device.async_get_artmode.reset_mock()
    mock_device.async_get_current_art.reset_mock()
    mock_device.async_get_art_settings.reset_mock()
    mock_device.async_get_slideshow_state.reset_mock()


def _assert_art_getter_count(mock_device, expected: int) -> None:
    assert mock_device.async_get_artmode.await_count == expected
    assert mock_device.async_get_current_art.await_count == expected
    assert mock_device.async_get_art_settings.await_count == expected
    assert mock_device.async_get_slideshow_state.await_count == expected


async def test_update_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.WATCHING


async def test_update_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
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
    coord.handle_art_event(
        "d2d_service_message",
        {"event": "art_mode_changed", "value": "on"},
    )
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
    coord.handle_art_event(
        "d2d_service_message",
        {"event": "artmode_status", "status": "on"},
    )
    assert coord.data.tv_mode is TvMode.ART_MODE


async def test_art_event_unknown_subevent_no_push(hass, mock_device, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.samsungtv_frame")
    private_value = "private-art-payload"
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=False,
        tv_mode=TvMode.WATCHING,
        current_art=None,
    )
    with patch.object(coord, "async_set_updated_data") as push:
        coord.handle_art_event(
            "d2d_service_message",
            {"event": "some_other_event", "private": private_value},
        )
        push.assert_not_called()
    assert private_value not in caplog.text
    assert "Art event received" in caplog.text


async def test_off_resets_art_mode(hass, mock_device):
    # First poll: TV is on and in art mode.
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
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


async def test_healthy_heartbeats_use_cached_art_without_art_io(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    clock = _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )

    await coord._async_update_data()
    for _ in range(6):
        clock.now += 10.0
        await coord._async_update_data()

    assert mock_device.async_get_artmode.await_count == 1
    assert mock_device.async_get_current_art.await_count == 1
    assert mock_device.async_get_art_settings.await_count == 1
    assert mock_device.async_get_slideshow_state.await_count == 1


async def test_new_ready_generation_reconciles_once(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    clock = _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )

    await coord._async_update_data()
    clock.now = 10.0
    mock_device.art_generation = 2
    await coord._async_update_data()
    clock.now = 20.0
    await coord._async_update_data()

    _assert_art_getter_count(mock_device, 2)


async def test_reconcile_is_spaced_300_seconds(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    clock = _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )

    await coord._async_update_data()
    clock.now = 299.9
    await coord._async_update_data()
    _assert_art_getter_count(mock_device, 1)

    clock.now = 300.0
    await coord._async_update_data()
    _assert_art_getter_count(mock_device, 2)


async def test_two_back_to_back_art_reconciles_bypass_request_debounce(
    hass, mock_device
):
    mock_device.art_ready = True
    mock_device.art_generation = 1
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_art_settings.side_effect = [
        SETTINGS_ONE,
        SETTINGS_TWO,
    ]
    mock_device.async_get_slideshow_state.side_effect = [
        SLIDESHOW_ONE,
        SLIDESHOW_TWO,
    ]
    coord = _make(hass, mock_device)
    coord._clock = lambda: 0.0

    await coord.async_request_art_reconcile()
    await coord.async_request_art_reconcile()

    assert mock_device.async_get_art_settings.await_count == 2
    assert mock_device.async_get_slideshow_state.await_count == 2
    assert coord.data.art_settings is SETTINGS_TWO
    assert coord.data.slideshow is SLIDESHOW_TWO
    assert coord._next_art_reconcile == ART_RECONCILE_SECONDS


async def test_reconcile_never_runs_when_session_not_ready(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=2,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    mock_device.art_ready = False

    await coord._async_update_data()

    _assert_art_getter_count(mock_device, 0)


async def test_dead_listener_observation_does_not_parallel_art_query(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=0,
        now=0.0,
        art_mode=False,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )
    mock_device.art_ready = False

    await coord._async_update_data()

    mock_device.observe_art_power.assert_called_once_with(
        True, "on", False
    )
    _assert_art_getter_count(mock_device, 0)


async def test_reachable_edge_delegates_one_power_trigger(
    hass, mock_device
):
    coord = _make(hass, mock_device)

    # First poll: unreachable.
    mock_device.async_device_info.return_value = None
    await coord._async_update_data()
    mock_device.observe_art_power.reset_mock()

    # Second poll: reachable again -> one synchronous session observation.
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    await coord._async_update_data()

    mock_device.observe_art_power.assert_called_once_with(
        True, "on", True
    )


def test_temporary_listener_restart_owner_is_removed(hass, mock_device):
    coord = _make(hass, mock_device)

    assert not hasattr(coord, "restart_listener")
    assert not hasattr(coord, "_listener_task")
    assert not hasattr(coord, "_async_kick_listener_restart")
    assert not hasattr(coord, "_restart_listener_safe")


async def test_standby_wins_after_art_with_power_on_seen(hass, mock_device):
    """2022-24 Frames: art mode runs with PowerState 'on', so once that has
    been observed, standby + art-still-answering must mean shutdown => OFF
    in a single poll (the dying art socket answers 'on' for ~50 s)."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
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
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.ART_MODE


async def test_reconcile_reads_one_settings_snapshot_and_one_slideshow(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.art_ready = True
    mock_device.art_generation = 4
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0034"
    mock_device.async_get_art_settings.return_value = SETTINGS
    mock_device.async_get_slideshow_state.return_value = SLIDESHOW
    coord = _make(hass, mock_device)
    data = await coord._async_poll()

    assert data.current_art == "MY_F0034"
    assert data.art_settings is SETTINGS
    assert data.slideshow is SLIDESHOW
    assert data.optional_art_generation == 4
    assert data.art_brightness == SETTINGS.brightness
    assert data.art_color_temperature == SETTINGS.color_temperature
    mock_device.async_get_current_art.assert_awaited_once()
    mock_device.async_get_art_settings.assert_awaited_once()
    mock_device.async_get_slideshow_state.assert_awaited_once()
    mock_device.async_get_art_brightness.assert_not_awaited()
    mock_device.async_get_color_temperature.assert_not_awaited()


async def test_art_extras_skipped_when_watching_but_cache_held(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    clock = _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0034",
        brightness=7,
        color_temperature=2,
    )
    await coord._async_update_data()

    # A live push switches to watching while the selected-art cache persists.
    coord.handle_art_event(
        "d2d_service_message",
        {"event": "art_mode_changed", "value": "off"},
    )
    _reset_art_getters(mock_device)
    clock.now = 10.0
    data = await coord._async_update_data()
    _assert_art_getter_count(mock_device, 0)
    assert data.tv_mode is TvMode.WATCHING
    assert data.current_art == "MY_F0034"


async def test_off_hides_all_art_snapshots_and_optional_generation(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0034",
        brightness=7,
        color_temperature=2,
    )
    first = await coord._async_update_data()
    assert first.art_settings is not None
    assert first.slideshow is SLIDESHOW
    assert first.optional_art_generation == 1

    mock_device.async_device_info.return_value = None
    await coord._async_update_data()
    data = await coord._async_update_data()  # past OFF debounce
    assert data.tv_mode is TvMode.OFF
    assert data.current_art is None
    assert data.art_brightness is None
    assert data.art_color_temperature is None
    assert data.art_settings is None
    assert data.slideshow is None
    assert data.optional_art_generation is None


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


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "art_mode_changed", "value": "on"},
        {"event": "image_selected", "content_id": "MY_F0042"},
        {
            "event": "slideshow_image_changed",
            "content_id": "MY_F0042",
        },
    ],
    ids=[
        "art_mode_changed",
        "image_selected",
        "slideshow_image_changed",
    ],
)
async def test_named_push_events_preserve_optional_snapshots_by_identity(
    hass, mock_device, payload
):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=False,
        tv_mode=TvMode.WATCHING,
        current_art="MY_F0001",
        art_brightness=SETTINGS.brightness,
        art_color_temperature=SETTINGS.color_temperature,
        art_settings=SETTINGS,
        slideshow=SLIDESHOW,
        optional_art_generation=4,
    )
    coord._art_mode = False

    coord.handle_art_event("d2d_service_message", payload)

    assert coord.data.art_settings is SETTINGS
    assert coord.data.slideshow is SLIDESHOW
    assert coord.data.optional_art_generation == 4


async def test_session_ready_callback_schedules_one_refresh(
    hass, mock_device
):
    coord = _make(hass, mock_device)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def _blocked_refresh():
        refresh_started.set()
        await release_refresh.wait()

    with patch.object(
        coord,
        "async_request_refresh",
        AsyncMock(side_effect=_blocked_refresh),
    ) as refresh:
        try:
            coord.handle_art_session_state(ArtSessionState.READY)
            coord.handle_art_session_state(ArtSessionState.READY)
            await asyncio.wait_for(refresh_started.wait(), timeout=0.1)

            assert refresh.await_count == 1
            assert (
                coord.config_entry.async_create_background_task.call_count
                == 1
            )
            assert (
                coord.config_entry.async_create_background_task.call_args.args[2]
                == "samsungtv_frame-art-ready-refresh"
            )
        finally:
            release_refresh.set()
            await hass.async_block_till_done(wait_background_tasks=True)


async def test_ready_transition_exposes_generation_mismatch_while_refresh_blocked(
    hass, mock_device
):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=True,
        tv_mode=TvMode.ART_MODE,
        current_art="MY_F0001",
        art_brightness=SETTINGS_ONE.brightness,
        art_settings=SETTINGS_ONE,
        slideshow=SLIDESHOW_ONE,
        optional_art_generation=1,
    )
    mock_device.art_ready = True
    mock_device.art_generation = 2
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def _blocked_refresh():
        refresh_started.set()
        await release_refresh.wait()

    with patch.object(
        coord,
        "async_request_refresh",
        AsyncMock(side_effect=_blocked_refresh),
    ):
        try:
            coord.handle_art_session_state(ArtSessionState.READY)
            await asyncio.wait_for(refresh_started.wait(), timeout=0.1)

            assert coord.data.art_settings is SETTINGS_ONE
            assert coord.data.slideshow is SLIDESHOW_ONE
            assert (
                coord.data.optional_art_generation
                != mock_device.art_generation
            )
        finally:
            release_refresh.set()
            await hass.async_block_till_done(wait_background_tasks=True)


async def test_push_updates_cache_between_reconciliations(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    clock = _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    coord.data = await coord._async_update_data()
    _reset_art_getters(mock_device)

    coord.handle_art_event(
        "d2d_service_message",
        {"event": "art_mode_changed", "value": "on"},
    )
    coord.handle_art_event(
        "d2d_service_message",
        {"event": "image_selected", "content_id": "MY_F0099"},
    )
    clock.now = 10.0
    data = await coord._async_update_data()

    assert data.tv_mode is TvMode.ART_MODE
    assert data.art_mode is True
    assert data.current_art == "MY_F0099"
    _assert_art_getter_count(mock_device, 0)


async def test_reconcile_reads_current_art_settings_and_slideshow_sequentially(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    order: list[str] = []
    mode_started = asyncio.Event()
    current_started = asyncio.Event()
    settings_started = asyncio.Event()
    slideshow_started = asyncio.Event()
    release_mode = asyncio.Event()
    release_current = asyncio.Event()
    release_settings = asyncio.Event()
    release_slideshow = asyncio.Event()

    async def _blocked(
        name: str,
        started: asyncio.Event,
        release: asyncio.Event,
        value,
    ):
        order.append(f"{name}:start")
        started.set()
        await release.wait()
        order.append(f"{name}:end")
        return value

    async def _mode(*_args, **_kwargs):
        return await _blocked("mode", mode_started, release_mode, True)

    async def _current_art():
        return await _blocked(
            "current", current_started, release_current, "MY_F0001"
        )

    async def _settings():
        return await _blocked(
            "settings", settings_started, release_settings, SETTINGS
        )

    async def _slideshow():
        return await _blocked(
            "slideshow",
            slideshow_started,
            release_slideshow,
            SLIDESHOW,
        )

    mock_device.async_get_artmode.side_effect = _mode
    mock_device.async_get_current_art.side_effect = _current_art
    mock_device.async_get_art_settings.side_effect = _settings
    mock_device.async_get_slideshow_state.side_effect = _slideshow
    data = None
    poll_task = asyncio.create_task(coord._async_update_data())
    releases = (
        release_mode,
        release_current,
        release_settings,
        release_slideshow,
    )
    try:
        await asyncio.wait_for(mode_started.wait(), timeout=0.1)
        assert not current_started.is_set()
        assert not settings_started.is_set()
        assert not slideshow_started.is_set()

        release_mode.set()
        await asyncio.wait_for(current_started.wait(), timeout=0.1)
        assert not settings_started.is_set()
        assert not slideshow_started.is_set()

        release_current.set()
        await asyncio.wait_for(settings_started.wait(), timeout=0.1)
        assert not slideshow_started.is_set()

        release_settings.set()
        await asyncio.wait_for(slideshow_started.wait(), timeout=0.1)
        release_slideshow.set()
        data = await asyncio.wait_for(poll_task, timeout=0.1)
    finally:
        for release in releases:
            release.set()
        if not poll_task.done():
            poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)

    assert data is not None
    assert order == [
        "mode:start",
        "mode:end",
        "current:start",
        "current:end",
        "settings:start",
        "settings:end",
        "slideshow:start",
        "slideshow:end",
    ]
    _assert_art_getter_count(mock_device, 1)
    assert data.art_settings is SETTINGS
    assert data.slideshow is SLIDESHOW


async def test_reconcile_does_not_overwrite_newer_push(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art="STALE_ART",
        brightness=4,
        color_temperature=2,
    )
    mode_started = asyncio.Event()
    release_mode = asyncio.Event()

    async def _stale_mode(*_args, **_kwargs):
        mode_started.set()
        await release_mode.wait()
        return None

    mock_device.async_get_artmode.side_effect = _stale_mode
    coord._art_fail_streak = 2
    data = None
    poll_task = asyncio.create_task(coord._async_update_data())
    try:
        await asyncio.wait_for(mode_started.wait(), timeout=0.1)
        mock_device.async_get_current_art.assert_not_awaited()
        mock_device.async_get_art_settings.assert_not_awaited()
        mock_device.async_get_slideshow_state.assert_not_awaited()
        coord.handle_art_event(
            "d2d_service_message",
            {"event": "art_mode_changed", "value": "on"},
        )
        coord.handle_art_event(
            "d2d_service_message",
            {"event": "image_selected", "content_id": "PUSHED_ART"},
        )
        release_mode.set()
        data = await asyncio.wait_for(poll_task, timeout=0.1)
    finally:
        release_mode.set()
        await asyncio.gather(poll_task, return_exceptions=True)

    assert data is not None
    assert data.art_mode is True
    assert data.current_art == "PUSHED_ART"
    assert coord._art_mode is True
    assert coord._current_art == "PUSHED_ART"
    assert coord._art_fail_streak == 0
    _assert_art_getter_count(mock_device, 1)


async def test_current_art_ready_loss_hides_optional_cache_but_live_none_is_current(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    coord.data = await coord._async_update_data()
    initial_settings = coord.data.art_settings
    initial_slideshow = coord.data.slideshow
    assert initial_settings is not None
    assert initial_slideshow is SLIDESHOW

    async def _lose_ready_during_current_art():
        mock_device.art_ready = False
        return None

    mock_device.art_generation = 2
    mock_device.async_get_current_art.side_effect = (
        _lose_ready_during_current_art
    )
    after_loss = await coord._async_update_data()
    calls_after_loss = (
        mock_device.async_get_artmode.await_count,
        mock_device.async_get_current_art.await_count,
        mock_device.async_get_art_settings.await_count,
        mock_device.async_get_slideshow_state.await_count,
    )

    mock_device.art_ready = True
    mock_device.art_generation = 3
    mock_device.async_get_current_art.side_effect = None
    mock_device.async_get_current_art.return_value = None
    mock_device.async_get_art_settings.return_value = None
    mock_device.async_get_slideshow_state.return_value = None
    after_live_none = await coord._async_update_data()

    assert after_loss.current_art == "MY_F0001"
    assert after_loss.art_brightness is None
    assert after_loss.art_color_temperature is None
    assert after_loss.art_settings is None
    assert after_loss.slideshow is None
    assert after_loss.optional_art_generation is None
    assert calls_after_loss == (2, 2, 1, 1)
    assert (
        after_live_none.current_art,
        after_live_none.art_brightness,
        after_live_none.art_color_temperature,
    ) == (None, None, None)
    assert after_live_none.art_settings is None
    assert after_live_none.slideshow is None
    assert after_live_none.optional_art_generation == 3


@pytest.mark.parametrize(
    "loss_point",
    ["settings", "slideshow"],
    ids=["after-settings-await", "after-slideshow-await"],
)
async def test_generation_loss_discards_both_optional_snapshots(
    hass, mock_device, loss_point
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=SETTINGS_ONE.brightness,
        color_temperature=SETTINGS_ONE.color_temperature,
    )
    coord.data = await coord._async_update_data()
    initial_settings = coord.data.art_settings
    initial_slideshow = coord.data.slideshow
    assert initial_settings is not None
    assert initial_slideshow is SLIDESHOW
    assert coord.data.optional_art_generation == 1
    _reset_art_getters(mock_device)

    mock_device.art_generation = 2
    mock_device.async_get_current_art.return_value = "MY_F0002"

    async def _settings():
        if loss_point == "settings":
            mock_device.art_generation = 3
        return SETTINGS_TWO

    async def _slideshow():
        if loss_point == "slideshow":
            mock_device.art_generation = 3
        return SLIDESHOW_TWO

    mock_device.async_get_art_settings.side_effect = _settings
    mock_device.async_get_slideshow_state.side_effect = _slideshow

    data = await coord._async_update_data()

    assert data.tv_mode is TvMode.ART_MODE
    assert data.art_brightness is None
    assert data.art_color_temperature is None
    assert data.art_settings is None
    assert data.slideshow is None
    assert data.optional_art_generation is None
    assert coord._art_settings is initial_settings
    assert coord._slideshow is initial_slideshow
    assert coord._optional_art_generation == 1
    assert mock_device.async_get_artmode.await_count == 1
    assert mock_device.async_get_current_art.await_count == 1
    assert mock_device.async_get_art_settings.await_count == 1
    expected_slideshow_reads = 0 if loss_point == "settings" else 1
    assert (
        mock_device.async_get_slideshow_state.await_count
        == expected_slideshow_reads
    )


@pytest.mark.parametrize(
    ("settings", "slideshow"),
    [
        (ArtSettingsSnapshot(), SLIDESHOW),
        (SETTINGS, None),
    ],
    ids=["unsupported-settings", "malformed-slideshow"],
)
async def test_unknown_optional_results_do_not_change_tv_mode(
    hass, mock_device, settings, slideshow
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.art_ready = True
    mock_device.art_generation = 4
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0001"
    mock_device.async_get_art_settings.return_value = settings
    mock_device.async_get_slideshow_state.return_value = slideshow
    coord = _make(hass, mock_device)

    data = await coord._async_poll()

    assert data.tv_mode is TvMode.ART_MODE
    assert data.art_settings is settings
    assert data.slideshow is slideshow
    assert data.optional_art_generation == 4
    assert coord._art_fail_streak == 0
    mock_device.async_get_art_settings.assert_awaited_once()
    mock_device.async_get_slideshow_state.assert_awaited_once()


async def test_running_app_detected_while_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}

    async def _status(app_id):
        return {"visible": app_id == "NETFLIX_ID", "running": True}

    mock_device.async_app_status.side_effect = _status
    coord = _make(hass, mock_device)
    coord.app_map = {
        "Netflix": {"appId": "NETFLIX_ID"},
        "YouTube": {"appId": "YT_ID"},
    }
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.WATCHING
    assert data.running_app == "Netflix"


async def test_running_app_none_when_no_visible_app(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_app_status.return_value = {"visible": False}
    coord = _make(hass, mock_device)
    coord.app_map = {"Netflix": {"appId": "NETFLIX_ID"}}
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )
    data = await coord._async_update_data()
    assert data.running_app is None


async def test_running_app_not_swept_in_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    coord.app_map = {"Netflix": {"appId": "NETFLIX_ID"}}
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    data = await coord._async_update_data()
    mock_device.async_app_status.assert_not_awaited()
    assert data.running_app is None


async def test_push_art_on_then_learned_standby_is_off(hass, mock_device):
    """A live Art-on push teaches that this model uses standby for shutdown."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    clock = _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    coord.data = await coord._async_update_data()
    _reset_art_getters(mock_device)

    coord.handle_art_event(
        "d2d_service_message",
        {"event": "art_mode_changed", "value": "on"},
    )
    assert coord.data.tv_mode is TvMode.ART_MODE

    # Even a new READY generation must not reconcile during learned shutdown.
    mock_device.art_generation = 2
    clock.now = 10.0
    mock_device.async_device_info.return_value = {"PowerState": "standby"}
    coord.data = await coord._async_update_data()

    assert coord.data.tv_mode is TvMode.OFF
    assert coord.data.art_mode is False
    mock_device.observe_art_power.assert_called_with(True, None, False)
    _assert_art_getter_count(mock_device, 0)

    coord.handle_art_event(
        "d2d_service_message",
        {"event": "art_mode_changed", "value": "on"},
    )

    assert coord.data.tv_mode is TvMode.OFF
    _assert_art_getter_count(mock_device, 0)


async def test_reachable_logical_off_invalidates_remote_confirmation(
    hass, mock_device
):
    """A REST-reachable standby shutdown is still a remote power boundary."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.remote_confirmed = True
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    coord.data = await coord._async_update_data()
    assert coord.data.tv_mode is TvMode.ART_MODE

    mock_device.async_device_info.return_value = {"PowerState": "standby"}
    coord.data = await coord._async_update_data()

    assert coord.data.tv_mode is TvMode.OFF
    assert mock_device.remote_confirmed is False


async def test_unavailable_session_becomes_unknown_without_network_attempts(
    hass, mock_device
):
    """Unavailable READY state ages the cache without Art reads or opens."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.remote_confirmed = False
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    coord.data = await coord._async_update_data()
    assert coord.data.tv_mode is TvMode.ART_MODE
    mock_device.art_ready = False
    _reset_art_getters(mock_device)
    coord.config_entry.async_create_background_task.reset_mock()

    for _ in range(ART_FAIL_UNKNOWN_COUNT - 1):
        coord.data = await coord._async_update_data()
        assert coord.data.tv_mode is TvMode.ART_MODE
    coord.data = await coord._async_update_data()

    assert coord.data.tv_mode is TvMode.UNKNOWN
    assert coord._art_mode is True
    _assert_art_getter_count(mock_device, 0)
    mock_device.async_start_art_session.assert_not_awaited()
    task_names = [
        task_call.args[2]
        for task_call in coord.config_entry.async_create_background_task.call_args_list
    ]
    assert "samsungtv_frame-listener-restart" not in task_names


async def test_failed_due_reconcile_counts_once(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=None,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )

    async def _failed_mode(*_args, **_kwargs):
        mock_device.art_ready = False
        return None

    mock_device.async_get_artmode.side_effect = _failed_mode
    await coord._async_update_data()

    assert coord._art_fail_streak == 1
    assert mock_device.async_get_artmode.await_count == 1
    assert mock_device.async_get_current_art.await_count == 0
    assert mock_device.async_get_art_settings.await_count == 0
    assert mock_device.async_get_slideshow_state.await_count == 0

    mock_device.async_get_artmode.side_effect = None
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0002"
    mock_device.async_get_art_settings.return_value = ArtSettingsSnapshot(
        supported=frozenset(
            {ArtSettingKey.BRIGHTNESS, ArtSettingKey.COLOR_TEMPERATURE}
        ),
        brightness=6,
        color_temperature=4,
    )
    mock_device.async_get_slideshow_state.return_value = SLIDESHOW_TWO
    mock_device.art_ready = True
    mock_device.art_generation = 2
    coord.data = await coord._async_update_data()

    assert coord._art_fail_streak == 0
    assert coord.data.tv_mode is TvMode.ART_MODE
    assert mock_device.async_get_artmode.await_count == 2
    assert mock_device.async_get_current_art.await_count == 1
    assert mock_device.async_get_art_settings.await_count == 1
    assert mock_device.async_get_slideshow_state.await_count == 1


async def test_reachable_edge_and_off_reset_failure_episode(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.remote_confirmed = False
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    coord.data = await coord._async_update_data()
    mock_device.art_ready = False
    _reset_art_getters(mock_device)

    async def _assert_exact_failure_budget():
        for expected in range(1, ART_FAIL_UNKNOWN_COUNT):
            coord.data = await coord._async_update_data()
            assert coord._art_fail_streak == expected
            assert coord.data.tv_mode is TvMode.ART_MODE
        coord.data = await coord._async_update_data()
        assert coord._art_fail_streak == ART_FAIL_UNKNOWN_COUNT
        assert coord.data.tv_mode is TvMode.UNKNOWN

    await _assert_exact_failure_budget()

    mock_device.async_device_info.return_value = None
    coord.data = await coord._async_update_data()
    assert coord._art_fail_streak == 0
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord.data = await coord._async_update_data()
    assert coord._art_fail_streak == 0
    assert coord.data.tv_mode is TvMode.ART_MODE
    await _assert_exact_failure_budget()

    mock_device.async_device_info.return_value = {"PowerState": "standby"}
    coord.data = await coord._async_update_data()
    assert coord._art_fail_streak == 0
    assert coord.data.tv_mode is TvMode.OFF
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    await _assert_exact_failure_budget()
    _assert_art_getter_count(mock_device, 0)


@pytest.mark.parametrize(
    "push_payload",
    [
        {"event": "image_selected", "content_id": "MY_F0099"},
        {"event": "art_mode_changed", "value": "on"},
        {"event": "artmode_status", "status": "on"},
    ],
    ids=["image-selected", "art-mode-changed", "artmode-status"],
)
async def test_push_resets_failure_streak(
    hass, mock_device, push_payload
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    coord.data = await coord._async_update_data()
    mock_device.art_ready = False
    _reset_art_getters(mock_device)
    for _ in range(3):
        coord.data = await coord._async_update_data()
    assert coord._art_fail_streak == 3
    observation_count = mock_device.observe_art_power.call_count

    coord.handle_art_event(
        "d2d_service_message",
        push_payload,
    )

    assert coord._art_fail_streak == 0
    assert mock_device.observe_art_power.call_count == observation_count
    _assert_art_getter_count(mock_device, 0)
    mock_device.async_start_art_session.assert_not_awaited()


async def test_unreachable_ready_session_never_reconciles(
    hass, mock_device
):
    mock_device.async_device_info.return_value = None
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=300.0,
        art_mode=True,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )

    await coord._async_update_data()

    _assert_art_getter_count(mock_device, 0)


async def test_cancelled_reconcile_reserves_window(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    coord = _make(hass, mock_device)
    clock = _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art="MY_F0001",
        brightness=5,
        color_temperature=3,
    )
    mode_started = asyncio.Event()
    never_release = asyncio.Event()

    async def _blocked_mode(*_args, **_kwargs):
        mode_started.set()
        await never_release.wait()
        return False

    mock_device.async_get_artmode.side_effect = _blocked_mode
    first_poll = asyncio.create_task(coord._async_update_data())
    try:
        await asyncio.wait_for(mode_started.wait(), timeout=0.1)
        first_poll.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_poll
    finally:
        first_poll.cancel()
        await asyncio.gather(first_poll, return_exceptions=True)

    mock_device.async_get_artmode.side_effect = None
    mock_device.async_get_artmode.return_value = False
    clock.now = 10.0
    await coord._async_update_data()

    assert mock_device.async_get_artmode.await_count == 1
    assert mock_device.async_get_current_art.await_count == 0
    assert mock_device.async_get_art_settings.await_count == 0
    assert mock_device.async_get_slideshow_state.await_count == 0


async def test_confirmed_online_polls_never_schedule_app_discovery(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.remote_confirmed = True
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )
    for _ in range(12):
        coord.data = await coord._async_update_data()
    await hass.async_block_till_done()

    assert coord.app_map == DEFAULT_APP_MAP
    assert coord.app_map is not DEFAULT_APP_MAP
    assert all(
        coord.app_map[name] is not DEFAULT_APP_MAP[name]
        for name in DEFAULT_APP_MAP
    )
    mock_device.async_app_list.assert_not_awaited()
    assert all(
        call.args[2] != f"{DOMAIN}-app-list"
        for call in coord.config_entry.async_create_background_task.call_args_list
    )


async def test_repeated_power_cycles_never_schedule_app_discovery(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.remote_confirmed = True
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )

    for _ in range(4):
        mock_device.async_device_info.return_value = {"PowerState": "on"}
        mock_device.remote_confirmed = True
        coord.data = await coord._async_update_data()
        mock_device.async_device_info.return_value = None
        coord.data = await coord._async_update_data()
        mock_device.async_device_info.return_value = {"PowerState": "on"}
        coord.data = await coord._async_update_data()
        mock_device.remote_confirmed = True
        coord.data = await coord._async_update_data()
    await hass.async_block_till_done()

    assert coord.app_map == DEFAULT_APP_MAP
    mock_device.async_app_list.assert_not_awaited()
    assert all(
        call.args[2] != f"{DOMAIN}-app-list"
        for call in coord.config_entry.async_create_background_task.call_args_list
    )


async def test_volume_polled_when_powered_on(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_volume.return_value = (0.15, True)
    coord = _make(hass, mock_device)
    _seed_ready_art(
        mock_device,
        coord,
        generation=1,
        now=0.0,
        art_mode=False,
        current_art=None,
        brightness=None,
        color_temperature=None,
    )
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


async def test_reachable_heartbeat_captures_token_while_art_unavailable(
    hass, mock_device
):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.art_ready = False
    mock_device.newest_token = "fresh-token"
    coord = _make(hass, mock_device)
    coord._clock = FakeClock(0.0)
    coord.config_entry.data = {"host": "1.2.3.4", "token": None}
    with patch.object(hass.config_entries, "async_update_entry") as update:
        await coord._async_update_data()
    mock_device.update_token.assert_called_once_with("fresh-token")
    update.assert_called_once()
    assert update.call_args.kwargs["data"]["token"] == "fresh-token"
    _assert_art_getter_count(mock_device, 0)


def test_handle_remote_token_updates_entry_before_adopting_clients(
    hass, mock_device
):
    coord = _make(hass, mock_device)
    coord.config_entry.data = {CONF_TOKEN: "old-token"}
    order: list[str] = []
    mock_device.update_token.side_effect = lambda token: order.append("device")

    def _update_entry(entry, *, data):
        assert entry is coord.config_entry
        assert data[CONF_TOKEN] == "new-token"
        order.append("entry")

    with patch.object(
        hass.config_entries,
        "async_update_entry",
        side_effect=_update_entry,
    ) as update:
        coord.handle_remote_token("new-token")

    assert order == ["entry", "device"]
    mock_device.update_token.assert_called_once_with("new-token")
    update.assert_called_once()


def test_handle_remote_token_ignores_missing(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.config_entry.data = {CONF_TOKEN: "old-token"}

    with patch.object(hass.config_entries, "async_update_entry") as update:
        coord.handle_remote_token("")

    mock_device.update_token.assert_not_called()
    update.assert_not_called()


def test_handle_remote_token_adopts_stale_runtime_when_entry_matches(
    hass, mock_device
):
    coord = _make(hass, mock_device)
    coord.config_entry.data = {CONF_TOKEN: "new-token"}

    with patch.object(hass.config_entries, "async_update_entry") as update:
        coord.handle_remote_token("new-token")

    update.assert_not_called()
    mock_device.update_token.assert_called_once_with("new-token")


def test_handle_remote_token_does_not_adopt_when_entry_update_fails(
    hass, mock_device
):
    coord = _make(hass, mock_device)
    coord.config_entry.data = {CONF_TOKEN: "old-token"}
    error = RuntimeError("entry update failed")

    with (
        patch.object(
            hass.config_entries,
            "async_update_entry",
            side_effect=error,
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        coord.handle_remote_token("new-token")

    assert raised.value is error
    mock_device.update_token.assert_not_called()


async def test_remote_token_persistence_failure_retries_before_send(hass):
    device = FrameDevice(
        hass,
        host="1.2.3.4",
        mac="A0:D0:5B:86:CE:B7",
        token="old-token",
        ssl_context=MagicMock(),
        task_factory=lambda coroutine, name: asyncio.create_task(
            coroutine, name=name
        ),
    )
    device._art_session = MagicMock(async_stop=AsyncMock())
    remote = MagicMock(token="new-token")
    order: list[str] = []
    remote.open = AsyncMock(side_effect=lambda: order.append("open"))
    remote.send_commands = AsyncMock(side_effect=lambda _commands: order.append("send"))
    remote.close = AsyncMock()
    device._remote = remote
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "abc"
    entry.options = {}
    entry.data = {CONF_TOKEN: "old-token"}
    coord = FrameCoordinator(hass, entry, device)
    device.set_remote_token_callback(coord.handle_remote_token)
    update_error = RuntimeError("entry update failed")
    update_attempt = 0

    def _update_entry(updated_entry, *, data):
        nonlocal update_attempt
        assert updated_entry is entry
        update_attempt += 1
        order.append(f"persist-{update_attempt}")
        if update_attempt == 1:
            raise update_error
        entry.data = data

    original_update_token = device.update_token

    def _adopt_token(token):
        order.append("adopt")
        original_update_token(token)

    with (
        patch.object(
            hass.config_entries,
            "async_update_entry",
            side_effect=_update_entry,
        ),
        patch.object(device, "update_token", side_effect=_adopt_token),
    ):
        with pytest.raises(RuntimeError) as raised:
            await device.async_send_key("KEY_HOME")
        assert raised.value is update_error
        assert device._token == "old-token"
        assert device._art.token == "old-token"
        assert remote.token == "new-token"
        remote.send_commands.assert_not_awaited()

        await device.async_send_key("KEY_HOME")

    assert order == [
        "open",
        "persist-1",
        "open",
        "persist-2",
        "adopt",
        "send",
    ]
    assert device._token == "new-token"
    assert device._art.token == "new-token"
    assert remote.send_commands.await_count == 1


async def test_duplicate_remote_reauth_is_suppressed_by_home_assistant(
    hass, mock_device
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Frame TV",
        data={
            CONF_HOST: "1.2.3.4",
            CONF_MAC: "A0:D0:5B:86:CE:B7",
            CONF_TOKEN: "tok",
        },
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    coord = FrameCoordinator(hass, entry, mock_device)

    coord.handle_remote_reauth()
    coord.handle_remote_reauth()
    await hass.async_block_till_done()

    reauth_flows = [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]
    assert len(reauth_flows) == 1


async def test_poll_no_token_update_when_none_issued(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
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
    release_refresh = asyncio.Event()

    async def _refresh():
        await release_refresh.wait()

    with (
        patch.object(coord, "async_set_updated_data") as push,
        patch.object(
            coord, "async_request_refresh", AsyncMock(side_effect=_refresh)
        ) as refresh,
    ):
        try:
            coord.handle_art_event(
                "d2d_service_message", {"event": "go_to_standby"}
            )
            coord.handle_art_event(
                "d2d_service_message", {"event": "go_to_standby"}
            )
            # Destination is ambiguous -> never a state change by itself...
            push.assert_not_called()
            coord.config_entry.async_create_background_task.assert_called_once()
            assert (
                coord.config_entry.async_create_background_task.call_args.args[0]
                is hass
            )
            assert (
                coord.config_entry.async_create_background_task.call_args.args[2]
                == "samsungtv_frame-standby-refresh"
            )
            await asyncio.sleep(0)
            refresh.assert_awaited_once()
        finally:
            release_refresh.set()
            await hass.async_block_till_done()
        # ...but it must trigger an immediate poll to resolve OFF fast.
        refresh.assert_awaited_once()
