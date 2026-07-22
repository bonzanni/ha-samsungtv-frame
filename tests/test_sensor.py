from dataclasses import replace
from unittest.mock import patch

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.samsungtv_frame.models import (
    SlideshowMode,
    SlideshowState,
    TvMode,
)

TV_MODE = "sensor.samsung_frame_tv_tv_mode"
CURRENT_ART = "sensor.samsung_frame_tv_current_art"
SLIDESHOW = "sensor.samsung_frame_tv_art_slideshow"
SLIDESHOW_STATE = SlideshowState(
    mode=SlideshowMode.SHUFFLE,
    duration_minutes=30,
    category_id="MY-C0004",
)


async def _setup(
    hass,
    mock_device,
    *,
    slideshow: SlideshowState | None = SLIDESHOW_STATE,
    art_ready: bool = True,
    generation: int = 1,
    reachable: bool = True,
    art_mode: bool = True,
):
    mock_device.art_ready = art_ready
    mock_device.art_generation = generation
    mock_device.async_device_info.return_value = (
        {"PowerState": "on"} if reachable else None
    )
    mock_device.async_get_artmode.return_value = art_mode
    mock_device.async_get_slideshow_state.return_value = slideshow
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "1.2.3.4",
            CONF_MAC: "A0:D0:5B:86:CE:B7",
            CONF_TOKEN: "t",
        },
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice",
        return_value=mock_device,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_tv_mode_sensor_watching(hass, mock_device):
    await _setup(hass, mock_device, art_mode=False)

    state = hass.states.get(TV_MODE)
    assert state.state == "watching"
    assert state.attributes["options"] == ["off", "watching", "art_mode"]


async def test_current_art_sensor(hass, mock_device):
    mock_device.async_get_current_art.return_value = "MY_F0034"
    await _setup(hass, mock_device)

    assert hass.states.get(CURRENT_ART).state == "MY_F0034"


async def test_slideshow_state_options_attributes_and_stable_id(
    hass, mock_device
):
    await _setup(hass, mock_device)

    state = hass.states.get(SLIDESHOW)
    assert state.state == "shuffle"
    assert state.attributes["options"] == ["off", "sequential", "shuffle"]
    assert state.attributes["device_class"] == "enum"
    assert {
        key: state.attributes[key]
        for key in ("duration_minutes", "category_id")
    } == {
        "duration_minutes": 30,
        "category_id": "MY-C0004",
    }
    assert set(state.attributes) - {
        "options",
        "device_class",
        "friendly_name",
    } == {"duration_minutes", "category_id"}

    registry_entry = er.async_get(hass).async_get(SLIDESHOW)
    assert registry_entry is not None
    assert registry_entry.unique_id == "A0:D0:5B:86:CE:B7_art_slideshow"


async def test_slideshow_unavailable_without_authoritative_state(
    hass, mock_device
):
    await _setup(hass, mock_device, slideshow=None)

    assert hass.states.get(SLIDESHOW).state == "unavailable"


@pytest.mark.parametrize("scenario", ["tv-off", "art-unready"])
async def test_slideshow_unavailable_without_live_art_state(
    hass, mock_device, scenario
):
    entry = await _setup(hass, mock_device)
    coordinator = entry.runtime_data
    if scenario == "art-unready":
        mock_device.art_ready = False
        updated = replace(
            coordinator.data, current_art="cached-while-unready"
        )
    else:
        updated = replace(coordinator.data, tv_mode=TvMode.OFF)
    coordinator.async_set_updated_data(updated)
    await hass.async_block_till_done()

    assert hass.states.get(SLIDESHOW).state == "unavailable"


async def test_slideshow_unavailable_for_stale_generation(hass, mock_device):
    entry = await _setup(hass, mock_device)
    coordinator = entry.runtime_data
    mock_device.art_generation = 2
    coordinator.async_set_updated_data(
        replace(coordinator.data, current_art="stale-generation")
    )
    await hass.async_block_till_done()

    assert hass.states.get(SLIDESHOW).state == "unavailable"
