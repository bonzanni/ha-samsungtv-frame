"""Data models and pure state derivation for Samsung Frame TV."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TvMode(StrEnum):
    """Derived high-level TV mode used by automations."""

    OFF = "off"
    WATCHING = "watching"
    ART_MODE = "art_mode"
    UNKNOWN = "unknown"


class ArtSettingKey(StrEnum):
    """Known optional Art Mode setting keys."""

    BRIGHTNESS = "brightness"
    COLOR_TEMPERATURE = "color_temperature"
    MOTION_TIMER = "motion_timer"
    MOTION_SENSITIVITY = "motion_sensitivity"
    BRIGHTNESS_SENSOR = "brightness_sensor_setting"


class SlideshowMode(StrEnum):
    """Known slideshow playback modes."""

    OFF = "off"
    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"


def derive_tv_mode(
    reachable: bool,
    art_mode: bool | None,
    power_state: str | None,
    standby_wins: bool = False,
) -> TvMode:
    """Derive the tri-state from the three raw signals.

    Order matters: unreachable => OFF; art websocket is the source of truth for
    art mode (never gated on PowerState by default — 2025 LS03F Frames report
    "standby" during normal art mode, upstream samsung-tv-ws-api #185);
    art-off + powered => WATCHING; a reachable TV reporting PowerState
    "standby" is dark (this Frame model keeps its NIC up for several minutes
    after power-off, answering REST as "standby" while reachable) => OFF;
    anything else is transitional/UNKNOWN and is held as last-stable by the
    coordinator.

    standby_wins flips the art-vs-standby precedence: once the coordinator has
    seen art mode coexist with PowerState "on" (2022-24 models), "standby" can
    only mean the TV is shutting down — even though its art websocket keeps
    answering "on" for ~50 s while it dies, which would otherwise hold ART for
    the whole shutdown window.
    """
    if not reachable:
        return TvMode.OFF
    if standby_wins and power_state == "standby":
        return TvMode.OFF
    if art_mode is True:
        return TvMode.ART_MODE
    if art_mode is False and power_state == "on":
        return TvMode.WATCHING
    if power_state == "standby":
        return TvMode.OFF
    return TvMode.UNKNOWN


@dataclass(frozen=True)
class ArtSettingsSnapshot:
    """Normalized optional Art Mode settings and their advertised support."""

    supported: frozenset[ArtSettingKey] = frozenset()
    brightness: int | None = None
    color_temperature: int | None = None
    motion_timer: str | None = None
    motion_sensitivity: str | None = None
    brightness_sensor_enabled: bool | None = None


@dataclass(frozen=True)
class SlideshowState:
    """Normalized slideshow state."""

    mode: SlideshowMode
    duration_minutes: int
    category_id: str | None = None


@dataclass(frozen=True)
class FrameData:
    """Single fan-in snapshot of TV state shared by all entities."""

    reachable: bool
    power_state: str | None
    art_mode: bool | None
    tv_mode: TvMode
    current_art: str | None = None
    art_brightness: int | None = None
    art_color_temperature: int | None = None
    running_app: str | None = None
    volume_level: float | None = None
    is_muted: bool | None = None
    art_settings: ArtSettingsSnapshot | None = None
    slideshow: SlideshowState | None = None
    optional_art_generation: int | None = None
