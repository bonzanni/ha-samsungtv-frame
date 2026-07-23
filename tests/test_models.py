from dataclasses import FrozenInstanceError, fields

import pytest

from custom_components.samsung_tv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    FrameData,
    SlideshowMode,
    SlideshowState,
    TvMode,
    derive_tv_mode,
)


@pytest.mark.parametrize(
    ("reachable", "art_mode", "power_state", "expected"),
    [
        (False, None, None, TvMode.OFF),          # unreachable => OFF regardless
        (False, True, "on", TvMode.OFF),          # unreachable wins even if art cached True
        (True, True, "on", TvMode.ART_MODE),      # art is source of truth
        (True, True, "standby", TvMode.ART_MODE), # do NOT gate art on PowerState (#185 trap)
        (True, False, "on", TvMode.WATCHING),     # art off + powered => watching
        (True, False, "standby", TvMode.OFF),      # reachable + art-off + standby => dark/OFF
        (True, None, "standby", TvMode.OFF),       # reachable + standby (art not yet known) => OFF
        (True, None, "on", TvMode.UNKNOWN),       # art unknown yet => transitional
    ],
)
def test_derive_tv_mode(reachable, art_mode, power_state, expected):
    assert derive_tv_mode(reachable, art_mode, power_state) == expected


@pytest.mark.parametrize(
    ("reachable", "art_mode", "power_state", "expected"),
    [
        # standby overrides even a (dying) art socket still answering "on"
        (True, True, "standby", TvMode.OFF),
        (True, None, "standby", TvMode.OFF),
        # everything else is unchanged
        (True, True, "on", TvMode.ART_MODE),
        (True, False, "on", TvMode.WATCHING),
        (False, True, "on", TvMode.OFF),
    ],
)
def test_derive_tv_mode_standby_wins(reachable, art_mode, power_state, expected):
    """Once art+power-on has been observed (2022-24 models), standby means
    shutdown regardless of what the art websocket claims."""
    assert (
        derive_tv_mode(reachable, art_mode, power_state, standby_wins=True)
        == expected
    )


def test_frame_data_optional_art_details_default_unknown():
    data = FrameData(True, "on", True, TvMode.ART_MODE)

    assert data.art_settings is None
    assert data.slideshow is None
    assert data.optional_art_generation is None


def test_frame_data_has_one_canonical_art_settings_state():
    field_names = {field.name for field in fields(FrameData)}

    assert "art_brightness" not in field_names
    assert "art_color_temperature" not in field_names
    assert "art_settings" in field_names


def test_art_detail_snapshots_are_immutable():
    settings = ArtSettingsSnapshot(
        supported=frozenset({ArtSettingKey.BRIGHTNESS}), brightness=7
    )
    slideshow = SlideshowState(SlideshowMode.SEQUENTIAL, 30, "MY-C0004")

    with pytest.raises(FrozenInstanceError):
        settings.brightness = 8
    with pytest.raises(FrozenInstanceError):
        slideshow.duration_minutes = 60
