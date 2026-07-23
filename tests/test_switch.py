# tests/test_switch.py
from dataclasses import replace
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_tv_frame.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.samsung_tv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    TvMode,
)

ART_MODE = "switch.samsung_frame_tv_art_mode_switch"
BRIGHTNESS_SENSOR = "switch.samsung_frame_tv_art_brightness_sensor"
SETTINGS = ArtSettingsSnapshot(
    supported=frozenset({ArtSettingKey.BRIGHTNESS_SENSOR}),
    brightness_sensor_enabled=True,
)


async def _setup(
    hass,
    mock_device,
    *,
    settings: ArtSettingsSnapshot | None = SETTINGS,
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
    mock_device.async_get_art_settings.return_value = settings
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "1.2.3.4",
            CONF_MAC: "02:00:00:00:00:01",
            CONF_TOKEN: "t",
        },
        unique_id="02:00:00:00:00:01",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsung_tv_frame.FrameDevice",
        return_value=mock_device,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_switch_reflects_art_mode(hass, mock_device):
    await _setup(hass, mock_device, art_mode=True)

    assert hass.states.get(ART_MODE).state == "on"


async def test_switch_off_when_watching(hass, mock_device):
    await _setup(hass, mock_device, art_mode=False)

    assert hass.states.get(ART_MODE).state == "off"


async def test_switch_unavailable_when_tv_off(hass, mock_device):
    entry = await _setup(hass, mock_device, reachable=False)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(ART_MODE).state == "unavailable"


async def test_switch_turn_on_sets_art_mode(hass, mock_device):
    await _setup(hass, mock_device, art_mode=False)
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": ART_MODE},
        blocking=True,
    )

    mock_device.async_set_artmode.assert_awaited_once_with(True)


async def test_switch_turn_off_leaves_art_mode(hass, mock_device):
    await _setup(hass, mock_device, art_mode=True)
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": ART_MODE},
        blocking=True,
    )

    mock_device.async_set_artmode.assert_awaited_once_with(False)


@pytest.mark.parametrize(
    ("enabled", "expected"), [(True, "on"), (False, "off")]
)
async def test_brightness_sensor_supported_state(
    hass, mock_device, enabled, expected
):
    settings = ArtSettingsSnapshot(
        supported=frozenset({ArtSettingKey.BRIGHTNESS_SENSOR}),
        brightness_sensor_enabled=enabled,
    )
    await _setup(hass, mock_device, settings=settings)

    assert hass.states.get(BRIGHTNESS_SENSOR).state == expected


async def test_brightness_sensor_category_and_stable_id(hass, mock_device):
    await _setup(hass, mock_device)

    entry = er.async_get(hass).async_get(BRIGHTNESS_SENSOR)
    assert entry is not None
    assert entry.unique_id == (
        "02:00:00:00:00:01_art_brightness_sensor"
    )
    assert entry.entity_category is EntityCategory.CONFIG


@pytest.mark.parametrize("scenario", ["unsupported", "malformed"])
async def test_brightness_sensor_unavailable_when_capability_not_authoritative(
    hass, mock_device, scenario
):
    settings = (
        ArtSettingsSnapshot()
        if scenario == "unsupported"
        else ArtSettingsSnapshot(
            supported=frozenset({ArtSettingKey.BRIGHTNESS_SENSOR}),
            brightness_sensor_enabled=None,
        )
    )
    await _setup(hass, mock_device, settings=settings)

    assert hass.states.get(BRIGHTNESS_SENSOR).state == "unavailable"


@pytest.mark.parametrize("scenario", ["tv-off", "art-unready"])
async def test_brightness_sensor_unavailable_without_live_art_state(
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

    assert hass.states.get(BRIGHTNESS_SENSOR).state == "unavailable"


async def test_brightness_sensor_unavailable_for_stale_generation(
    hass, mock_device
):
    entry = await _setup(hass, mock_device)
    coordinator = entry.runtime_data
    mock_device.art_generation = 2
    coordinator.async_set_updated_data(
        replace(coordinator.data, current_art="stale-generation")
    )
    await hass.async_block_till_done()

    assert hass.states.get(BRIGHTNESS_SENSOR).state == "unavailable"


async def test_brightness_sensor_mutations_force_two_direct_art_reconciles(
    hass, mock_device
):
    entry = await _setup(hass, mock_device)
    coordinator = entry.runtime_data
    with (
        patch.object(
            coordinator,
            "async_request_art_reconcile",
            new_callable=AsyncMock,
        ) as reconcile,
        patch.object(
            coordinator,
            "async_request_refresh",
            new_callable=AsyncMock,
        ) as ordinary_refresh,
    ):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": BRIGHTNESS_SENSOR},
            blocking=True,
        )
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": BRIGHTNESS_SENSOR},
            blocking=True,
        )

    assert mock_device.async_set_brightness_sensor.await_args_list == [
        call(False),
        call(True),
    ]
    assert reconcile.await_count == 2
    ordinary_refresh.assert_not_awaited()


async def test_brightness_sensor_mutation_error_hides_requested_value(
    hass, mock_device
):
    error = RuntimeError(
        "private protocol value False"
    )
    mock_device.async_set_brightness_sensor.side_effect = error
    entry = await _setup(hass, mock_device)
    coordinator = entry.runtime_data

    with (
        patch.object(
            coordinator,
            "async_request_art_reconcile",
            new_callable=AsyncMock,
        ) as reconcile,
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": BRIGHTNESS_SENSOR},
            blocking=True,
        )

    assert "False" not in str(raised.value)
    assert "private protocol" not in str(raised.value)
    assert str(raised.value) == "Failed to set art brightness sensor"
    assert raised.value.__cause__ is error
    reconcile.assert_not_awaited()
