# Local Art Settings Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add privacy-safe diagnostics, aggregate Art-settings state, slideshow readback, and supported local controls for Sleep After, motion sensitivity, and the brightness sensor without SmartThings or production deployment.

**Architecture:** Extend the existing `FrameCoordinator` and its single supervised Art websocket. Parse one aggregate settings response plus one slideshow response into immutable snapshots, publish only current-generation state, and force direct authoritative reconciliation after mutations. Optional-feature failures remain isolated from OFF/WATCHING/ART derivation.

**Tech Stack:** Python 3.13+, Home Assistant config-entry/entity/diagnostics APIs, `asyncio`, `websockets`, `samsungtvws[async,encrypted]==3.0.5`, pytest, pytest-homeassistant-custom-component.

## Global Constraints

- Keep `samsungtvws[async,encrypted]==3.0.5`; add no Samsung websocket dependency and no synchronous runtime fallback.
- Keep one supervised Art websocket and one receiver per config entry; entities never access the websocket directly.
- Background reads never open, pair, or retry the Art session. User mutations use the existing `ArtSessionTrigger.USER` readiness path and execute exactly once.
- Reconcile Art mode, current artwork, one aggregate settings response, and one slideshow response only on READY, every `300` seconds, or after a successful mutation—not on every heartbeat.
- Publish optional state only when fresh for the current READY generation. Optional failures never increment `_art_fail_streak` or change the core OFF/WATCHING/ART result.
- Legacy brightness/color and slideshow fallback occurs only after a correlated `ResponseError`, never after malformed data, timeout, disconnect, or generation loss.
- Motion timer wire values are exactly `off`, `5`, `15`, `30`, `60`, `120`, `240`; sensitivity wire values are exactly `1`, `2`, `3`; brightness-sensor wire values are exactly `on`, `off`.
- Use neutral sensitivity options `1`, `2`, `3`; do not infer low/medium/high mapping.
- Diagnostics perform no I/O and never include host, MAC, token, entry identifiers/title, content IDs, app names, volume/mute, raw payloads, exception text, or arbitrary options/private attributes.
- Preserve existing entity unique IDs and config-entry schema/version. Add `Platform.SELECT`; diagnostics are auto-discovered and are not a platform.
- Follow strict red-green-refactor: observe each production behavior test fail before implementing it.
- Call external Claude only through the `claude-lesina` shell function. Prefer Fable for plan/code review; use Terra for independent architecture/spec-conformance review.
- Do not unload, reload, deploy, or run branch acceptance against the production N150. The authorized direct-TV evidence probe used only the installed library, correlated all three setters, restored all original settings, and copied no branch code; no further production probe is part of this plan.
- Run the complete test suite before every task commit, in addition to that task's focused tests.

---

## File Map

- Create `custom_components/samsungtv_frame/art_settings.py`: pure parsing and normalization for aggregate Art settings and slideshow payloads.
- Modify `custom_components/samsungtv_frame/models.py`: immutable Art-settings/slideshow enums and snapshots carried by `FrameData`.
- Modify `custom_components/samsungtv_frame/frame_art.py`: raw aggregate/slideshow and direct legacy getters plus live-verified setting setters.
- Modify `custom_components/samsungtv_frame/device.py`: background-only optional reads, generation-scoped dialect caches, and USER-triggered setting mutations.
- Modify `custom_components/samsungtv_frame/coordinator.py`: aggregate generation-fenced reconciliation, direct post-write refresh, field-preserving push updates, and allowlisted diagnostics snapshot.
- Modify `custom_components/samsungtv_frame/entity.py`: shared optional Art-setting availability predicate.
- Modify `custom_components/samsungtv_frame/number.py`: migrate brightness/color to the snapshot and remove stale normal-refresh behavior.
- Create `custom_components/samsungtv_frame/select.py`: Sleep After and motion-sensitivity controls.
- Modify `custom_components/samsungtv_frame/switch.py`: brightness-sensor configuration switch.
- Modify `custom_components/samsungtv_frame/sensor.py`: read-only slideshow ENUM sensor.
- Modify `custom_components/samsungtv_frame/media_player.py`: force Art reconciliation after slideshow writes.
- Modify `custom_components/samsungtv_frame/const.py`: register `Platform.SELECT`.
- Create `custom_components/samsungtv_frame/diagnostics.py`: zero-I/O config-entry diagnostics adapter.
- Modify `custom_components/samsungtv_frame/strings.json` and `custom_components/samsungtv_frame/translations/en.json`: entity names, states, and select options.
- Modify `tests/conftest.py`: explicit optional-feature mock methods/attributes.
- Create `tests/test_art_settings.py`, `tests/test_select.py`, and `tests/test_diagnostics.py`.
- Modify `tests/test_models.py`, `tests/test_frame_art.py`, `tests/test_device.py`, `tests/test_coordinator.py`, `tests/test_number.py`, `tests/test_switch.py`, `tests/test_sensor.py`, `tests/test_media_player.py`, and `tests/test_init.py`.
- Modify `README.md` and `CHANGELOG.md`: feature documentation and intentional brightness/color availability change.

---

### Task 1: Immutable Models and Pure Payload Parsing

**Files:**
- Create: `custom_components/samsungtv_frame/art_settings.py`
- Modify: `custom_components/samsungtv_frame/models.py`
- Create: `tests/test_art_settings.py`
- Modify: `tests/test_models.py`

**Interfaces:**
- Produces: `ArtSettingKey`, `ArtSettingsSnapshot`, `SlideshowMode`, `SlideshowState`.
- Produces: `parse_art_settings(payload: dict[str, Any]) -> ArtSettingsSnapshot | None`.
- Produces: `parse_slideshow(payload: dict[str, Any]) -> SlideshowState | None`.
- Produces: `normalize_art_setting(key: ArtSettingKey, value: Any) -> int | str | bool | None` for correlated legacy responses.
- `None` means the whole optional payload is unknown/malformed; a valid snapshot with a missing key means that key is unsupported.

- [ ] **Step 1: Write failing immutable-model tests**

Append tests that construct the new frozen dataclasses and prove `FrameData` defaults preserve compatibility:

```python
from dataclasses import FrozenInstanceError

from custom_components.samsungtv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    FrameData,
    SlideshowMode,
    SlideshowState,
    TvMode,
)


def test_frame_data_optional_art_details_default_unknown():
    data = FrameData(True, "on", True, TvMode.ART_MODE)
    assert data.art_settings is None
    assert data.slideshow is None
    assert data.optional_art_generation is None


def test_art_detail_snapshots_are_immutable():
    settings = ArtSettingsSnapshot(
        supported=frozenset({ArtSettingKey.BRIGHTNESS}), brightness=7
    )
    slideshow = SlideshowState(SlideshowMode.SEQUENTIAL, 30, "MY-C0004")
    with pytest.raises(FrozenInstanceError):
        settings.brightness = 8
    with pytest.raises(FrozenInstanceError):
        slideshow.duration_minutes = 60
```

- [ ] **Step 2: Write failing parser tests**

Create table-driven tests covering:

```python
def test_parse_complete_art_settings_normalizes_known_values():
    payload = {"data": json.dumps([
        {"item": "brightness", "value": "7"},
        {"item": "color_temperature", "value": -2},
        {"item": "motion_timer", "value": "30"},
        {"item": "motion_sensitivity", "value": 2},
        {"item": "brightness_sensor_setting", "value": "on"},
        {"item": "future_setting", "value": "ignored"},
    ])}
    result = parse_art_settings(payload)
    assert result == ArtSettingsSnapshot(
        supported=frozenset(ArtSettingKey),
        brightness=7,
        color_temperature=-2,
        motion_timer="30",
        motion_sensitivity="2",
        brightness_sensor_enabled=True,
    )


def test_parse_valid_list_marks_missing_known_items_unsupported():
    result = parse_art_settings({"data": "[]"})
    assert result == ArtSettingsSnapshot()


@pytest.mark.parametrize("data", [None, 4, {}, "not-json", "{}"])
def test_parse_malformed_whole_settings_returns_none(data):
    assert parse_art_settings({"data": data}) is None


def test_parse_duplicate_setting_is_supported_but_value_unknown():
    payload = {"data": json.dumps([
        {"item": "brightness", "value": 6},
        {"item": "brightness", "value": 7},
    ])}
    result = parse_art_settings(payload)
    assert ArtSettingKey.BRIGHTNESS in result.supported
    assert result.brightness is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"value": "off", "type": "slideshow", "category_id": "MY-C0004"},
         SlideshowState(SlideshowMode.OFF, 0, "MY-C0004")),
        ({"value": "30", "type": "slideshow", "category_id": "MY-C0004"},
         SlideshowState(SlideshowMode.SEQUENTIAL, 30, "MY-C0004")),
        ({"value": 60, "type": "shuffleslideshow", "category_id": "MY-C0008"},
         SlideshowState(SlideshowMode.SHUFFLE, 60, "MY-C0008")),
    ],
)
def test_parse_slideshow(payload, expected):
    assert parse_slideshow(payload) == expected
```

Also parameterize invalid brightness outside `0..10`, color temperature outside
`-5..5`, timer outside the exact option set, sensitivity outside `1..3`, invalid
sensor booleans, non-positive slideshow durations, and unknown slideshow types;
the advertised setting remains in `supported` while its value becomes `None`.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_models.py tests/test_art_settings.py
```

Expected: collection fails because the models and parser module do not exist.

- [ ] **Step 4: Implement the models**

Add to `models.py`:

```python
class ArtSettingKey(StrEnum):
    BRIGHTNESS = "brightness"
    COLOR_TEMPERATURE = "color_temperature"
    MOTION_TIMER = "motion_timer"
    MOTION_SENSITIVITY = "motion_sensitivity"
    BRIGHTNESS_SENSOR = "brightness_sensor_setting"


class SlideshowMode(StrEnum):
    OFF = "off"
    SEQUENTIAL = "sequential"
    SHUFFLE = "shuffle"


@dataclass(frozen=True)
class ArtSettingsSnapshot:
    supported: frozenset[ArtSettingKey] = frozenset()
    brightness: int | None = None
    color_temperature: int | None = None
    motion_timer: str | None = None
    motion_sensitivity: str | None = None
    brightness_sensor_enabled: bool | None = None


@dataclass(frozen=True)
class SlideshowState:
    mode: SlideshowMode
    duration_minutes: int
    category_id: str | None = None
```

Append to `FrameData`:

```python
    art_settings: ArtSettingsSnapshot | None = None
    slideshow: SlideshowState | None = None
    optional_art_generation: int | None = None
```

- [ ] **Step 5: Implement strict pure parsers**

Create `art_settings.py` with the complete implementation:

```python
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
```

- [ ] **Step 6: Verify GREEN and commit**

Run the Task 1 command again. Expected: all focused tests pass.

Then run the complete suite:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
```

Expected: all existing and new tests pass before commit.

```bash
git add custom_components/samsungtv_frame/models.py \
  custom_components/samsungtv_frame/art_settings.py \
  tests/test_models.py tests/test_art_settings.py
git commit -m "feat: model local art settings state"
```

---

### Task 2: Protocol Adapter and Device Capability Cache

**Files:**
- Modify: `custom_components/samsungtv_frame/frame_art.py`
- Modify: `custom_components/samsungtv_frame/device.py`
- Modify: `tests/test_frame_art.py`
- Modify: `tests/test_device.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Produces: `FrameArt.get_art_settings_payload()`, `get_legacy_brightness()`,
  `get_legacy_color_temperature()`, `get_auto_rotation_status()`, and
  `get_legacy_slideshow_status()`.
- Produces: `FrameArt.set_motion_timer(value)`,
  `set_motion_sensitivity(value)`, and
  `set_brightness_sensor_setting(enabled)` with correlated live-verified
  acknowledgements.
- Produces: `FrameDevice.async_get_art_settings() -> ArtSettingsSnapshot | None` and `async_get_slideshow_state() -> SlideshowState | None`.
- Produces: matching USER-triggered async device setters.
- Keeps optional dialect decisions private and tagged with `art_generation`.
- Temporarily preserves `async_get_art_brightness()` and
  `async_get_color_temperature()` as projections for the pre-Task-3 coordinator.

- [ ] **Step 1: Write failing raw-adapter tests**

Add tests proving exact getter commands. Expectations are:

```python
await art.get_art_settings_payload()
art.request.assert_awaited_once_with("get_artmode_settings")

await art.get_auto_rotation_status()
art.request.assert_awaited_once_with("get_auto_rotation_status")

await art.get_legacy_slideshow_status()
art.request.assert_awaited_once_with("get_slideshow_status")

await art.get_legacy_brightness()
art.request.assert_awaited_once_with("get_brightness")

await art.get_legacy_color_temperature()
art.request.assert_awaited_once_with("get_color_temperature")
```

Replace the existing nested-setting and malformed-JSON fallback expectations with
raw-payload tests. Add setter tests using the exact sanitized LS03B terminal
payloads:

```python
{"event": "set_motion_timer", "request_id": "request-1", "value": "5"}
{"event": "set_motion_sensitivity", "request_id": "request-1", "value": "1"}
{"event": "set_brightness_sensor_setting", "request_id": "request-1", "value": "off"}
```

For each setter, assert the exact request name/value, response correlation, and
returned payload. Parameterize every invalid domain value to raise `ValueError`
before `request()` is awaited.

- [ ] **Step 2: Write failing device-boundary tests**

Add tests that establish:

```python
async def test_art_settings_aggregate_dialect_reuses_one_command_for_generation(device):
    device._art.get_art_settings_payload = AsyncMock(return_value=VALID_PAYLOAD)
    first = await device.async_get_art_settings()
    second = await device.async_get_art_settings()
    assert first == second == EXPECTED_SETTINGS
    assert device._art.get_art_settings_payload.await_count == 2
    device._art.get_legacy_brightness = AsyncMock()
    device._art.get_legacy_color_temperature = AsyncMock()
    device._art.get_legacy_brightness.assert_not_awaited()
    device._art.get_legacy_color_temperature.assert_not_awaited()


async def test_correlated_aggregate_response_error_uses_legacy_for_generation(device):
    device._art.get_art_settings_payload = AsyncMock(side_effect=ResponseError("unsupported"))
    device._art.get_legacy_brightness = AsyncMock(return_value="7")
    device._art.get_legacy_color_temperature = AsyncMock(return_value="-2")
    first = await device.async_get_art_settings()
    second = await device.async_get_art_settings()
    assert first == second
    device._art.get_art_settings_payload.assert_awaited_once()
    assert device._art.get_legacy_brightness.await_count == 2
    assert device._art.get_legacy_color_temperature.await_count == 2
```

Also test that malformed aggregate data leaves dialect unknown and retries aggregate
next time; transport failure calls `async_connection_failed` and performs no legacy
fallback; a generation change resets aggregate/slideshow dialect selection; modern
slideshow `ResponseError` probes legacy once; two correlated errors cache unsupported;
background reads never call `async_ensure_ready`; correlated legacy success with an
invalid value advertises support with value `None`; transport failure or generation
loss during either legacy getter returns overall `None` rather than an empty
unsupported snapshot; no dialect cache changes after generation loss; each setting
mutation ensures USER readiness exactly once; `ResponseError` leaves the session
healthy; and timeout/transport error calls `async_connection_failed` without retry.

- [ ] **Step 3: Run transport/device tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_frame_art.py tests/test_device.py
```

Expected: failures for missing getter/setter/device methods and legacy fallback
semantics.

- [ ] **Step 4: Implement the raw adapter**

Keep parsing outside the transport. Add:

```python
async def get_art_settings_payload(self) -> dict[str, Any]:
    return await self.request("get_artmode_settings")

async def get_auto_rotation_status(self) -> dict[str, Any]:
    return await self.request("get_auto_rotation_status")

async def get_legacy_slideshow_status(self) -> dict[str, Any]:
    return await self.request("get_slideshow_status")

async def get_legacy_brightness(self) -> Any:
    return (await self.request("get_brightness")).get("value")

async def get_legacy_color_temperature(self) -> Any:
    return (await self.request("get_color_temperature")).get("value")
```

Replace the current `get_artmode_settings(setting)`, `get_brightness()`, and
`get_color_temperature()` combination rather than retaining its implicit
aggregate/malformed-JSON fallback. Add the live-verified setters:

```python
async def set_motion_timer(self, value: str) -> dict[str, Any]:
    if value not in MOTION_TIMERS:
        raise ValueError("Invalid motion timer")
    return await self.request("set_motion_timer", value=value)

async def set_motion_sensitivity(self, value: str) -> dict[str, Any]:
    if value not in MOTION_SENSITIVITIES:
        raise ValueError("Invalid motion sensitivity")
    return await self.request("set_motion_sensitivity", value=value)

async def set_brightness_sensor_setting(
    self, enabled: bool
) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise ValueError("Brightness sensor state must be boolean")
    return await self.request(
        "set_brightness_sensor_setting", value="on" if enabled else "off"
    )
```

Import the exact domain constants from `art_settings.py`. The default request
correlator is sufficient because the observed response carries the matching
`request_id`; do not register or guess a UUID-less sub-event.

- [ ] **Step 5: Implement generation-scoped device reads**

Add private enums `_ArtSettingsDialect(UNKNOWN, AGGREGATE, LEGACY)` and
`_SlideshowDialect(UNKNOWN, AUTO_ROTATION, LEGACY, UNSUPPORTED)`, with a cached
generation. On generation change, reset both to unknown.

```python
class _ArtSettingsDialect(StrEnum):
    UNKNOWN = "unknown"
    AGGREGATE = "aggregate"
    LEGACY = "legacy"


class _SlideshowDialect(StrEnum):
    UNKNOWN = "unknown"
    AUTO_ROTATION = "auto_rotation"
    LEGACY = "legacy"
    UNSUPPORTED = "unsupported"
```

Use a sentinel so a correlated response containing an invalid/`None` value remains
distinguishable from session unavailability, timeout, disconnect, or generation
loss:

```python
_ART_READ_FAILED = object()


async def _async_art_read_response(
    self, operation: Callable[[], Awaitable[Any]]
) -> Any:
    if self._stopped or not self._art_session.ready:
        return _ART_READ_FAILED
    try:
        return await operation()
    except ResponseError:
        raise
    except Exception as err:  # noqa: BLE001
        await self._art_session.async_connection_failed(err)
        return _ART_READ_FAILED
```

Capture the generation before every sequence. Reset dialects only when that captured
generation changes. Every cache write requires both `art_ready` and equality with
the captured generation. The aggregate method follows:

```python
async def async_get_art_settings(self) -> ArtSettingsSnapshot | None:
    generation = self.art_generation
    self._reset_optional_dialects_for_generation(generation)
    if self._art_settings_dialect is _ArtSettingsDialect.LEGACY:
        return await self._async_get_legacy_art_settings(generation)
    try:
        payload = await self._async_art_read_response(self._art.get_art_settings_payload)
    except ResponseError:
        if not self._optional_generation_is_current(generation):
            return None
        self._art_settings_dialect = _ArtSettingsDialect.LEGACY
        return await self._async_get_legacy_art_settings(generation)
    if payload is _ART_READ_FAILED or not self._optional_generation_is_current(generation):
        return None
    snapshot = parse_art_settings(payload) if isinstance(payload, dict) else None
    if snapshot is not None and self._optional_generation_is_current(generation):
        self._art_settings_dialect = _ArtSettingsDialect.AGGREGATE
    return snapshot
```

`_async_get_legacy_art_settings(generation)` calls both direct legacy getters in
sequence. A correlated `ResponseError` means that key is absent. Any
`_ART_READ_FAILED` result or generation loss returns overall `None`. Any other
correlated response advertises the key in `supported`, even when
`normalize_art_setting()` returns `None`. Build one `ArtSettingsSnapshot` from those
supported keys/normalized values.

For this task only, preserve `async_get_art_brightness()` and
`async_get_color_temperature()` as projections of `async_get_art_settings()` so the
old coordinator remains green. Task 4 deletes them after the coordinator and number
entities have migrated.

- [ ] **Step 6: Implement exact slideshow dialect transitions**

Use this transition table, fencing every assignment with
`_optional_generation_is_current(generation)`:

```python
async def async_get_slideshow_state(self) -> SlideshowState | None:
    generation = self.art_generation
    self._reset_optional_dialects_for_generation(generation)
    dialect = self._slideshow_dialect
    if dialect is _SlideshowDialect.UNSUPPORTED:
        return None
    if dialect is _SlideshowDialect.LEGACY:
        getter = self._art.get_legacy_slideshow_status
    else:
        getter = self._art.get_auto_rotation_status
    try:
        payload = await self._async_art_read_response(getter)
    except ResponseError:
        if not self._optional_generation_is_current(generation):
            return None
        if dialect is _SlideshowDialect.LEGACY:
            self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
            return None
        try:
            payload = await self._async_art_read_response(
                self._art.get_legacy_slideshow_status
            )
        except ResponseError:
            if self._optional_generation_is_current(generation):
                self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
            return None
        dialect = _SlideshowDialect.LEGACY
    else:
        dialect = (
            _SlideshowDialect.LEGACY
            if dialect is _SlideshowDialect.LEGACY
            else _SlideshowDialect.AUTO_ROTATION
        )
    if payload is _ART_READ_FAILED or not self._optional_generation_is_current(generation):
        return None
    self._slideshow_dialect = dialect
    return parse_slideshow(payload) if isinstance(payload, dict) else None
```

Tests cover UNKNOWN→AUTO_ROTATION, UNKNOWN→LEGACY, UNKNOWN→UNSUPPORTED,
LEGACY reuse, a correlated malformed modern/legacy payload retaining the newly
proven dialect while returning unknown state, transport failure retaining capability
unknown, and generation loss preventing every cache write.

Add device setters that perform exactly one existing USER mutation:

```python
async def async_set_motion_timer(self, value: str) -> None:
    await self._async_art_mutation(lambda: self._art.set_motion_timer(value))

async def async_set_motion_sensitivity(self, value: str) -> None:
    await self._async_art_mutation(
        lambda: self._art.set_motion_sensitivity(value)
    )

async def async_set_brightness_sensor(self, enabled: bool) -> None:
    await self._async_art_mutation(
        lambda: self._art.set_brightness_sensor_setting(enabled)
    )
```

- [ ] **Step 7: Extend explicit test mocks, verify GREEN, and commit**

Add explicit `AsyncMock` methods for both optional getters and all three setters to
`tests/conftest.py`; add plain non-truthy values for any new readiness or capability
attribute.

Run the Task 2 command. Expected: all transport/device tests pass with no leaked
task/socket warnings.

Run the complete suite command from Task 1. Expected: all tests pass before commit.

```bash
git add custom_components/samsungtv_frame/frame_art.py \
  custom_components/samsungtv_frame/device.py tests/test_frame_art.py \
  tests/test_device.py tests/conftest.py
git commit -m "feat: read frame art settings and slideshow state"
```

---

### Task 3: Generation-Fenced Coordinator State and Authoritative Refresh

**Files:**
- Modify: `custom_components/samsungtv_frame/coordinator.py`
- Modify: `tests/test_coordinator.py`
- Modify: `tests/test_number.py`

**Interfaces:**
- Consumes: Task 2 device getters and Task 1 snapshots.
- Produces: `FrameCoordinator.async_request_art_reconcile() -> None`.
- Publishes `FrameData.art_settings` and `FrameData.slideshow` only for a current READY generation.

- [ ] **Step 1: Write failing aggregate-reconciliation tests**

Replace per-field getter expectations with one settings snapshot and add tests for:

```python
async def test_reconcile_reads_one_settings_snapshot_and_one_slideshow(hass, mock_device):
    mock_device.art_ready = True
    mock_device.art_generation = 4
    mock_device.async_get_art_settings.return_value = SETTINGS
    mock_device.async_get_slideshow_state.return_value = SLIDESHOW
    coord = _make(hass, mock_device)
    data = await coord._async_poll()
    assert data.art_settings is SETTINGS
    assert data.slideshow is SLIDESHOW
    mock_device.async_get_art_settings.assert_awaited_once()
    mock_device.async_get_slideshow_state.assert_awaited_once()
```

Cover generation loss after each optional await, five-minute cadence without
heartbeat reads, malformed/unsupported optional values leaving `tv_mode` unchanged,
and OFF publishing both snapshots plus `optional_art_generation` as `None`.

Add a READY-transition test where `coord.data` contains generation-1 snapshots,
`mock_device.art_generation` becomes `2`, and the scheduled READY refresh is blocked.
Assert `coord.data.optional_art_generation != mock_device.art_generation` during that
interval, so entities and diagnostics cannot expose generation-1 values.

Update `tests/test_number.py` setup to feed one `ArtSettingsSnapshot` through
`mock_device.async_get_art_settings`; existing brightness/color state assertions must
remain green through the temporary FrameData projections.

- [ ] **Step 2: Write failing push-preservation and forced-refresh tests**

Add a `FrameData` containing both snapshots, send `art_mode_changed`,
`image_selected`, and `slideshow_image_changed`, and assert the snapshots remain
object-identical afterward.

Use real coordinator refreshes rather than mocking `async_refresh()`:

```python
async def test_two_back_to_back_art_reconciles_bypass_request_debounce(hass, mock_device):
    mock_device.art_ready = True
    mock_device.art_generation = 1
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_art_settings.side_effect = [SETTINGS_ONE, SETTINGS_TWO]
    mock_device.async_get_slideshow_state.side_effect = [SLIDESHOW_ONE, SLIDESHOW_TWO]
    coord = _make(hass, mock_device)
    coord._clock = lambda: 0.0
    await coord.async_request_art_reconcile()
    await coord.async_request_art_reconcile()
    assert mock_device.async_get_art_settings.await_count == 2
    assert mock_device.async_get_slideshow_state.await_count == 2
    assert coord.data.art_settings is SETTINGS_TWO
    assert coord.data.slideshow is SLIDESHOW_TWO
    assert coord._next_art_reconcile == ART_RECONCILE_SECONDS
```

This proves both back-to-back calls actually reread optional Art state within one
300-second window; a mocked `async_refresh()` call count is insufficient.

- [ ] **Step 3: Run coordinator tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_coordinator.py tests/test_number.py
```

Expected: failures for missing snapshots/device calls/direct refresh and push field
loss.

- [ ] **Step 4: Implement one atomic optional snapshot path**

Replace `_art_brightness` and `_art_color_temp` with `_art_settings`, `_slideshow`,
and `_optional_art_generation`. Commit the optional snapshot only after both reads
complete on the same READY generation:

```python
art_settings = await self.device.async_get_art_settings()
if not self._art_session_is_ready(generation):
    return (
        art_mode is not None
        or self._art_live_push_revision != live_push_revision
    )
slideshow = await self.device.async_get_slideshow_state()
if self._art_session_is_ready(generation):
    self._art_settings = art_settings
    self._slideshow = slideshow
    self._optional_art_generation = generation
```

Always finish optional reads after a live Art-mode sample, but never use their
failure to update `_art_fail_streak`. Publish `None` for optional fields when OFF,
not READY, or not fresh for the current generation. Until Task 4 migrates the two
existing number entities, populate their compatibility projections from the same
snapshot only:

```python
art_brightness=(
    published_settings.brightness if published_settings is not None else None
),
art_color_temperature=(
    published_settings.color_temperature if published_settings is not None else None
),
art_settings=published_settings,
slideshow=published_slideshow,
optional_art_generation=(
    self._optional_art_generation if optional_fresh and not is_off else None
),
```

`optional_fresh` requires device READY and
`_optional_art_generation == device.art_generation`.
`published_settings`/`published_slideshow` use the caches only when
`optional_fresh and not is_off`. A valid current generation with unknown settings
may still publish `art_settings=None`; retain the generation separately so
diagnostics can distinguish current-unknown from stale.

- [ ] **Step 5: Preserve all fields on push and add direct reconcile**

Import `replace` from `dataclasses` and replace the hand-built `FrameData` push
update with:

```python
base = current or FrameData(True, power_state, self._art_mode, mode)
self.async_set_updated_data(replace(
    base,
    reachable=True,
    power_state=power_state,
    art_mode=self._art_mode,
    tv_mode=mode,
    current_art=self._current_art,
))
```

Add:

```python
async def async_request_art_reconcile(self) -> None:
    self._next_art_reconcile = 0.0
    await self.async_refresh()
```

- [ ] **Step 6: Verify GREEN and commit**

Run the Task 3 command. Expected: all coordinator tests pass.

Run the complete suite command from Task 1. Expected: all tests pass before commit.

```bash
git add custom_components/samsungtv_frame/coordinator.py \
  tests/test_coordinator.py tests/test_number.py
git commit -m "feat: reconcile optional art state atomically"
```

---

### Task 4: Home Assistant Entity Surfaces

**Files:**
- Modify: `custom_components/samsungtv_frame/const.py`
- Modify: `custom_components/samsungtv_frame/entity.py`
- Modify: `custom_components/samsungtv_frame/models.py`
- Modify: `custom_components/samsungtv_frame/coordinator.py`
- Modify: `custom_components/samsungtv_frame/device.py`
- Create: `custom_components/samsungtv_frame/select.py`
- Modify: `custom_components/samsungtv_frame/number.py`
- Modify: `custom_components/samsungtv_frame/switch.py`
- Modify: `custom_components/samsungtv_frame/sensor.py`
- Modify: `custom_components/samsungtv_frame/media_player.py`
- Modify: `custom_components/samsungtv_frame/strings.json`
- Modify: `custom_components/samsungtv_frame/translations/en.json`
- Create: `tests/test_select.py`
- Modify: `tests/test_models.py`, `tests/test_device.py`,
  `tests/test_coordinator.py`, `tests/test_number.py`, `tests/test_switch.py`,
  `tests/test_sensor.py`, `tests/test_media_player.py`, `tests/test_init.py`

**Interfaces:**
- Consumes: `FrameData.art_settings`, `FrameData.slideshow`, Task 2 device setters, and `async_request_art_reconcile()`.
- Produces: `FrameArtSleepAfterSelect`, `FrameArtMotionSensitivitySelect`, `FrameBrightnessSensorSwitch`, `FrameSlideshowSensor`.

- [ ] **Step 1: Write failing platform/entity tests**

Add `Platform.SELECT` setup coverage and assert stable IDs:

```python
assert hass.states.get("select.samsung_frame_tv_art_sleep_after").state == "30"
assert hass.states.get("select.samsung_frame_tv_art_motion_sensitivity").state == "2"
assert hass.states.get("switch.samsung_frame_tv_art_brightness_sensor").state == "on"
assert hass.states.get("sensor.samsung_frame_tv_art_slideshow").state == "shuffle"
```

Assert select options are the exact stable wire keys, entity registry category is
`EntityCategory.CONFIG`, and slideshow attributes are exactly
`duration_minutes`/`category_id`.

For every optional setting, test supported, unsupported, malformed, TV OFF, and
Art-unready availability. Add a generation-staleness case where
`data.optional_art_generation != device.art_generation`. Change existing
brightness/color OFF tests from `unknown` to `unavailable`.

- [ ] **Step 2: Write failing mutation/readback tests**

For each evidence-backed setter, invoke the entity service, assert the exact device
call, and assert `async_request_art_reconcile()` is awaited. Patch that method and
invoke two sequential mutations to prove both calls bypass the ordinary request
debouncer. Assert generic `HomeAssistantError` text contains no requested value.

Update brightness, color-temperature, and slideshow service tests to assert the same
direct Art reconcile method.

- [ ] **Step 3: Run entity tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_select.py tests/test_number.py \
  tests/test_switch.py tests/test_sensor.py tests/test_media_player.py \
  tests/test_init.py tests/test_models.py tests/test_device.py \
  tests/test_coordinator.py
```

Expected: collection/setup/state failures for the new platform/entities and stale
refresh behavior.

- [ ] **Step 4: Add shared optional availability logic**

Add shared freshness plus setting-specific helpers to `entity.py`:

```python
def optional_art_state_fresh(coordinator: FrameCoordinator) -> bool:
    return (
        coordinator.device.art_ready
        and coordinator.data.optional_art_generation
        == coordinator.device.art_generation
        and coordinator.data.tv_mode in (TvMode.WATCHING, TvMode.ART_MODE)
    )


def art_setting_available(
    coordinator: FrameCoordinator,
    key: ArtSettingKey,
    value: object | None,
) -> bool:
    settings = coordinator.data.art_settings
    return (
        optional_art_state_fresh(coordinator)
        and settings is not None
        and key in settings.supported
        and value is not None
    )
```

Each entity's `available` property combines `super().available` with the helper;
for example brightness uses:

```python
settings = self.coordinator.data.art_settings
value = settings.brightness if settings is not None else None
return super().available and art_setting_available(
    self.coordinator, ArtSettingKey.BRIGHTNESS, value
)
```

Use the corresponding enum/field pairs for color temperature, motion timer, motion
sensitivity, and brightness sensor. Existing brightness/color values project from
`data.art_settings` and their setters await `async_request_art_reconcile()`.

The slideshow sensor uses the same freshness guard:

```python
@property
def available(self) -> bool:
    return (
        super().available
        and optional_art_state_fresh(self.coordinator)
        and self.coordinator.data.slideshow is not None
    )
```

Its tests cover TV OFF, session unready, unknown slideshow state, and a stale
`optional_art_generation` before the new READY refresh completes.

During Tasks 1–3, retain the old defaulted `FrameData.art_brightness` and
`art_color_temperature` fields as projections so existing platform tests remain
green. In this task, after migrating both number entities, delete those two fields
from `FrameData`, delete their coordinator projections, and update all test fixtures
to use `ArtSettingsSnapshot`. The finished tree has one canonical settings state.
Delete the transitional `FrameDevice.async_get_art_brightness()` and
`async_get_color_temperature()` projections and their direct tests in the same
change; the coordinator no longer consumes them after Task 3.

- [ ] **Step 5: Implement selects, switch, and sensor**

Add `Platform.SELECT`. Both selects use `EntityCategory.CONFIG`, translation keys,
stable unique-ID suffixes, and fixed options. Brightness sensor joins the existing
Art-mode switch in `switch.py`. Slideshow is a read-only ENUM sensor with:

```python
_attr_device_class = SensorDeviceClass.ENUM
_attr_options = list(SlideshowMode)

@property
def extra_state_attributes(self):
    state = self.coordinator.data.slideshow
    if state is None:
        return None
    return {
        "duration_minutes": state.duration_minutes,
        "category_id": state.category_id,
    }
```

Use neutral sensitivity states `1`, `2`, `3` unless Task 2 captured a verified
semantic mapping. Do not dynamically add unknown protocol values as options.

- [ ] **Step 6: Add translations and service refresh**

Add matching entity translation keys to both JSON files. Translate Sleep After
state keys as Off, 5 minutes, 15 minutes, 30 minutes, 1 hour, 2 hours, 4 hours;
translate slideshow states Off, Sequential, Shuffle. Keep neutral sensitivity
labels Low/Medium/High out unless their mapping was verified.

After a successful slideshow service mutation, await
`coordinator.async_request_art_reconcile()`.

- [ ] **Step 7: Verify GREEN and commit**

Run the Task 4 command. Expected: all entity/setup tests pass.

Run the complete suite command from Task 1. Expected: all tests pass before commit.

```bash
git add custom_components/samsungtv_frame/const.py \
  custom_components/samsungtv_frame/entity.py \
  custom_components/samsungtv_frame/models.py \
  custom_components/samsungtv_frame/coordinator.py \
  custom_components/samsungtv_frame/device.py \
  custom_components/samsungtv_frame/select.py \
  custom_components/samsungtv_frame/number.py \
  custom_components/samsungtv_frame/switch.py \
  custom_components/samsungtv_frame/sensor.py \
  custom_components/samsungtv_frame/media_player.py \
  custom_components/samsungtv_frame/strings.json \
  custom_components/samsungtv_frame/translations/en.json \
  tests/test_select.py tests/test_number.py tests/test_switch.py \
  tests/test_sensor.py tests/test_media_player.py tests/test_init.py \
  tests/test_models.py tests/test_device.py tests/test_coordinator.py
git commit -m "feat: expose local frame art settings"
```

---

### Task 5: Strictly Allowlisted Diagnostics

**Files:**
- Modify: `custom_components/samsungtv_frame/coordinator.py`
- Create: `custom_components/samsungtv_frame/diagnostics.py`
- Modify: `tests/test_coordinator.py`
- Create: `tests/test_diagnostics.py`

**Interfaces:**
- Produces: `FrameCoordinator.diagnostics_snapshot() -> dict[str, Any]`.
- Produces: `async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]`.

- [ ] **Step 1: Write failing coordinator allowlist tests**

Construct current and stale-generation `FrameData` snapshots and assert the exact
safe keys/values returned by `diagnostics_snapshot()`. The stale case sets
`optional_art_generation != device.art_generation` and must report no supported
settings and `slideshow_known is False`. Assert the method performs no device call
and contains no current-art, running-app, volume, mute, host, MAC, or token field.

- [ ] **Step 2: Write failing adapter privacy and zero-I/O tests**

Create a loaded config entry whose host, MAC, token, title, current art, running app,
options, and arbitrary private attributes contain unique canary strings. Patch every
device coroutine to raise if awaited. Serialize diagnostics and assert none of the
canaries occur:

```python
result = await async_get_config_entry_diagnostics(hass, entry)
serialized = json.dumps(result, sort_keys=True)
for canary in PRIVATE_CANARIES:
    assert canary not in serialized
assert result["loaded"] is True
```

Add an unloaded-entry test asserting the complete result equals:

```python
{"loaded": False, "model": "QE65LS03BAUXXH", "heartbeat_seconds": 10}
```

The test must also reject an arbitrary unallowlisted option canary.

- [ ] **Step 3: Run diagnostics tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_diagnostics.py tests/test_coordinator.py
```

Expected: collection fails because `diagnostics.py` and
`FrameCoordinator.diagnostics_snapshot()` do not exist.

- [ ] **Step 4: Implement the coordinator snapshot and allowlist adapter**

Add a coordinator method returning only safe scalar/enum fields named in the
design. Convert enums to `.value`, setting names to a sorted list, and expose only
boolean knowledge for Art/slideshow—not current values or content IDs. Optional
capability/state is known only when `data.optional_art_generation` equals the live
device generation. Do not call the device or inspect arbitrary attributes.

Use only `CONF_MODEL`, `OPT_HEARTBEAT`, `DEFAULT_HEARTBEAT_SECONDS`, and the explicit
coordinator snapshot:

```python
async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FrameConfigEntry
) -> dict[str, Any]:
    del hass
    coordinator = getattr(entry, "runtime_data", None)
    result = {
        "loaded": coordinator is not None,
        "model": entry.data.get(CONF_MODEL, "The Frame"),
        "heartbeat_seconds": entry.options.get(
            OPT_HEARTBEAT, DEFAULT_HEARTBEAT_SECONDS
        ),
    }
    if not result["loaded"]:
        return result
    return {**result, **coordinator.diagnostics_snapshot()}
```

Do not call HA's broad redaction helper because unrecognized entry fields must never
enter the result in the first place.

- [ ] **Step 5: Verify GREEN and commit**

Run the Task 5 command. Expected: all diagnostics tests pass and no device mock was
awaited.

Run the complete suite command from Task 1. Expected: all tests pass before commit.

```bash
git add custom_components/samsungtv_frame/coordinator.py \
  custom_components/samsungtv_frame/diagnostics.py \
  tests/test_coordinator.py tests/test_diagnostics.py
git commit -m "feat: add private frame diagnostics"
```

---

### Task 6: User Documentation, Regression, and Review Gates

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Review fixes may modify only files already listed in Tasks 1–5; expanding the file
  set requires revising this plan before editing.

**Interfaces:**
- Consumes: all completed behavior from Tasks 1–5.
- Produces: documented local-only entities and an evidence-backed release candidate branch; no production deployment.

- [ ] **Step 1: Document the shipped behavior**

Add an `Unreleased` changelog section describing aggregate local Art settings,
slideshow readback, diagnostics, and the intentional brightness/color availability
change. In README Entities, list exact new entity domains/names and explain that
unavailable means the TV/session/feature is not currently authoritative. Document
that the existing slideshow service remains the atomic write surface.

Document all three live-verified local controls and do not bump the manifest version.

- [ ] **Step 2: Run focused suites for every touched boundary**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_art_settings.py tests/test_models.py \
  tests/test_frame_art.py tests/test_device.py tests/test_coordinator.py \
  tests/test_select.py tests/test_number.py tests/test_switch.py \
  tests/test_sensor.py tests/test_media_player.py tests/test_init.py \
  tests/test_diagnostics.py
```

Expected: all focused tests pass with no pending-task, unclosed-socket, or coroutine
warning.

- [ ] **Step 3: Run complete regression and static checks**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
git diff --check origin/main...HEAD
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/python -m compileall -q \
  custom_components/samsungtv_frame
uvx ruff check custom_components tests
```

Expected: full pytest passes, `git diff --check` prints nothing, and compileall exits
zero, and Ruff reports no violations. Remove only generated `__pycache__`
directories under the writable `/tmp` worktree before the final status check.

- [ ] **Step 4: Run privacy and secret scans**

```bash
rg -n '1\.2\.3\.4|A0:D0:5B:86:CE:B7|token|content_id|running_app' \
  custom_components/samsungtv_frame/diagnostics.py tests/test_diagnostics.py
```

Expected: production diagnostics contains only forbidden-field assertions/comments or
constant imports required by tests; manually verify no real IP, MAC, token, or private
HA/TV data exists anywhere in the branch diff.

- [ ] **Step 5: Obtain two independent final reviews**

Give the complete `origin/main...HEAD` diff and the approved spec to:

1. a fresh Terra subagent for spec conformance, regression risk, and test adequacy;
2. a fresh Fable `claude-lesina` process for adversarial protocol/privacy/code review.

Evaluate every finding against the repository. Fix blocking findings test-first and
repeat the affected focused/full checks. Reviewer approval without passing local
verification is not sufficient.

- [ ] **Step 6: Commit documentation or verified review fixes**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: describe local frame art settings"
```

If review fixes changed code, commit each independently verified fix with a specific
`fix:` message before this documentation commit.

- [ ] **Step 7: Final no-production handoff**

Confirm `git status --short --branch` is clean, report exact commands/results, and
provide the branch/commit list. Do not push, deploy, reload, or modify the production
N150.
