from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.samsungtv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    TvMode,
)
from custom_components.samsungtv_frame.select import (
    FrameArtMotionSensitivitySelect,
    FrameArtSleepAfterSelect,
)
from custom_components.samsungtv_frame.sensor import FrameSlideshowSensor
from custom_components.samsungtv_frame.switch import FrameBrightnessSensorSwitch

SLEEP_AFTER = "select.samsung_frame_tv_art_sleep_after"
MOTION_SENSITIVITY = "select.samsung_frame_tv_art_motion_sensitivity"
SETTINGS = ArtSettingsSnapshot(
    supported=frozenset(
        {
            ArtSettingKey.MOTION_TIMER,
            ArtSettingKey.MOTION_SENSITIVITY,
        }
    ),
    motion_timer="30",
    motion_sensitivity="2",
)
SELECT_CASES = [
    pytest.param(
        SLEEP_AFTER,
        ArtSettingKey.MOTION_TIMER,
        "motion_timer",
        "30",
        id="sleep-after",
    ),
    pytest.param(
        MOTION_SENSITIVITY,
        ArtSettingKey.MOTION_SENSITIVITY,
        "motion_sensitivity",
        "2",
        id="motion-sensitivity",
    ),
]


def _settings_for(
    key: ArtSettingKey, field: str, value: str | None
) -> ArtSettingsSnapshot:
    return ArtSettingsSnapshot(
        supported=frozenset({key}),
        **{field: value},
    )


async def _setup(
    hass,
    mock_device,
    *,
    settings: ArtSettingsSnapshot | None = SETTINGS,
    art_ready: bool = True,
    generation: int = 1,
    reachable: bool = True,
):
    mock_device.art_ready = art_ready
    mock_device.art_generation = generation
    mock_device.async_device_info.return_value = (
        {"PowerState": "on"} if reachable else None
    )
    mock_device.async_get_artmode.return_value = True
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
        "custom_components.samsungtv_frame.FrameDevice",
        return_value=mock_device,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_select_platform_states_options_categories_and_stable_ids(
    hass, mock_device
):
    await _setup(hass, mock_device)

    sleep = hass.states.get(SLEEP_AFTER)
    sensitivity = hass.states.get(MOTION_SENSITIVITY)
    assert sleep.state == "30"
    assert sleep.attributes["options"] == [
        "off",
        "5",
        "15",
        "30",
        "60",
        "120",
        "240",
    ]
    assert sensitivity.state == "2"
    assert sensitivity.attributes["options"] == ["1", "2", "3"]

    registry = er.async_get(hass)
    expected_ids = {
        SLEEP_AFTER: "02:00:00:00:00:01_art_sleep_after",
        MOTION_SENSITIVITY: (
            "02:00:00:00:00:01_art_motion_sensitivity"
        ),
    }
    for entity_id, unique_id in expected_ids.items():
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.unique_id == unique_id
        assert entry.entity_category is EntityCategory.CONFIG


@pytest.mark.parametrize(
    ("entity_id", "key", "field", "value"), SELECT_CASES
)
async def test_optional_select_supported_state(
    hass, mock_device, entity_id, key, field, value
):
    await _setup(
        hass,
        mock_device,
        settings=_settings_for(key, field, value),
    )

    assert hass.states.get(entity_id).state == value


@pytest.mark.parametrize(
    ("entity_id", "key", "field", "value"), SELECT_CASES
)
@pytest.mark.parametrize("scenario", ["unsupported", "malformed"])
async def test_optional_select_unavailable_when_capability_not_authoritative(
    hass, mock_device, entity_id, key, field, value, scenario
):
    settings = (
        ArtSettingsSnapshot()
        if scenario == "unsupported"
        else _settings_for(key, field, None)
    )
    await _setup(hass, mock_device, settings=settings)

    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.parametrize(
    ("entity_id", "key", "field", "value"), SELECT_CASES
)
@pytest.mark.parametrize("scenario", ["tv-off", "art-unready"])
async def test_optional_select_unavailable_without_live_art_state(
    hass, mock_device, entity_id, key, field, value, scenario
):
    entry = await _setup(
        hass,
        mock_device,
        settings=_settings_for(key, field, value),
    )
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

    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.parametrize(
    ("entity_id", "key", "field", "value"), SELECT_CASES
)
async def test_optional_select_unavailable_for_stale_generation(
    hass, mock_device, entity_id, key, field, value
):
    entry = await _setup(
        hass,
        mock_device,
        settings=_settings_for(key, field, value),
    )
    coordinator = entry.runtime_data
    mock_device.art_generation = 2
    coordinator.async_set_updated_data(
        replace(coordinator.data, current_art="stale-generation")
    )
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"


@pytest.mark.parametrize(
    ("entity_id", "values", "device_method"),
    [
        pytest.param(
            SLEEP_AFTER,
            ("5", "120"),
            "async_set_motion_timer",
            id="sleep-after",
        ),
        pytest.param(
            MOTION_SENSITIVITY,
            ("1", "3"),
            "async_set_motion_sensitivity",
            id="motion-sensitivity",
        ),
    ],
)
async def test_select_mutations_force_two_direct_art_reconciles(
    hass, mock_device, entity_id, values, device_method
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
        for value in values:
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": value},
                blocking=True,
            )

    assert getattr(mock_device, device_method).await_args_list == [
        call(value) for value in values
    ]
    assert reconcile.await_count == 2
    ordinary_refresh.assert_not_awaited()


@pytest.mark.parametrize(
    ("entity_id", "value", "device_method"),
    [
        pytest.param(
            SLEEP_AFTER,
            "240",
            "async_set_motion_timer",
            id="sleep-after",
        ),
        pytest.param(
            MOTION_SENSITIVITY,
            "3",
            "async_set_motion_sensitivity",
            id="motion-sensitivity",
        ),
    ],
)
async def test_select_mutation_error_hides_requested_value(
    hass, mock_device, entity_id, value, device_method
):
    error = RuntimeError(
        f"private protocol value {value}"
    )
    getattr(mock_device, device_method).side_effect = error
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
            "select",
            "select_option",
            {"entity_id": entity_id, "option": value},
            blocking=True,
        )

    assert value not in str(raised.value)
    assert "private protocol" not in str(raised.value)
    expected = {
        SLEEP_AFTER: "Failed to set art sleep after",
        MOTION_SENSITIVITY: "Failed to set art motion sensitivity",
    }
    assert str(raised.value) == expected[entity_id]
    assert raised.value.__cause__ is error
    reconcile.assert_not_awaited()


def test_task_4_entity_classes_use_translation_keys_without_names():
    expected = {
        FrameArtSleepAfterSelect: "art_sleep_after",
        FrameArtMotionSensitivitySelect: "art_motion_sensitivity",
        FrameBrightnessSensorSwitch: "art_brightness_sensor",
        FrameSlideshowSensor: "art_slideshow",
    }

    for entity_class, translation_key in expected.items():
        assert entity_class.__dict__["__attr_translation_key"] == translation_key
        assert "__attr_name" not in entity_class.__dict__


def test_optional_entity_translations_are_exact_and_neutral():
    root = Path(__file__).parents[1]
    expected_sleep_states = {
        "off": "Off",
        "5": "5 minutes",
        "15": "15 minutes",
        "30": "30 minutes",
        "60": "1 hour",
        "120": "2 hours",
        "240": "4 hours",
    }
    expected_slideshow_states = {
        "off": "Off",
        "sequential": "Sequential",
        "shuffle": "Shuffle",
    }

    for relative_path in (
        "custom_components/samsungtv_frame/strings.json",
        "custom_components/samsungtv_frame/translations/en.json",
    ):
        payload = json.loads((root / relative_path).read_text())
        entity = payload["entity"]
        assert entity["select"]["art_sleep_after"] == {
            "name": "Art sleep after",
            "state": expected_sleep_states,
        }
        assert entity["select"]["art_motion_sensitivity"] == {
            "name": "Art motion sensitivity"
        }
        assert entity["switch"]["art_brightness_sensor"] == {
            "name": "Art brightness sensor"
        }
        assert entity["sensor"]["art_slideshow"] == {
            "name": "Art slideshow",
            "state": expected_slideshow_states,
        }
