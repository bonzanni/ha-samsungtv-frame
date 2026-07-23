# tests/test_number.py
from dataclasses import replace
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
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

BRIGHTNESS = "number.samsung_frame_tv_art_brightness"
COLOR_TEMPERATURE = "number.samsung_frame_tv_art_color_temperature"
SETTINGS = ArtSettingsSnapshot(
    supported=frozenset(
        {ArtSettingKey.BRIGHTNESS, ArtSettingKey.COLOR_TEMPERATURE}
    ),
    brightness=7,
    color_temperature=-2,
)
NUMBER_CASES = [
    pytest.param(
        BRIGHTNESS,
        ArtSettingKey.BRIGHTNESS,
        "brightness",
        7,
        id="brightness",
    ),
    pytest.param(
        COLOR_TEMPERATURE,
        ArtSettingKey.COLOR_TEMPERATURE,
        "color_temperature",
        -2,
        id="color-temperature",
    ),
]


def _settings_for(
    key: ArtSettingKey, field: str, value: int | None
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
        "custom_components.samsung_tv_frame.FrameDevice",
        return_value=mock_device,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("entity_id", "key", "field", "value"), NUMBER_CASES
)
async def test_optional_number_supported_state(
    hass, mock_device, entity_id, key, field, value
):
    await _setup(
        hass,
        mock_device,
        settings=_settings_for(key, field, value),
    )

    assert hass.states.get(entity_id).state == str(value)


@pytest.mark.parametrize(
    ("entity_id", "key", "field", "value"), NUMBER_CASES
)
@pytest.mark.parametrize("scenario", ["unsupported", "malformed"])
async def test_optional_number_unavailable_when_capability_not_authoritative(
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
    ("entity_id", "key", "field", "value"), NUMBER_CASES
)
@pytest.mark.parametrize("scenario", ["tv-off", "art-unready"])
async def test_optional_number_unavailable_without_live_art_state(
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
    ("entity_id", "key", "field", "value"), NUMBER_CASES
)
async def test_optional_number_unavailable_for_stale_generation(
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
            BRIGHTNESS,
            (8, 9),
            "async_set_art_brightness",
            id="brightness",
        ),
        pytest.param(
            COLOR_TEMPERATURE,
            (3, 4),
            "async_set_color_temperature",
            id="color-temperature",
        ),
    ],
)
async def test_optional_number_mutations_force_two_direct_art_reconciles(
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
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
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
            BRIGHTNESS,
            8,
            "async_set_art_brightness",
            id="brightness",
        ),
        pytest.param(
            COLOR_TEMPERATURE,
            3,
            "async_set_color_temperature",
            id="color-temperature",
        ),
    ],
)
async def test_optional_number_mutation_error_hides_requested_value(
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
            "number",
            "set_value",
            {"entity_id": entity_id, "value": value},
            blocking=True,
        )

    assert str(value) not in str(raised.value)
    assert "private protocol" not in str(raised.value)
    expected = {
        BRIGHTNESS: "Failed to set art brightness",
        COLOR_TEMPERATURE: "Failed to set art color temperature",
    }
    assert str(raised.value) == expected[entity_id]
    assert raised.value.__cause__ is error
    reconcile.assert_not_awaited()
