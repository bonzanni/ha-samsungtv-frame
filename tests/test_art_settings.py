import json

import pytest

from custom_components.samsung_tv_frame.art_settings import (
    normalize_art_setting,
    parse_art_settings,
    parse_slideshow,
)
from custom_components.samsung_tv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    SlideshowMode,
    SlideshowState,
)


def test_parse_complete_art_settings_normalizes_known_values():
    payload = {
        "data": json.dumps(
            [
                {"item": "brightness", "value": "7"},
                {"item": "color_temperature", "value": -2},
                {"item": "motion_timer", "value": "30"},
                {"item": "motion_sensitivity", "value": 2},
                {"item": "brightness_sensor_setting", "value": "on"},
                {"item": "future_setting", "value": "ignored"},
            ]
        )
    }

    assert parse_art_settings(payload) == ArtSettingsSnapshot(
        supported=frozenset(ArtSettingKey),
        brightness=7,
        color_temperature=-2,
        motion_timer="30",
        motion_sensitivity="2",
        brightness_sensor_enabled=True,
    )


def test_parse_valid_list_marks_missing_known_items_unsupported():
    assert parse_art_settings({"data": "[]"}) == ArtSettingsSnapshot()


@pytest.mark.parametrize("data", [None, 4, {}, "not-json", "{}"])
def test_parse_malformed_whole_settings_returns_none(data):
    assert parse_art_settings({"data": data}) is None


def test_parse_duplicate_setting_is_supported_but_value_unknown():
    payload = {
        "data": json.dumps(
            [
                {"item": "brightness", "value": 6},
                {"item": "brightness", "value": 7},
            ]
        )
    }

    result = parse_art_settings(payload)

    assert result is not None
    assert ArtSettingKey.BRIGHTNESS in result.supported
    assert result.brightness is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (ArtSettingKey.BRIGHTNESS, -1),
        (ArtSettingKey.BRIGHTNESS, 11),
        (ArtSettingKey.BRIGHTNESS, " 7 "),
        (ArtSettingKey.BRIGHTNESS, True),
        (ArtSettingKey.BRIGHTNESS, 7.0),
        (ArtSettingKey.COLOR_TEMPERATURE, -6),
        (ArtSettingKey.COLOR_TEMPERATURE, 6),
        (ArtSettingKey.COLOR_TEMPERATURE, "\t-2\n"),
        (ArtSettingKey.MOTION_TIMER, "10"),
        (ArtSettingKey.MOTION_TIMER, True),
        (ArtSettingKey.MOTION_SENSITIVITY, "0"),
        (ArtSettingKey.MOTION_SENSITIVITY, "4"),
        (ArtSettingKey.BRIGHTNESS_SENSOR, "yes"),
        (ArtSettingKey.BRIGHTNESS_SENSOR, 1),
    ],
)
def test_parse_advertised_invalid_setting_keeps_support_but_value_unknown(
    key, value
):
    result = parse_art_settings(
        {"data": json.dumps([{"item": key.value, "value": value}])}
    )

    assert result is not None
    assert key in result.supported
    assert normalize_art_setting(key, value) is None
    field = {
        ArtSettingKey.BRIGHTNESS: "brightness",
        ArtSettingKey.COLOR_TEMPERATURE: "color_temperature",
        ArtSettingKey.MOTION_TIMER: "motion_timer",
        ArtSettingKey.MOTION_SENSITIVITY: "motion_sensitivity",
        ArtSettingKey.BRIGHTNESS_SENSOR: "brightness_sensor_enabled",
    }[key]
    assert getattr(result, field) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"value": "off", "type": "slideshow", "category_id": "MY-C0004"},
            SlideshowState(SlideshowMode.OFF, 0, "MY-C0004"),
        ),
        (
            {"value": "30", "type": "slideshow", "category_id": "MY-C0004"},
            SlideshowState(SlideshowMode.SEQUENTIAL, 30, "MY-C0004"),
        ),
        (
            {
                "value": 60,
                "type": "shuffleslideshow",
                "category_id": "MY-C0008",
            },
            SlideshowState(SlideshowMode.SHUFFLE, 60, "MY-C0008"),
        ),
    ],
)
def test_parse_slideshow(payload, expected):
    assert parse_slideshow(payload) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"value": 0, "type": "slideshow"},
        {"value": -1, "type": "slideshow"},
        {"value": "\t30\n", "type": "slideshow"},
        {"value": True, "type": "slideshow"},
        {"value": 30.0, "type": "slideshow"},
        {"value": 30, "type": "unknown"},
        {"value": 30, "type": "slideshow", "category_id": 4},
    ],
)
def test_parse_invalid_slideshow_returns_none(payload):
    assert parse_slideshow(payload) is None
