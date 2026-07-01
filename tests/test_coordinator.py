# tests/test_coordinator.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.samsungtv_frame.coordinator import FrameCoordinator
from custom_components.samsungtv_frame.models import FrameData, TvMode


def _make(hass, device) -> FrameCoordinator:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "abc"
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


async def test_art_event_go_to_standby_holds(hass, mock_device):
    coord = _make(hass, mock_device)
    coord.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=True,
        tv_mode=TvMode.ART_MODE,
        current_art=None,
    )
    with patch.object(coord, "async_set_updated_data") as push:
        coord.handle_art_event("d2d_service_message", {"event": "go_to_standby"})
        push.assert_not_called()
