# Optional Art Probe Timeout Hotfix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release v0.7.1 so silently unsupported optional Art commands fall back without killing a healthy websocket, while ambiguous or genuine transport loss still enters the existing supervised recovery path.

**Architecture:** Add an opt-in five-second probe policy to `FrameArt` for read-only optional capability commands only. `FrameDevice` collects same-generation probe outcomes, requires correlated liveness evidence before caching a dialect, retires all-silent passes exactly once, and routes slideshow writes through the generation-scoped dialect learned by readback. Coordinator freshness and generation semantics remain unchanged.

**Tech Stack:** Python 3.13+, Home Assistant config-entry/coordinator APIs, `asyncio`, `websockets`, `samsungtvws[async,encrypted]==3.0.5`, pytest, pytest-homeassistant-custom-component, Ruff, HACS, GitHub Actions.

## Global Constraints

- Keep one supervised Art websocket and one receiver per config entry; do not add another connection owner, coordinator, or retry loop.
- Keep `samsungtvws[async,encrypted]==3.0.5`; add no dependency and no synchronous runtime fallback.
- `ART_PROBE_DEADLINE` is exactly `5` seconds. `ART_REQUEST_DEADLINE` remains exactly `20` seconds and `POLL_DEADLINE` remains exactly `45` seconds.
- `ArtProbeTimeout` inherits directly from `Exception`, not `TimeoutError`, `ResponseError`, or a transport exception.
- Probe behavior is opt-in and limited to aggregate settings, legacy brightness/color-temperature getters, and modern/legacy slideshow getters.
- Mutations, uploads, `get_artmode_status`, `get_current_artwork`, and D2D transfers keep normal close-on-timeout semantics.
- A dialect cache write requires at least one correlated value or `ResponseError` in the same discovery pass and current Art generation. Silence alone never caches `UNSUPPORTED`.
- An all-silent pass calls `async_connection_failed()` exactly once. `_ART_READ_FAILED` means the generic path already retired the session and must not trigger a second call.
- Cached-dialect probe timeouts are handled inside `FrameDevice`; no `ArtProbeTimeout` may escape into `FrameCoordinator`.
- Dialect caches reset on every Art generation. A mutation after reconnect must not reuse a stale slideshow dialect.
- Slideshow mutations may read the learned dialect to select the exact setter but never promote, demote, or write the dialect cache.
- Do not weaken READY/generation freshness and do not reorder coordinator commits; no coordinator state-model change is in scope.
- Preserve privacy: no host, MAC, token, content/category identifiers from production, raw websocket payloads, or exception details in logs, docs, commits, or external reviews.
- Apply the live-protocol-first doctrine: before finalizing any implementation contract that depends on a TV connection, probe the exact command and payload shape against an authorized live TV. Documentation and upstream examples are hypotheses until live evidence confirms them.
- Follow strict red-green-refactor. Observe each focused regression fail before implementation, then run the full suite before each task commit.
- Call external Claude only through the `claude-lesina` shell function. Use Fable for final plan/code review and give it time to finish.
- Treat `docs/` in this Samsung repository as public, tracked engineering documentation. Sanitize new design/plan files before explicitly staging them; never copy private Hindsight-repository material into this repository.
- Deploy only a tagged GitHub release through the configured HACS custom repository; never copy branch files or a tar archive into production.

---

## File Map

- Create `AGENTS.md`: codify the public live-protocol-first contributor doctrine and its release gate.
- Add `docs/superpowers/specs/2026-07-22-optional-art-probe-timeout-hotfix-design.md` and `docs/superpowers/plans/2026-07-22-optional-art-probe-timeout-hotfix.md`: commit the sanitized approved contract and execution plan under the repository's existing public docs convention.
- Modify `custom_components/samsungtv_frame/const.py`: add the five-second optional probe deadline.
- Modify `custom_components/samsungtv_frame/frame_art.py`: add `ArtProbeTimeout`, opt-in probe request semantics, optional getter opt-in, and exact modern/legacy slideshow setters.
- Modify `custom_components/samsungtv_frame/device.py`: propagate probe outcomes, collect liveness evidence, own exactly-once retirement, cache dialects only after proof, and route slideshow writes using the current-generation dialect.
- Modify `tests/test_frame_art.py`: pin probe timeout, waiter cleanup, late correlated response isolation, unchanged normal timeout, exact optional getter flags, and exact slideshow setter requests.
- Modify `tests/test_device.py`: pin every discovery/cached/mixed-failure path, generation fencing, write routing, and the sanitized live LS03B matrix.
- Modify `custom_components/samsungtv_frame/manifest.json`: bump `0.7.0` to `0.7.1` only after code and reviews pass.
- Modify `CHANGELOG.md`: prepend the v0.7.1 compatibility/recovery entry.

---

### Task 0: Codify and Satisfy the Live-Protocol-First Gate

**Files:**
- Create: `AGENTS.md`
- Add: `docs/superpowers/specs/2026-07-22-optional-art-probe-timeout-hotfix-design.md`
- Add: `docs/superpowers/plans/2026-07-22-optional-art-probe-timeout-hotfix.md`

**Interfaces:**
- Produces: a repository-wide contributor rule for every TV-connection-dependent feature and bugfix.
- Requires: sanitized, authorized evidence for the exact command, payload format, response correlation, timeout/error behavior, and any mutation restoration before an implementation contract becomes final.
- Blocks: release of a connection-dependent change whose live behavior is still inferred only from documentation, another model, or upstream code.

- [ ] **Step 1: Branch from the validated v0.7.0 baseline**

Execute Tasks 0–4 from `/tmp/ha-samsungtv-frame-native-async`; the absolute `/home/nicola/Projects/ha-samsungtv-frame/.venv` path supplies the already-provisioned test tools only. Verify `HEAD` is `e06146ca4c1648e20bc5dfcd3ad77b9743830321`, the public tree has no tracked edits, and exactly these two sanitized documentation files are untracked:

```text
docs/superpowers/plans/2026-07-22-optional-art-probe-timeout-hotfix.md
docs/superpowers/specs/2026-07-22-optional-art-probe-timeout-hotfix-design.md
```

Then create the release branch:

```bash
git switch -c fix/optional-art-probe-timeout-v0.7.1
```

Never stage with `git add .`; every public commit must use explicit paths so unrelated local files cannot enter public history.

- [ ] **Step 2: Add the doctrine to the public contributor guide**

Create root `AGENTS.md` with this normative section:

```markdown
# Contributor and AI-assistant guide

## Live-protocol-first doctrine

Before finalizing the implementation contract for any feature or bugfix whose
correctness depends on a TV connection, Samsung command, event, payload, or
timing behavior, probe the exact candidate protocol against an authorized live
TV.

- Record a sanitized behavior matrix in the approved design, implementation
  plan, issue, or pull request, covering the exact request name and
  parameter shape, response/event correlation, returned value shape, timeout or
  error behavior, TV model family, and relevant operating mode. Never record or
  share a host, MAC address, token, private Home Assistant data,
  artwork/content identifier observed from a production device, or raw
  production payload. Synthetic protocol fixtures remain permitted.
- Treat documentation, upstream implementations, and observations from other TV
  models as hypotheses. Use them to design the probe, not as a substitute for
  live evidence on the target TV.
- For a mutation probe, capture the original value first, make the smallest
  reversible change, read it back authoritatively, and restore and verify the
  exact original value even if the probe fails.
- Encode the confirmed behavior as a failing automated regression before writing
  runtime implementation code.
- If an authorized target TV is unavailable, mark the protocol contract
  provisional and do not describe the feature as implementation-ready or
  release-ready.
```

- [ ] **Step 3: Record that this hotfix already passes the live gate**

Use only this sanitized matrix in tests, reviews, and release notes:

| Live command on the 2022 Frame family in Art mode | Observed outcome |
|---|---|
| `get_artmode_status` | Correlated success |
| `get_current_artwork` | Correlated success |
| `get_artmode_settings` | Correlated aggregate success |
| `get_brightness` | Silent timeout |
| `get_color_temperature` | Silent timeout |
| `get_auto_rotation_status` | Silent timeout |
| `get_slideshow_status` | Correlated legacy success |
| `set_auto_rotation_status` with the unchanged value | Silent timeout |
| `set_slideshow_status` with the unchanged value | Correlated legacy success; original `off` state preserved |

This evidence was gathered before the hotfix contract was settled. It proves both the bounded-probe fallback and the learned legacy slideshow-write route required below. No host, credential, private HA data, content/category identifier, or raw payload may be copied into the repository or sent to reviewers.

- [ ] **Step 4: Verify and commit the public doctrine, design, and plan**

```bash
git add AGENTS.md \
  docs/superpowers/specs/2026-07-22-optional-art-probe-timeout-hotfix-design.md \
  docs/superpowers/plans/2026-07-22-optional-art-probe-timeout-hotfix.md
git diff --check --cached
git commit -m "docs: specify live-probed art timeout hotfix"
```

Expected: the commit contains only the sanitized `AGENTS.md`, approved design, and implementation plan.

---

### Task 1: Optional Probe Transport Contract

**Files:**
- Modify: `custom_components/samsungtv_frame/const.py`
- Modify: `custom_components/samsungtv_frame/frame_art.py`
- Modify: `tests/test_frame_art.py`

**Interfaces:**
- Produces: `ART_PROBE_DEADLINE: int = 5`.
- Produces: `class ArtProbeTimeout(Exception)`.
- Changes: `FrameArt.request(request: str, *, expected_sub_event: str | None = None, request_id: str | None = None, probe: bool = False, **params: Any) -> dict[str, Any]`.
- Changes: `FrameArt._request_unlocked(self, request: str, *, expected_sub_event: str | None, request_id: str | None, probe: bool = False, **params: Any) -> dict[str, Any]`.
- Guarantees: a probe timeout removes only its own waiter and leaves the websocket open; a normal timeout still closes it.

- [ ] **Step 1: Update the optional-getter request-shape test and verify RED**

Change the expected call in `test_optional_getters_send_exact_raw_requests` without importing the new exception yet:

```python
# Existing table and assertions remain; change the final assertion:
art.request.assert_awaited_once_with(request_name, probe=True)
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_frame_art.py::test_optional_getters_send_exact_raw_requests
```

Expected: FAIL because optional getters currently omit `probe=True`.

- [ ] **Step 2: Write probe-timeout and correlation regressions and verify RED**

Import `ART_PROBE_DEADLINE` from `const.py` and `ArtProbeTimeout` from `frame_art.py`, then append focused tests near `test_request_timeout_closes_transport`:

```python
def test_art_probe_timeout_is_not_a_normal_timeout():
    assert ART_PROBE_DEADLINE == 5
    assert issubclass(ArtProbeTimeout, Exception)
    assert not issubclass(ArtProbeTimeout, TimeoutError)
    assert not issubclass(ArtProbeTimeout, ResponseError)


async def test_probe_timeout_preserves_transport_and_owns_only_its_waiter():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_PROBE_DEADLINE",
            0.01,
        ),
    ):
        await art.start_listening()
        with pytest.raises(ArtProbeTimeout):
            await art.request("get_auto_rotation_status", probe=True)

        first_id = sent_art_request(ws, 0)["request_id"]
        assert not ws.closed
        assert art.connection is ws
        assert not art._pending
        assert art._uuidless_pending is None

        second = asyncio.create_task(
            art.request("get_slideshow_status", probe=True)
        )
        await wait_for_sent(ws, 2)
        second_id = sent_art_request(ws, 1)["request_id"]
        assert second_id != first_id

        await ws.frames.put(
            art_response(
                event="get_auto_rotation_status",
                request_id=first_id,
                value="off",
            )
        )
        await asyncio.sleep(0)
        assert not second.done()

        expected = {
            "event": "get_slideshow_status",
            "request_id": second_id,
            "value": "off",
        }
        await ws.frames.put(art_response(**expected))
        assert await second == expected
        await art.close()


async def test_probe_deadline_includes_blocked_websocket_send():
    art = make_art()
    ws = FakeWebSocket([])
    art.connection = ws
    art.is_alive = MagicMock(return_value=True)
    never_release = asyncio.Event()

    async def blocked_send(*_args):
        await never_release.wait()

    with (
        patch.object(art, "_send_command", side_effect=blocked_send),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_PROBE_DEADLINE",
            0.01,
        ),
        pytest.raises(ArtProbeTimeout),
    ):
        await art.request("get_artmode_settings", probe=True)

    assert not ws.closed
    assert art.connection is ws
    assert not art._pending
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q \
  tests/test_frame_art.py::test_art_probe_timeout_is_not_a_normal_timeout \
  tests/test_frame_art.py::test_probe_timeout_preserves_transport_and_owns_only_its_waiter \
  tests/test_frame_art.py::test_probe_deadline_includes_blocked_websocket_send
```

Expected: FAIL because the exception, flag, and deadline do not exist.

- [ ] **Step 3: Implement the probe transport contract**

Add to `const.py` beside the Art deadlines:

```python
ART_PROBE_DEADLINE = 5
```

Import it in `frame_art.py` and add:

```python
class ArtProbeTimeout(Exception):
    """An optional read-only capability probe received no response."""
```

Thread `probe: bool = False` from `request()` into `_request_unlocked()`. Replace the request deadline block with:

```python
deadline = ART_PROBE_DEADLINE if probe else ART_REQUEST_DEADLINE
try:
    async with asyncio.timeout(deadline):
        await self._send_command(connection, command, 0)
        response = await future
except TimeoutError:
    if probe:
        raise ArtProbeTimeout from None
    await self.close()
    raise
```

Do not change the existing `finally`: it already removes the exact UUID waiter, clears `_uuidless_pending` only when it owns that waiter, and consumes/cancels the future.

Opt in only these `FrameArt` getters:

```python
async def get_art_settings_payload(self) -> dict[str, Any]:
    return await self.request("get_artmode_settings", probe=True)

async def get_auto_rotation_status(self) -> dict[str, Any]:
    return await self.request("get_auto_rotation_status", probe=True)

async def get_legacy_slideshow_status(self) -> dict[str, Any]:
    return await self.request("get_slideshow_status", probe=True)

async def get_legacy_brightness(self) -> Any:
    return (await self.request("get_brightness", probe=True)).get("value")

async def get_legacy_color_temperature(self) -> Any:
    return (
        await self.request("get_color_temperature", probe=True)
    ).get("value")
```

Do not add `probe=True` anywhere else.

- [ ] **Step 4: Verify GREEN and unchanged normal timeout semantics**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_frame_art.py
```

Expected: all `test_frame_art.py` tests pass, including the existing `test_request_timeout_closes_transport` and `test_request_deadline_includes_blocked_websocket_send`.

- [ ] **Step 5: Run full suite, review the diff, and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/ruff check \
  custom_components/samsungtv_frame/const.py \
  custom_components/samsungtv_frame/frame_art.py tests/test_frame_art.py
git diff --check
git add custom_components/samsungtv_frame/const.py \
  custom_components/samsungtv_frame/frame_art.py tests/test_frame_art.py
git commit -m "fix: preserve art transport during optional probes"
```

Expected: complete suite and Ruff pass; commit contains no device dialect changes.

---

### Task 2: Liveness-Proven Dialect Discovery

**Files:**
- Modify: `custom_components/samsungtv_frame/device.py`
- Modify: `tests/test_device.py`

**Interfaces:**
- Consumes: `ArtProbeTimeout` from Task 1.
- Changes: `_async_art_read_response()` re-raises `ResponseError` and `ArtProbeTimeout`, returns `_ART_READ_FAILED` only after it has already retired a generic transport failure.
- Changes: `_async_get_legacy_art_settings(generation: int, *, liveness_proven: bool = False) -> ArtSettingsSnapshot | None` collects both getter outcomes before caching.
- Guarantees: dialect caches are written only with same-pass correlated liveness; no probe exception reaches the coordinator.

- [ ] **Step 1: Add slideshow discovery regressions and verify RED**

Import `ArtProbeTimeout` from `frame_art`, then add:

```python
@pytest.mark.parametrize("cached_auto", [False, True])
async def test_modern_slideshow_probe_timeout_falls_back_to_live_legacy(
    device, cached_auto
):
    if cached_auto:
        device._optional_dialect_generation = device.art_generation
        device._slideshow_dialect = (
            device._slideshow_dialect.__class__.AUTO_ROTATION
        )
    timeout = ArtProbeTimeout()
    device._art.get_auto_rotation_status = AsyncMock(side_effect=timeout)
    device._art.get_legacy_slideshow_status = AsyncMock(
        return_value=LEGACY_SLIDESHOW_PAYLOAD
    )

    assert await device.async_get_slideshow_state() == EXPECTED_LEGACY_SLIDESHOW
    assert device._slideshow_dialect.value == "legacy"
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_two_silent_slideshow_probes_retire_exactly_once(device):
    modern = ArtProbeTimeout()
    legacy = ArtProbeTimeout()
    device._art.get_auto_rotation_status = AsyncMock(side_effect=modern)
    device._art.get_legacy_slideshow_status = AsyncMock(side_effect=legacy)

    assert await device.async_get_slideshow_state() is None
    assert device._slideshow_dialect.value == "unknown"
    device._art_session.async_connection_failed.assert_awaited_once_with(legacy)


async def test_modern_correlated_error_proves_legacy_silence_is_unsupported(device):
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=ResponseError("modern unsupported")
    )
    device._art.get_legacy_slideshow_status = AsyncMock(
        side_effect=ArtProbeTimeout()
    )

    assert await device.async_get_slideshow_state() is None
    assert device._slideshow_dialect.value == "unsupported"
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_cached_legacy_slideshow_silence_retires_without_escape(device):
    device._optional_dialect_generation = device.art_generation
    device._slideshow_dialect = device._slideshow_dialect.__class__.LEGACY
    timeout = ArtProbeTimeout()
    device._art.get_legacy_slideshow_status = AsyncMock(side_effect=timeout)

    assert await device.async_get_slideshow_state() is None
    device._art_session.async_connection_failed.assert_awaited_once_with(timeout)
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q \
  tests/test_device.py::test_modern_slideshow_probe_timeout_falls_back_to_live_legacy \
  tests/test_device.py::test_two_silent_slideshow_probes_retire_exactly_once \
  tests/test_device.py::test_modern_correlated_error_proves_legacy_silence_is_unsupported \
  tests/test_device.py::test_cached_legacy_slideshow_silence_retires_without_escape
```

Expected: FAIL because `ArtProbeTimeout` is currently treated as a generic failure or escapes cached branches.

- [ ] **Step 2: Add settings discovery regressions and verify RED**

Add:

```python
async def test_aggregate_timeout_with_live_legacy_subset_caches_legacy(device):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    device._art.get_legacy_brightness = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    device._art.get_legacy_color_temperature = AsyncMock(return_value="-2")

    assert await device.async_get_art_settings() == ArtSettingsSnapshot(
        supported=frozenset({ArtSettingKey.COLOR_TEMPERATURE}),
        color_temperature=-2,
    )
    assert device._art_settings_dialect.value == "legacy"
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_aggregate_error_proves_two_silent_legacy_getters_live(device):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=ResponseError("aggregate unsupported")
    )
    device._art.get_legacy_brightness = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    device._art.get_legacy_color_temperature = AsyncMock(
        side_effect=ArtProbeTimeout()
    )

    assert await device.async_get_art_settings() == ArtSettingsSnapshot()
    assert device._art_settings_dialect.value == "legacy"
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_all_silent_settings_probes_retire_exactly_once(device):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    device._art.get_legacy_brightness = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    final_timeout = ArtProbeTimeout()
    device._art.get_legacy_color_temperature = AsyncMock(
        side_effect=final_timeout
    )

    assert await device.async_get_art_settings() is None
    assert device._art_settings_dialect.value == "unknown"
    device._art_session.async_connection_failed.assert_awaited_once_with(
        final_timeout
    )


async def test_cached_aggregate_silence_uses_live_legacy_fallback(device):
    device._optional_dialect_generation = device.art_generation
    device._art_settings_dialect = (
        device._art_settings_dialect.__class__.AGGREGATE
    )
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    device._art.get_legacy_brightness = AsyncMock(return_value="7")
    device._art.get_legacy_color_temperature = AsyncMock(return_value="-2")

    result = await device.async_get_art_settings()
    assert result is not None
    assert device._art_settings_dialect.value == "legacy"
    device._art_session.async_connection_failed.assert_not_awaited()
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q \
  tests/test_device.py::test_aggregate_timeout_with_live_legacy_subset_caches_legacy \
  tests/test_device.py::test_aggregate_error_proves_two_silent_legacy_getters_live \
  tests/test_device.py::test_all_silent_settings_probes_retire_exactly_once \
  tests/test_device.py::test_cached_aggregate_silence_uses_live_legacy_fallback
```

Expected: FAIL because current logic falls back only after `ResponseError` and caches `LEGACY` before proof.

- [ ] **Step 3: Add mixed-failure and generation regressions and verify RED**

Add a delegate whose generic failure is converted by `_async_art_read_response()` into `_ART_READ_FAILED`, then assert no second retirement:

```python
async def test_probe_timeout_then_transport_failure_retires_only_once(device):
    transport_error = OSError("lost")
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    device._art.get_legacy_slideshow_status = AsyncMock(
        side_effect=transport_error
    )

    assert await device.async_get_slideshow_state() is None
    device._art_session.async_connection_failed.assert_awaited_once_with(
        transport_error
    )
```

Extend the existing generation-loss parametrizations with `ArtProbeTimeout` before a fallback and assert the fallback does not cache against a changed generation.

Add the explicit generation case:

```python
async def test_generation_change_after_probe_timeout_skips_fallback(device):
    async def timeout_after_generation_change():
        device._art_session.generation += 1
        raise ArtProbeTimeout()

    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=timeout_after_generation_change
    )
    device._art.get_legacy_slideshow_status = AsyncMock()

    assert await device.async_get_slideshow_state() is None
    assert device._slideshow_dialect.value == "unknown"
    device._art.get_legacy_slideshow_status.assert_not_awaited()
    device._art_session.async_connection_failed.assert_not_awaited()
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q \
  tests/test_device.py::test_probe_timeout_then_transport_failure_retires_only_once \
  tests/test_device.py::test_generation_change_after_probe_timeout_skips_fallback
```

Expected: the mixed-failure test FAILS by double retirement or wrong exception handling, and the generation test FAILS by attempting a stale fallback.

- [ ] **Step 4: Implement explicit probe outcome handling**

Import `ArtProbeTimeout`. Change `_async_art_read_response()` ordering:

```python
try:
    return await operation()
except (ResponseError, ArtProbeTimeout):
    raise
except Exception as err:  # noqa: BLE001
    await self._art_session.async_connection_failed(err)
    return _ART_READ_FAILED
```

Restructure `_async_get_legacy_art_settings()` to collect both outcomes:

```python
async def _async_get_legacy_art_settings(
    self,
    generation: int,
    *,
    liveness_proven: bool = False,
) -> ArtSettingsSnapshot | None:
    supported: set[ArtSettingKey] = set()
    normalized: dict[ArtSettingKey, int | str | bool | None] = {}
    last_timeout: ArtProbeTimeout | None = None
    getters = (
        (ArtSettingKey.BRIGHTNESS, self._art.get_legacy_brightness),
        (
            ArtSettingKey.COLOR_TEMPERATURE,
            self._art.get_legacy_color_temperature,
        ),
    )

    for key, getter in getters:
        try:
            value = await self._async_art_read_response(getter)
        except ResponseError:
            liveness_proven = True
            continue
        except ArtProbeTimeout as err:
            last_timeout = err
            continue

        if value is _ART_READ_FAILED:
            return None
        if not self._optional_generation_is_current(generation):
            return None
        liveness_proven = True
        supported.add(key)
        normalized[key] = normalize_art_setting(key, value)

    if not self._optional_generation_is_current(generation):
        return None
    if not liveness_proven:
        assert last_timeout is not None
        await self._art_session.async_connection_failed(last_timeout)
        return None

    self._art_settings_dialect = _ArtSettingsDialect.LEGACY
    return ArtSettingsSnapshot(
        supported=frozenset(supported),
        brightness=normalized.get(ArtSettingKey.BRIGHTNESS),
        color_temperature=normalized.get(
            ArtSettingKey.COLOR_TEMPERATURE
        ),
    )
```

Replace `async_get_art_settings()` with this decision shape:

```python
async def async_get_art_settings(self) -> ArtSettingsSnapshot | None:
    """Return normalized settings with same-pass liveness evidence."""
    generation = self.art_generation
    self._reset_optional_dialects_for_generation(generation)
    if self._art_settings_dialect is _ArtSettingsDialect.LEGACY:
        return await self._async_get_legacy_art_settings(generation)

    liveness_proven = False
    try:
        payload = await self._async_art_read_response(
            self._art.get_art_settings_payload
        )
    except ResponseError:
        liveness_proven = True
    except ArtProbeTimeout:
        pass
    else:
        if (
            payload is _ART_READ_FAILED
            or not self._optional_generation_is_current(generation)
        ):
            return None
        snapshot = (
            parse_art_settings(payload)
            if isinstance(payload, dict)
            else None
        )
        if (
            snapshot is not None
            and self._optional_generation_is_current(generation)
        ):
            self._art_settings_dialect = _ArtSettingsDialect.AGGREGATE
        return snapshot

    if not self._optional_generation_is_current(generation):
        return None
    return await self._async_get_legacy_art_settings(
        generation,
        liveness_proven=liveness_proven,
    )
```

Do not cache `LEGACY` before the collector returns.

Replace `async_get_slideshow_state()` with the explicit paths below:

```python
async def async_get_slideshow_state(self) -> SlideshowState | None:
    """Return slideshow state using same-pass liveness evidence."""
    generation = self.art_generation
    self._reset_optional_dialects_for_generation(generation)
    dialect = self._slideshow_dialect
    if dialect is _SlideshowDialect.UNSUPPORTED:
        return None

    if dialect is _SlideshowDialect.LEGACY:
        try:
            payload = await self._async_art_read_response(
                self._art.get_legacy_slideshow_status
            )
        except ResponseError:
            if self._optional_generation_is_current(generation):
                self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
            return None
        except ArtProbeTimeout as err:
            if self._optional_generation_is_current(generation):
                await self._art_session.async_connection_failed(err)
            return None
        if (
            payload is _ART_READ_FAILED
            or not self._optional_generation_is_current(generation)
        ):
            return None
        return parse_slideshow(payload) if isinstance(payload, dict) else None

    liveness_proven = False
    try:
        payload = await self._async_art_read_response(
            self._art.get_auto_rotation_status
        )
    except ResponseError:
        liveness_proven = True
    except ArtProbeTimeout:
        pass
    else:
        if (
            payload is _ART_READ_FAILED
            or not self._optional_generation_is_current(generation)
        ):
            return None
        self._slideshow_dialect = _SlideshowDialect.AUTO_ROTATION
        return parse_slideshow(payload) if isinstance(payload, dict) else None

    if not self._optional_generation_is_current(generation):
        return None
    try:
        payload = await self._async_art_read_response(
            self._art.get_legacy_slideshow_status
        )
    except ResponseError:
        if self._optional_generation_is_current(generation):
            self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
        return None
    except ArtProbeTimeout as err:
        if not self._optional_generation_is_current(generation):
            return None
        if liveness_proven:
            self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
        else:
            await self._art_session.async_connection_failed(err)
        return None

    if (
        payload is _ART_READ_FAILED
        or not self._optional_generation_is_current(generation)
    ):
        return None
    self._slideshow_dialect = _SlideshowDialect.LEGACY
    return parse_slideshow(payload) if isinstance(payload, dict) else None
```

Use small private helpers only if they make the proof visible; do not introduce a general retry framework or a second state machine.

- [ ] **Step 5: Verify focused and complete GREEN**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_device.py
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/ruff check \
  custom_components/samsungtv_frame/device.py tests/test_device.py
git diff --check
```

Expected: all tests pass; existing malformed-payload, `ResponseError`, generation-reset, stopped-device, and background-no-open tests remain green.

- [ ] **Step 6: Commit the independently reviewable dialect fix**

```bash
git add custom_components/samsungtv_frame/device.py tests/test_device.py
git commit -m "fix: prove art dialect liveness before caching"
```

---

### Task 3: Learned-Dialect Slideshow Writes and Live-Matrix Regression

**Files:**
- Modify: `custom_components/samsungtv_frame/frame_art.py`
- Modify: `custom_components/samsungtv_frame/device.py`
- Modify: `tests/test_frame_art.py`
- Modify: `tests/test_device.py`

**Interfaces:**
- Produces: `FrameArt.set_auto_rotation(duration: int, shuffle: bool, category_id: str) -> dict[str, Any]`.
- Produces: `FrameArt.set_legacy_slideshow(duration: int, shuffle: bool, category_id: str) -> dict[str, Any]`.
- Preserves: `FrameArt.set_slideshow(self, duration: int, shuffle: bool, category_id: str) -> dict[str, Any]` as modern-first with fallback only after correlated `ResponseError`.
- Changes: `FrameDevice.async_set_slideshow()` resets stale generation caches and routes from the read-learned dialect without mutating it.

- [ ] **Step 1: Write exact transport setter tests and verify RED**

Refactor the existing slideshow setter tests to pin exact methods:

```python
@pytest.mark.parametrize(
    ("method", "request_name"),
    [
        ("set_auto_rotation", "set_auto_rotation_status"),
        ("set_legacy_slideshow", "set_slideshow_status"),
    ],
)
async def test_exact_slideshow_setters_send_one_normal_request(
    method, request_name
):
    art = make_art()
    art.request = AsyncMock(return_value={"event": "changed"})

    assert await getattr(art, method)(15, True, "MY-C0004") == {
        "event": "changed"
    }
    art.request.assert_awaited_once_with(
        request_name,
        value="15",
        category_id="MY-C0004",
        type="shuffleslideshow",
    )
```

Keep `test_slideshow_falls_back_only_after_response_error` and `test_slideshow_does_not_mask_transport_errors` unchanged in meaning.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q \
  tests/test_frame_art.py::test_exact_slideshow_setters_send_one_normal_request \
  tests/test_frame_art.py::test_slideshow_falls_back_only_after_response_error \
  tests/test_frame_art.py::test_slideshow_does_not_mask_transport_errors
```

Expected: FAIL because the two exact setter methods do not exist yet.

- [ ] **Step 2: Implement exact setters and preserve the compatibility wrapper**

Factor the parameter mapping without changing wire values:

```python
@staticmethod
def _slideshow_params(
    duration: int, shuffle: bool, category_id: str
) -> dict[str, str]:
    return {
        "value": str(duration) if duration > 0 else "off",
        "category_id": category_id,
        "type": "shuffleslideshow" if shuffle else "slideshow",
    }

async def set_auto_rotation(
    self, duration: int, shuffle: bool, category_id: str
) -> dict[str, Any]:
    return await self.request(
        "set_auto_rotation_status",
        **self._slideshow_params(duration, shuffle, category_id),
    )

async def set_legacy_slideshow(
    self, duration: int, shuffle: bool, category_id: str
) -> dict[str, Any]:
    return await self.request(
        "set_slideshow_status",
        **self._slideshow_params(duration, shuffle, category_id),
    )

async def set_slideshow(
    self, duration: int, shuffle: bool, category_id: str
) -> dict[str, Any]:
    try:
        return await self.set_auto_rotation(duration, shuffle, category_id)
    except ResponseError:
        return await self.set_legacy_slideshow(
            duration, shuffle, category_id
        )
```

No setter uses `probe=True`; timeout remains indeterminate and closes the transport.

- [ ] **Step 3: Write device routing and generation-reset tests and verify RED**

Add the complete current-generation routing matrix:

```python
@pytest.mark.parametrize(
    ("dialect_name", "expected_delegate"),
    [
        ("LEGACY", "set_legacy_slideshow"),
        ("AUTO_ROTATION", "set_auto_rotation"),
        ("UNKNOWN", "set_slideshow"),
        ("UNSUPPORTED", "set_slideshow"),
    ],
)
async def test_slideshow_write_routes_dialect_without_cache_write(
    device, dialect_name, expected_delegate
):
    dialect_type = device._slideshow_dialect.__class__
    device._optional_dialect_generation = device.art_generation
    expected_dialect = getattr(dialect_type, dialect_name)
    device._slideshow_dialect = expected_dialect
    device._art.set_auto_rotation = AsyncMock()
    device._art.set_legacy_slideshow = AsyncMock()
    device._art.set_slideshow = AsyncMock()

    await device.async_set_slideshow(60, True, "MY-C0002")

    getattr(device._art, expected_delegate).assert_awaited_once_with(
        60, True, "MY-C0002"
    )
    for delegate in (
        "set_auto_rotation",
        "set_legacy_slideshow",
        "set_slideshow",
    ):
        if delegate != expected_delegate:
            getattr(device._art, delegate).assert_not_awaited()
    assert device._slideshow_dialect is expected_dialect


async def test_slideshow_write_does_not_reuse_previous_generation_dialect(device):
    dialect_type = device._slideshow_dialect.__class__
    device._optional_dialect_generation = device.art_generation - 1
    device._slideshow_dialect = dialect_type.LEGACY
    device._art.set_slideshow = AsyncMock()
    device._art.set_legacy_slideshow = AsyncMock()

    await device.async_set_slideshow(60, False, "MY-C0002")

    device._art.set_slideshow.assert_awaited_once_with(
        60, False, "MY-C0002"
    )
    device._art.set_legacy_slideshow.assert_not_awaited()
    assert device._slideshow_dialect is dialect_type.UNKNOWN
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q \
  tests/test_device.py::test_slideshow_write_routes_dialect_without_cache_write \
  tests/test_device.py::test_slideshow_write_does_not_reuse_previous_generation_dialect
```

Expected: FAIL because `FrameDevice.async_set_slideshow()` does not yet route by current-generation dialect.

- [ ] **Step 4: Implement generation-safe write routing**

Use a nested coroutine so reset and selection happen after USER readiness is established:

```python
async def async_set_slideshow(
    self, duration: int, shuffle: bool, category_id: str
) -> None:
    """Configure slideshow using this generation's read-proven dialect."""

    async def _set() -> None:
        self._reset_optional_dialects_for_generation(self.art_generation)
        if self._slideshow_dialect is _SlideshowDialect.LEGACY:
            await self._art.set_legacy_slideshow(
                duration, shuffle, category_id
            )
            return
        if self._slideshow_dialect is _SlideshowDialect.AUTO_ROTATION:
            await self._art.set_auto_rotation(
                duration, shuffle, category_id
            )
            return
        await self._art.set_slideshow(duration, shuffle, category_id)

    await self._async_art_mutation(_set)
```

Update broad mutation delegation tests to mock the three setter methods and assert USER readiness still occurs exactly once.

- [ ] **Step 5: Add the sanitized production-matrix regression**

Add a device-level test representing the exact live evidence without addresses, credentials, or raw identifiers:

```python
async def test_ls03b_live_matrix_keeps_generation_and_publishes_optionals(device):
    generation = device.art_generation
    device._art.get_art_settings_payload = AsyncMock(
        return_value=VALID_ART_SETTINGS_PAYLOAD
    )
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=ArtProbeTimeout()
    )
    device._art.get_legacy_slideshow_status = AsyncMock(
        return_value={"value": "off", "type": "slideshow"}
    )

    settings = await device.async_get_art_settings()
    slideshow = await device.async_get_slideshow_state()

    assert settings == EXPECTED_ART_SETTINGS
    assert slideshow == SlideshowState(SlideshowMode.OFF, 0)
    assert device.art_generation == generation
    assert device._art_settings_dialect.value == "aggregate"
    assert device._slideshow_dialect.value == "legacy"
    device._art_session.async_connection_failed.assert_not_awaited()
```

The existing coordinator tests already prove that non-`None` settings and slideshow returned by the device commit under the unchanged READY/generation fence; do not duplicate coordinator internals or change `coordinator.py`.

- [ ] **Step 6: Verify full GREEN, run Fable code review, and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_frame_art.py tests/test_device.py \
  tests/test_coordinator.py tests/test_media_player.py
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/ruff check .
git diff --check
```

Give Fable the public diff (including `AGENTS.md`), design, focused results, and full-suite result through `claude-lesina`. Resolve only verified findings, rerun focused/full tests, then:

```bash
git add custom_components/samsungtv_frame/frame_art.py \
  custom_components/samsungtv_frame/device.py \
  tests/test_frame_art.py tests/test_device.py
git commit -m "fix: route slideshow through learned art dialect"
```

---

### Task 4: Release v0.7.1 and Verify Production Through HACS

**Files:**
- Modify: `custom_components/samsungtv_frame/manifest.json`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: public release/tag `v0.7.1` with manifest version `0.7.1`.
- Deploys: the exact GitHub release through HACS custom-repository APIs.
- Restores: every production TV value changed during acceptance.

- [ ] **Step 1: Bump release metadata**

Change only the manifest version:

```json
"version": "0.7.1"
```

Prepend:

```markdown
## 0.7.1

- Treat silently unsupported optional Art capability reads as bounded probes,
  allowing modern-to-legacy fallback without closing a healthy websocket.
- Require same-generation correlated liveness before caching Art settings or
  slideshow dialects, while retaining supervised recovery for ambiguous
  all-silent transports.
- Route slideshow writes through the read-proven generation dialect so older
  Frame firmware does not time out on an unsupported modern command.
```

- [ ] **Step 2: Run release verification from a clean candidate tree**

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/ruff check .
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/python -m compileall -q \
  custom_components tests
git diff --check
git status --short --branch
```

Expected: all tests pass, Ruff and compileall are silent/successful, and only intentional release files remain uncommitted.

- [ ] **Step 3: Commit the release, fast-forward main, push, and wait for GitHub validation**

```bash
git add custom_components/samsungtv_frame/manifest.json CHANGELOG.md
git commit -m "release: v0.7.1 (optional Art probe fallback)"
git switch main
git merge --ff-only fix/optional-art-probe-timeout-v0.7.1
git push origin main
```

Wait for the commit's pytest, Ruff, hassfest, and HACS jobs. Do not tag or deploy a failing commit.

- [ ] **Step 4: Tag and publish the release**

```bash
git tag -a v0.7.1 -m "v0.7.1 optional Art probe fallback"
git push origin v0.7.1
gh release create v0.7.1 \
  --title "v0.7.1 — Optional Art probe fallback" \
  --notes-file /tmp/ha-samsungtv-frame-v0.7.1-release-notes.md
```

Prepare the short release notes in `/tmp`, not the public or private repository. Verify the release points at the exact validated commit and is latest stable.

- [ ] **Step 5: Install the exact tag through HACS**

Run production operations from `/home/nicola/Projects/ha-samsungtv-frame`. Pin and verify the packaged runtime before the deploy window:

```bash
PLUGIN_ROOT=/home/nicola/.codex/plugins/cache/personal/ha-prod-console/0.1.0+codex.20260712101512
test -x "$PLUGIN_ROOT/scripts/config.sh"
test -x "$PLUGIN_ROOT/scripts/exec.sh"
test -x "$PLUGIN_ROOT/scripts/ssh.sh"
test -x "$PLUGIN_ROOT/scripts/restart.sh"
test -x "$PLUGIN_ROOT/scripts/smoke.sh"
HPC_CODEX=1 bash "$PLUGIN_ROOT/scripts/config.sh"
```

Use only those packaged scripts for N150 access. Audit the existing sanitized `/tmp/hacs_samsung_release.py` helper as a whole and update only its public `VERSION` constant to `v0.7.1` with `apply_patch`. Stream and invoke it with these exact packaged entry points:

```bash
HPC_CODEX=1 bash "$PLUGIN_ROOT/scripts/ssh.sh" \
  docker exec -i homeassistant sh -c \
  'umask 077; cat > /tmp/hacs_samsung_release.py' \
  < /tmp/hacs_samsung_release.py
HPC_CODEX=1 bash "$PLUGIN_ROOT/scripts/exec.sh" homeassistant \
  python /tmp/hacs_samsung_release.py download
```

The helper must mint its five-minute credential only in container memory, print only allowlisted public status, and never persist or print authentication material. Core restart removes the container-local temporary helper.

Through that local authenticated HA WebSocket route:

1. refresh HACS repository `bonzanni/ha-samsungtv-frame`;
2. verify update entity reports installed `v0.7.0`, latest `v0.7.1`;
3. invoke `hacs/repository/download` with exact version `v0.7.1`;
4. compare the production integration's 29-file hash manifest against the tag and require an exact match;
5. run `HPC_CODEX=1 bash "$PLUGIN_ROOT/scripts/ssh.sh" ha core check --raw-json` and require `{"result":"ok"}`;
6. restart Core with `HPC_CODEX=1 bash "$PLUGIN_ROOT/scripts/restart.sh" --yes` under the already authorized deployment objective;
7. run `HPC_CODEX=1 bash "$PLUGIN_ROOT/scripts/smoke.sh"` immediately after restart and diagnose before any redeploy.

Never output the HA token, TV host, MAC, Samsung credential, content IDs, or raw HACS storage.

- [ ] **Step 6: Run live acceptance and restore all values**

Require diagnostics to show:

```text
integration_manifest.version=0.7.1
tv_mode=art_mode
art_session_state=ready
art_session_ready=true
art_failures=0
supported_settings includes the live aggregate keys
slideshow_known=true
```

Capture current values in process memory only. Round-trip one alternate valid value for each available setting, wait for authoritative reconciliation, then restore and verify the exact original:

- Art brightness;
- Art color temperature;
- Sleep After;
- motion sensitivity;
- brightness sensor;
- slideshow using the cached legacy dialect, preserving/restoring duration,
  shuffle mode, and category.

After restoration, wait through multiple heartbeat intervals and require:

- the Art generation remains stable;
- all supported optional entities remain available;
- no probe timeout escapes to coordinator logs;
- no repeated BACKOFF/READY churn;
- no new Samsung integration errors;
- Home Assistant Core remains responsive.

If any condition fails, diagnose once. If v0.7.1 cannot pass acceptance, install exact `v0.6.9` through HACS and restart Core; do not leave production on the known-churning v0.7.0 release.

- [ ] **Step 7: Final repository and production audit**

Verify:

```bash
git status --short --branch
git log -4 --oneline --decorate
git ls-remote --tags origin v0.7.1
```

Expected: public main clean and synchronized, tag exact, sanitized design/plan and `AGENTS.md` tracked, production manifest `0.7.1`, restored TV values, stable READY Art session.
