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
- Use neutral sensitivity options `1`, `2`, `3` unless direct LS03B evidence proves the semantic low/medium/high mapping.
- Diagnostics perform no I/O and never include host, MAC, token, entry identifiers/title, content IDs, app names, volume/mute, raw payloads, exception text, or arbitrary options/private attributes.
- Preserve existing entity unique IDs and config-entry schema/version. Add `Platform.SELECT`; diagnostics are auto-discovered and are not a platform.
- Follow strict red-green-refactor: observe each production behavior test fail before implementing it.
- Call external Claude only through the `claude-lesina` shell function. Prefer Fable for plan/code review; use Terra for independent architecture/spec-conformance review.
- Do not deploy, reload new code, or run acceptance against the production N150. A setter protocol probe may temporarily unload/reload the already-installed integration only if needed to obtain sanitized acknowledgement evidence and must restore every setting; it must not copy or activate branch code.
- If deterministic setter acknowledgement cannot be established without deploying branch code, defer the affected writable entity while retaining its read-only state; do not guess an acknowledgement schema.

---

## File Map

- Create `custom_components/samsungtv_frame/art_settings.py`: pure parsing and normalization for aggregate Art settings and slideshow payloads.
- Modify `custom_components/samsungtv_frame/models.py`: immutable Art-settings/slideshow enums and snapshots carried by `FrameData`.
- Modify `custom_components/samsungtv_frame/frame_art.py`: raw aggregate/slideshow getters and domain-validated setting setters.
- Modify `custom_components/samsungtv_frame/device.py`: background-only optional reads, generation-scoped dialect caches, and USER-triggered mutations.
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


def _normalize_setting(key: ArtSettingKey, value: Any) -> int | str | bool | None:
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
            normalized[key] = _normalize_setting(key, item.get("value"))

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

```bash
git add custom_components/samsungtv_frame/models.py \
  custom_components/samsungtv_frame/art_settings.py \
  tests/test_models.py tests/test_art_settings.py
git commit -m "feat: model local art settings state"
```

---

### Task 2: Protocol Adapter, Setter Evidence, and Device Capability Cache

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
- Produces only after evidence: `set_motion_timer(value)`, `set_motion_sensitivity(value)`, `set_brightness_sensor_setting(enabled)`.
- Produces: `FrameDevice.async_get_art_settings() -> ArtSettingsSnapshot | None` and `async_get_slideshow_state() -> SlideshowState | None`.
- Produces only after evidence: matching async device setters.
- Keeps optional dialect decisions private and tagged with `art_generation`.

- [ ] **Step 1: Capture the setter acknowledgement release evidence**

Without copying or loading branch code into Home Assistant, use the existing
installed v0.6.9 protocol stack in an isolated direct session. Temporarily unload
the installed Samsung integration only if required to avoid two Art receivers.
For each of motion timer, sensitivity, and brightness sensor:

1. read and record the original value;
2. send one different valid value with a unique request ID;
3. capture only the response event name, correlation field name, and whether its
   payload confirms the value—never IP, MAC, token, artwork, or HA data;
4. read back the value;
5. restore the original value and read it back;
6. close the direct session and reload the unchanged installed integration.

Store only sanitized response shapes as test fixtures. Replace the real request ID
with the literal `request-1`; retain only the observed terminal event name,
correlation-field name, and value-confirmation fields required by the test.

If a deterministic response is not observed, record that result and plan the
affected setter as a serialized send-and-authoritative-readback operation. If that
also cannot be proven without branch deployment, omit the device setter and make
that entity read-only for this increment.

- [ ] **Step 2: Write failing raw-adapter tests**

Add tests proving exact getter commands, domain validation, and the observed
acknowledgement behavior. Getter expectations are:

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

For each proven setter, assert the exact request name/value and parameterize every
invalid domain value to raise `ValueError` before `request()` is awaited. Replace
the existing malformed-JSON fallback expectation with a test that malformed
aggregate data does not call `get_brightness`/`get_color_temperature`.

- [ ] **Step 3: Write failing device-boundary tests**

Add tests that establish:

```python
async def test_art_settings_aggregate_success_is_cached_for_generation(device):
    device._art.get_art_settings_payload = AsyncMock(return_value=VALID_PAYLOAD)
    first = await device.async_get_art_settings()
    second = await device.async_get_art_settings()
    assert first == second == EXPECTED_SETTINGS
    assert device._art.get_art_settings_payload.await_count == 2
    device._art.get_brightness.assert_not_awaited()


async def test_correlated_aggregate_response_error_uses_legacy_for_generation(device):
    device._art.get_art_settings_payload = AsyncMock(side_effect=ResponseError("unsupported"))
    device._art.get_brightness = AsyncMock(return_value="7")
    device._art.get_color_temperature = AsyncMock(return_value="-2")
    first = await device.async_get_art_settings()
    second = await device.async_get_art_settings()
    assert first == second
    device._art.get_art_settings_payload.assert_awaited_once()
    assert device._art.get_brightness.await_count == 2
    assert device._art.get_color_temperature.await_count == 2
```

Also test that malformed aggregate data leaves dialect unknown and retries aggregate
next time; transport failure calls `async_connection_failed` and performs no legacy
fallback; a generation change resets aggregate/slideshow dialect selection; modern
slideshow `ResponseError` probes legacy once; two correlated errors cache unsupported;
background reads never call `async_ensure_ready`; and mutations call USER readiness
once without automatic retry.

- [ ] **Step 4: Run transport/device tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_frame_art.py tests/test_device.py
```

Expected: failures for missing getter/setter/device methods and legacy fallback
semantics.

- [ ] **Step 5: Implement the raw adapter**

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
aggregate/malformed-JSON fallback. Implement only evidence-backed setter
correlation. Validate values before calling `request()`. A timeout uses existing
request cleanup/close behavior and is never retried or converted to success.

- [ ] **Step 6: Implement generation-scoped device reads**

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

The strict read helper is:

```python
async def _async_art_read_response(
    self, operation: Callable[[], Awaitable[Any]]
) -> Any:
    if self._stopped or not self._art_session.ready:
        return None
    try:
        return await operation()
    except ResponseError:
        raise
    except Exception as err:  # noqa: BLE001
        await self._art_session.async_connection_failed(err)
        return None
```

Use a strict optional read helper that re-raises `ResponseError`, returns `None` for
malformed parser results, and calls `async_connection_failed` only for transport
exceptions. The aggregate method follows:

```python
async def async_get_art_settings(self) -> ArtSettingsSnapshot | None:
    self._reset_optional_dialects_for_generation()
    if self._art_settings_dialect is _ArtSettingsDialect.LEGACY:
        return await self._async_get_legacy_art_settings()
    try:
        payload = await self._async_art_read_response(self._art.get_art_settings_payload)
    except ResponseError:
        self._art_settings_dialect = _ArtSettingsDialect.LEGACY
        return await self._async_get_legacy_art_settings()
    snapshot = parse_art_settings(payload) if payload is not None else None
    if snapshot is not None:
        self._art_settings_dialect = _ArtSettingsDialect.AGGREGATE
    return snapshot
```

The legacy snapshot advertises only brightness/color keys whose direct
`get_legacy_brightness()`/`get_legacy_color_temperature()` calls return valid
values. Catch each legacy `ResponseError` independently without marking the session
failed. Implement slideshow dialect selection using the same rules and
`parse_slideshow`.

Delete independent public brightness/color read paths or make them projections of
`async_get_art_settings`; never issue another aggregate settings request for each
field.

- [ ] **Step 7: Extend explicit test mocks, verify GREEN, and commit**

Add explicit `AsyncMock` methods for both optional getters and every evidence-backed
setter to `tests/conftest.py`; add plain non-truthy values for any new readiness or
capability attribute.

Run the Task 2 command. Expected: all transport/device tests pass with no leaked
task/socket warnings.

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

**Interfaces:**
- Consumes: Task 2 device getters and Task 1 snapshots.
- Produces: `FrameCoordinator.async_request_art_reconcile() -> None`.
- Produces: `FrameCoordinator.diagnostics_snapshot() -> dict[str, Any]`.
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

Cover generation loss after each optional await, READY generation change invalidating
published optional values, five-minute cadence without heartbeat reads, malformed/
unsupported optional values leaving `tv_mode` unchanged, and OFF publishing both
snapshots as `None`.

- [ ] **Step 2: Write failing push-preservation and forced-refresh tests**

Add a `FrameData` containing both snapshots, send `art_mode_changed`,
`image_selected`, and `slideshow_image_changed`, and assert the snapshots remain
object-identical afterward.

For direct refresh:

```python
async def test_two_back_to_back_art_reconciles_bypass_request_debounce(hass, mock_device):
    coord = _make(hass, mock_device)
    with patch.object(coord, "async_refresh", AsyncMock()) as refresh:
        await coord.async_request_art_reconcile()
        await coord.async_request_art_reconcile()
    assert refresh.await_count == 2
    assert coord._next_art_reconcile == 0
```

Also assert `async_request_refresh` was never called by this method.

- [ ] **Step 3: Run coordinator tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_coordinator.py
```

Expected: failures for missing snapshots/device calls/direct refresh and push field
loss.

- [ ] **Step 4: Implement one atomic optional snapshot path**

Replace `_art_brightness` and `_art_color_temp` with `_art_settings` and
`_slideshow`, plus the generation that made them fresh. During reconciliation:

```python
art_settings = await self.device.async_get_art_settings()
settings_valid = self._art_session_is_ready(generation)
slideshow = None
slideshow_valid = False
if settings_valid:
    slideshow = await self.device.async_get_slideshow_state()
    slideshow_valid = self._art_session_is_ready(generation)
if settings_valid:
    self._art_settings = art_settings
if slideshow_valid:
    self._slideshow = slideshow
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
```

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

- [ ] **Step 6: Add an explicit diagnostics snapshot**

Return a new dictionary containing only safe scalar/enum fields named in the
design. Convert enums to `.value`, setting names to a sorted list, and expose only
boolean knowledge for Art/slideshow—not current values or content IDs. Do not call
the device or inspect arbitrary attributes.

- [ ] **Step 7: Verify GREEN and commit**

Run the Task 3 command. Expected: all coordinator tests pass.

```bash
git add custom_components/samsungtv_frame/coordinator.py tests/test_coordinator.py
git commit -m "feat: reconcile optional art state atomically"
```

---

### Task 4: Home Assistant Entity Surfaces

**Files:**
- Modify: `custom_components/samsungtv_frame/const.py`
- Modify: `custom_components/samsungtv_frame/entity.py`
- Modify: `custom_components/samsungtv_frame/models.py`
- Modify: `custom_components/samsungtv_frame/coordinator.py`
- Create: `custom_components/samsungtv_frame/select.py`
- Modify: `custom_components/samsungtv_frame/number.py`
- Modify: `custom_components/samsungtv_frame/switch.py`
- Modify: `custom_components/samsungtv_frame/sensor.py`
- Modify: `custom_components/samsungtv_frame/media_player.py`
- Modify: `custom_components/samsungtv_frame/strings.json`
- Modify: `custom_components/samsungtv_frame/translations/en.json`
- Create: `tests/test_select.py`
- Modify: `tests/test_number.py`, `tests/test_switch.py`, `tests/test_sensor.py`, `tests/test_media_player.py`, `tests/test_init.py`

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
Art-unready availability. Change existing brightness/color OFF tests from
`unknown` to `unavailable`.

- [ ] **Step 2: Write failing mutation/readback tests**

For each evidence-backed setter, invoke the entity service, assert the exact device
call, and assert `async_request_art_reconcile()` is awaited. Patch that method and
invoke two sequential mutations to prove both calls bypass the ordinary request
debouncer. Assert generic `HomeAssistantError` text contains no requested value.

Update brightness, color-temperature, and slideshow service tests to assert the same
direct Art reconcile method. A setter deferred by Task 2 must expose no callable
write method/service; its state remains read-only.

- [ ] **Step 3: Run entity tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_select.py tests/test_number.py \
  tests/test_switch.py tests/test_sensor.py tests/test_media_player.py tests/test_init.py
```

Expected: collection/setup/state failures for the new platform/entities and stale
refresh behavior.

- [ ] **Step 4: Add shared optional availability logic**

Add this helper to `entity.py`:

```python
def art_setting_available(
    coordinator: FrameCoordinator,
    key: ArtSettingKey,
    value: object | None,
) -> bool:
    settings = coordinator.data.art_settings
    return (
        coordinator.device.art_ready
        and coordinator.data.tv_mode in (TvMode.WATCHING, TvMode.ART_MODE)
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

During Tasks 1–3, retain the old defaulted `FrameData.art_brightness` and
`art_color_temperature` fields as projections so existing platform tests remain
green. In this task, after migrating both number entities, delete those two fields
from `FrameData`, delete their coordinator projections, and update all test fixtures
to use `ArtSettingsSnapshot`. The finished tree has one canonical settings state.

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

```bash
git add custom_components/samsungtv_frame/const.py \
  custom_components/samsungtv_frame/entity.py \
  custom_components/samsungtv_frame/models.py \
  custom_components/samsungtv_frame/coordinator.py \
  custom_components/samsungtv_frame/select.py \
  custom_components/samsungtv_frame/number.py \
  custom_components/samsungtv_frame/switch.py \
  custom_components/samsungtv_frame/sensor.py \
  custom_components/samsungtv_frame/media_player.py \
  custom_components/samsungtv_frame/strings.json \
  custom_components/samsungtv_frame/translations/en.json \
  tests/test_select.py tests/test_number.py tests/test_switch.py \
  tests/test_sensor.py tests/test_media_player.py tests/test_init.py
git commit -m "feat: expose local frame art settings"
```

---

### Task 5: Strictly Allowlisted Diagnostics

**Files:**
- Create: `custom_components/samsungtv_frame/diagnostics.py`
- Create: `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: `FrameCoordinator.diagnostics_snapshot()` from Task 3.
- Produces: `async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]`.

- [ ] **Step 1: Write failing privacy and zero-I/O tests**

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

- [ ] **Step 2: Run diagnostics tests and verify RED**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_diagnostics.py
```

Expected: collection fails because `diagnostics.py` does not exist.

- [ ] **Step 3: Implement the allowlist adapter**

Use only `CONF_MODEL`, `OPT_HEARTBEAT`, `DEFAULT_HEARTBEAT_SECONDS`, and the explicit
coordinator snapshot:

```python
async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FrameConfigEntry
) -> dict[str, Any]:
    del hass
    result = {
        "loaded": hasattr(entry, "runtime_data"),
        "model": entry.data.get(CONF_MODEL, "The Frame"),
        "heartbeat_seconds": entry.options.get(
            OPT_HEARTBEAT, DEFAULT_HEARTBEAT_SECONDS
        ),
    }
    if not result["loaded"]:
        return result
    return {**result, **entry.runtime_data.diagnostics_snapshot()}
```

Do not call HA's broad redaction helper because unrecognized entry fields must never
enter the result in the first place.

- [ ] **Step 4: Verify GREEN and commit**

Run the Task 5 command. Expected: all diagnostics tests pass and no device mock was
awaited.

```bash
git add custom_components/samsungtv_frame/diagnostics.py tests/test_diagnostics.py
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

Do not claim any setter that Task 2 deferred and do not bump the manifest version.

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
```

Expected: full pytest passes, `git diff --check` prints nothing, and compileall exits
zero. Remove only generated `__pycache__` directories under the writable `/tmp`
worktree before the final status check.

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

Confirm `git status --short --branch` is clean, report exact commands/results and any
deferred setters, and provide the branch/commit list. Do not push, deploy, reload, or
modify the production N150.
