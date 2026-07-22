"""Pure parsing for Samsung Frame optional Art state."""
from __future__ import annotations

import json
from typing import Any

from .models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    SlideshowMode,
    SlideshowState,
)

MOTION_TIMERS = frozenset({"off", "5", "15", "30", "60", "120", "240"})
MOTION_SENSITIVITIES = frozenset({"1", "2", "3"})


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    if type(value) is int:
        normalized = value
    elif isinstance(value, str):
        try:
            normalized = int(value)
        except ValueError:
            return None
        if value.strip() != str(normalized):
            return None
    else:
        return None
    return normalized if minimum <= normalized <= maximum else None


def _choice(value: Any, options: frozenset[str]) -> str | None:
    if isinstance(value, bool):
        return None
    normalized = str(value)
    return normalized if normalized in options else None


def _on_off(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"on", "off"}:
            return normalized == "on"
    return None


def normalize_art_setting(
    key: ArtSettingKey, value: Any
) -> int | str | bool | None:
    """Normalize a value from aggregate or correlated legacy responses."""
    if key is ArtSettingKey.BRIGHTNESS:
        return _bounded_int(value, 0, 10)
    if key is ArtSettingKey.COLOR_TEMPERATURE:
        return _bounded_int(value, -5, 5)
    if key is ArtSettingKey.MOTION_TIMER:
        return _choice(value, MOTION_TIMERS)
    if key is ArtSettingKey.MOTION_SENSITIVITY:
        return _choice(value, MOTION_SENSITIVITIES)
    return _on_off(value)


def parse_art_settings(payload: dict[str, Any]) -> ArtSettingsSnapshot | None:
    """Parse an aggregate Art Mode settings response."""
    raw = payload.get("data")
    if not isinstance(raw, str):
        return None
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list):
        return None

    supported: set[ArtSettingKey] = set()
    normalized: dict[ArtSettingKey, int | str | bool | None] = {}
    duplicates: set[ArtSettingKey] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            key = ArtSettingKey(item.get("item"))
        except (TypeError, ValueError):
            continue
        supported.add(key)
        if key in normalized:
            duplicates.add(key)
            normalized[key] = None
            continue
        if key not in duplicates:
            normalized[key] = normalize_art_setting(key, item.get("value"))

    return ArtSettingsSnapshot(
        supported=frozenset(supported),
        brightness=normalized.get(ArtSettingKey.BRIGHTNESS),
        color_temperature=normalized.get(ArtSettingKey.COLOR_TEMPERATURE),
        motion_timer=normalized.get(ArtSettingKey.MOTION_TIMER),
        motion_sensitivity=normalized.get(ArtSettingKey.MOTION_SENSITIVITY),
        brightness_sensor_enabled=normalized.get(ArtSettingKey.BRIGHTNESS_SENSOR),
    )


def parse_slideshow(payload: dict[str, Any]) -> SlideshowState | None:
    """Parse optional slideshow state."""
    category_id = payload.get("category_id")
    if category_id is not None and not isinstance(category_id, str):
        return None
    value = payload.get("value")
    if value == "off":
        return SlideshowState(SlideshowMode.OFF, 0, category_id)
    duration = _bounded_int(value, 1, 24 * 60)
    if duration is None:
        return None
    kind = payload.get("type")
    if kind == "slideshow":
        mode = SlideshowMode.SEQUENTIAL
    elif kind == "shuffleslideshow":
        mode = SlideshowMode.SHUFFLE
    else:
        return None
    return SlideshowState(mode, duration, category_id)
