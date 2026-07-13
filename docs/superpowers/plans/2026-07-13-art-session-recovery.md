# Supervised Art Session Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unbounded poll/listener Art reconnect churn with one supervised native-async session that uses push-driven cached state and host-aware backoff.

**Architecture:** Keep `FrameArt` as the live-proven low-level protocol adapter and add `ArtSession` as the sole runtime owner of connection lifecycle, single-flight recovery, and retry state. `FrameDevice` routes background reads and user mutations through the session; the coordinator polls REST power but only reconciles Art over an already-READY socket.

**Tech Stack:** Python 3.13+, Home Assistant config-entry APIs, `asyncio`, `websockets`, `samsungtvws[async,encrypted]==3.0.5`, pytest, pytest-homeassistant-custom-component.

## Global Constraints

- Keep the healthy `v0.6.7` Art wire path unchanged: TLS port `8002`, client name `Home Assistant`, existing token handling, Art endpoint, request framing, D2D framing, and upload correlation.
- Keep `samsungtvws[async,encrypted]==3.0.5`; add no Samsung websocket dependency and no synchronous fallback.
- At most one runtime Art websocket and one handshake may exist per config entry; only `ArtSession` may initiate runtime start, close, or recovery.
- Coordinator heartbeats never directly open or await an Art websocket and never
  call a getter that can ensure readiness. They synchronously submit a power
  observation; only the entry-owned `ArtSession` task may perform a budgeted open.
  Cold/reconnect seeding and 300-second reconciliation issue requests only when the
  current session is already `READY`.
- Generic delays are exactly `30, 60, 120, 300` seconds; hostless delays are exactly `60, 120, 300` seconds; the third consecutive hostless failure enters `DORMANT` for exactly `900` seconds; jitter is uniform plus or minus `20%`.
- `ms.channel.connect` is hostless only when it contains an explicit non-empty `clients` list with no `isHost is True`; absent, empty, or malformed client metadata must continue waiting for `ms.channel.ready`.
- A user Art operation may bypass recovery suppression for one connection probe and executes its mutation exactly once; uploads and all other mutations are never automatically retried.
- Preserve config-entry schema, entity IDs, services, remote/REST/UPnP/WoL behavior, pairing behavior, and stored-token data.
- Runtime receiver, connect, and refresh tasks must use `ConfigEntry.async_create_background_task`; unload must be bounded and terminal.
- Do not change D2D upload/thumbnail implementation or add HA Repairs UI in this release.
- Follow strict red-green-refactor: every production behavior change requires a failing test observed first.
- Version the finished release as `0.6.8` and deploy through the existing HACS custom repository, never a tar archive.
- Invoke external Claude only through the `claude-lesina` shell function; do not invoke `claude` directly.

---

## File Map

- Modify `custom_components/samsungtv_frame/frame_art.py`: typed hostless handshake and ready-only request precondition.
- Create `custom_components/samsungtv_frame/art_session.py`: connection state machine, single-flight connection ownership, backoff, user/power triggers, clean stop.
- Modify `custom_components/samsungtv_frame/const.py`: exact retry, dormant, jitter, and reconcile constants.
- Modify `custom_components/samsungtv_frame/device.py`: construct/use `ArtSession`, separate background reads from one-shot user mutations, expose readiness/generation/power observation.
- Modify `custom_components/samsungtv_frame/coordinator.py`: remove independent listener restart, use cached Art state, READY-generation seeding, and READY-only 300-second reconciliation.
- Modify `custom_components/samsungtv_frame/__init__.py`: wire session-state callback and arm the session before first refresh.
- Modify `tests/test_frame_art.py`: realistic host metadata, hostless fast failure, host-present timeout, ready-only requests.
- Create `tests/test_art_session.py`: deterministic state-machine and connection-budget tests.
- Modify `tests/test_device.py`, `tests/test_coordinator.py`, `tests/test_init.py`, and `tests/conftest.py`: facade, cache, setup, and unload regression coverage.
- Modify `custom_components/samsungtv_frame/manifest.json` and `README.md`: release `0.6.8` and recovery behavior.

---

### Task 1: Typed Host-Aware Handshake

**Files:**
- Modify: `custom_components/samsungtv_frame/frame_art.py:25-155`
- Modify: `tests/test_frame_art.py:1140-1405`

**Interfaces:**
- Produces: `class ArtHostUnavailable(ConnectionFailure)` from `frame_art.py`.
- Preserves: `FrameArt.open()`, `FrameArt._wait_for_handshake()`, and all healthy handshake behavior.
- Consumed by: `ArtSession` in Task 2.

- [ ] **Step 1: Make successful handshake fixtures carry real host metadata**

Change `handshake_frames()` and literal successful handshake frames to use:

```python
def handshake_frames():
    """Return a successful Art websocket handshake with the TV host present."""
    return [
        {
            "event": "ms.channel.connect",
            "data": {
                "clients": [
                    {"isHost": True, "deviceName": "Smart Device"},
                    {"isHost": False, "deviceName": "Home Assistant"},
                ]
            },
        },
        {"event": "ms.channel.ready"},
    ]
```

Fixtures specifically testing omitted metadata may retain a connect frame without
`clients` and must document that compatibility case.

- [ ] **Step 2: Write the failing host-role tests**

Add these tests before production code:

```python
async def test_open_fails_fast_when_connect_lists_no_art_host():
    ws = FakeWebSocket([
        {
            "event": "ms.channel.connect",
            "data": {"clients": [{"isHost": False, "deviceName": "Home Assistant"}]},
        },
        {"event": "ms.channel.ready"},
    ])
    art = make_art()
    with (
        patch("custom_components.samsungtv_frame.frame_art.connect", AsyncMock(return_value=ws)),
        pytest.raises(ArtHostUnavailable),
    ):
        await art.open()
    assert ws.closed
    assert ws.frames.qsize() == 1
    assert art.connection is None


async def test_open_allows_missing_client_metadata_when_ready_arrives():
    ws = FakeWebSocket([
        {"event": "ms.channel.connect", "data": {}},
        {"event": "ms.channel.ready"},
    ])
    art = make_art()
    with patch("custom_components.samsungtv_frame.frame_art.connect", AsyncMock(return_value=ws)):
        assert await art.open() is ws
    await art.close()


async def test_open_times_out_when_host_is_present_but_ready_never_arrives():
    ws = FakeWebSocket([
        {
            "event": "ms.channel.connect",
            "data": {"clients": [{"isHost": True, "deviceName": "Smart Device"}]},
        }
    ])
    art = make_art(timeout=0.01)
    with (
        patch("custom_components.samsungtv_frame.frame_art.connect", AsyncMock(return_value=ws)),
        pytest.raises(TimeoutError),
    ):
        await art.open()
    assert ws.closed
    assert art.connection is None
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q \
  tests/test_frame_art.py::test_open_fails_fast_when_connect_lists_no_art_host \
  tests/test_frame_art.py::test_open_allows_missing_client_metadata_when_ready_arrives \
  tests/test_frame_art.py::test_open_times_out_when_host_is_present_but_ready_never_arrives
```

Expected: collection or assertion failure because `ArtHostUnavailable` and host-role
validation do not exist. The missing-metadata and timeout tests may already pass; the
hostless test must fail for the intended reason before implementation.

- [ ] **Step 4: Implement conservative host validation**

Add the exception and helper:

```python
class ArtHostUnavailable(ConnectionFailure):
    """The channel connected but explicitly listed no internal Art host."""


def _explicitly_missing_art_host(frame: dict[str, Any]) -> bool:
    data = frame.get("data")
    if not isinstance(data, dict):
        return False
    clients = data.get("clients")
    if not isinstance(clients, list) or not clients:
        return False
    return not any(
        isinstance(client, dict) and client.get("isHost") is True
        for client in clients
    )
```

Immediately after `_check_for_token(frame)` in `_wait_for_handshake()`:

```python
if _explicitly_missing_art_host(frame):
    raise ArtHostUnavailable("Art channel connected without an internal host")
```

- [ ] **Step 5: Verify GREEN and the full transport suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q tests/test_frame_art.py
```

Expected: all `test_frame_art.py` tests pass with no warning or unclosed-task output.

- [ ] **Step 6: Commit the independently green transport change**

```bash
git add custom_components/samsungtv_frame/frame_art.py tests/test_frame_art.py
git commit -m "fix: detect unavailable frame art host"
```

---

### Task 2: Single-Owner Art Session State Machine

**Files:**
- Create: `custom_components/samsungtv_frame/art_session.py`
- Create: `tests/test_art_session.py`
- Modify: `custom_components/samsungtv_frame/const.py:68-75`

**Interfaces:**
- Consumes: `FrameArt`, `ArtHostUnavailable`, and existing `TaskFactory`.
- Produces: `ArtSessionState`, `ArtSessionTrigger`, `ArtSession`,
  `set_state_callback()`, `async_start()`, `observe_power()`, `async_ensure_ready()`,
  `async_connection_failed()`, `async_stop()`, `ready`, `generation`, and `state`.
- `observe_power()` is synchronous and may only schedule an entry-owned task; it
  never awaits network I/O.
- Consumed by: `FrameDevice` in Task 3.

- [ ] **Step 1: Add exact policy constants**

Append to `const.py`:

```python
ART_RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0)
ART_HOST_RETRY_DELAYS = (60.0, 120.0, 300.0)
ART_DORMANT_SECONDS = 900.0
ART_RETRY_JITTER = 0.20
ART_RECONCILE_SECONDS = 300.0
```

- [ ] **Step 2: Write deterministic fake-transport tests**

Create a `FakeArt` whose `start_listening`, `close`, `stop`, and `is_alive` are
controlled and counted. Inject `clock=lambda: now` and
`jitter=lambda delay: delay` into `ArtSession`.

Add these exact behaviors as separate tests:

- `test_concurrent_callers_share_one_connect_task`: block
  `FakeArt.start_listening`, call `async_ensure_ready(USER)` from two tasks, release
  the fake, and assert both return `True` while `start_calls == 1`.
- `test_generic_failures_follow_30_60_120_300_backoff`: feed five
  `ConnectionFailure` results, advance the fake clock to each deadline, and assert
  `next_retry_at - now` is `30`, `60`, `120`, `300`, then `300` seconds.
- `test_observations_inside_backoff_do_not_connect`: perform one generic failure,
  repeat 30 power-on observations at the same clock value, and assert one start.
- `test_three_hostless_failures_enter_900_second_dormant`: feed three
  `ArtHostUnavailable` failures at their due times, assert the first two states are
  `BACKOFF`, the third is `DORMANT`, and the dormant deadline is `now + 900`.
- `test_dormant_allows_exactly_one_half_open_probe`: hold the clock below 900 and
  assert no start, advance to the deadline, issue multiple observations, and assert
  exactly one new start task.
- `test_reachable_edge_resets_failures_and_allows_one_probe`: enter backoff, send a
  false-to-true reachable edge before its deadline, and assert one immediate start
  and reset failure counters.
- `test_user_trigger_bypasses_suppression_once`: enter dormant, call
  `async_ensure_ready(USER)` twice concurrently, and assert one physical start and
  the shared boolean result.
- `test_ready_resets_failures_and_increments_generation`: fail once, then succeed;
  assert state `READY`, generation `1`, zero counters, and `next_retry_at == 0`.
- `test_dead_ready_receiver_enters_backoff_without_immediate_open`: mark a ready
  fake dead, observe one power-on heartbeat, and assert state `BACKOFF`, a 30-second
  deadline, and no second start before that deadline.
- `test_stop_during_connect_is_terminal_and_closes_transport`: stop while the fake
  start is blocked, release it, and assert state `STOPPED`, `stop_calls == 1`,
  `close_calls == 1`, and no later observation starts work.

The connect-budget assertion for repeated heartbeats is:

```python
for _ in range(30):
    session.observe_power(reachable=True, power_state="on", reachable_edge=False)
    await asyncio.sleep(0)
assert art.start_listening.await_count == 1
```

before the deterministic clock advances to the first retry deadline.

- [ ] **Step 3: Run the new module tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q tests/test_art_session.py
```

Expected: collection fails because `art_session.py` does not exist.

- [ ] **Step 4: Implement the public types and constructor**

Use these exact public types:

```python
class ArtSessionState(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    READY = "ready"
    BACKOFF = "backoff"
    DORMANT = "dormant"


class ArtSessionTrigger(StrEnum):
    BACKGROUND = "background"
    POWER_EDGE = "power_edge"
    USER = "user"


type StateCallback = Callable[[ArtSessionState], None]
type Clock = Callable[[], float]
type Jitter = Callable[[float], float]


def _default_jitter(delay: float) -> float:
    spread = delay * ART_RETRY_JITTER
    return random.uniform(delay - spread, delay + spread)
```

`ArtSession.__init__` must accept:

```python
def __init__(
    self,
    art: FrameArt,
    *,
    task_factory: TaskFactory,
    state_callback: StateCallback | None = None,
    clock: Clock = time.monotonic,
    jitter: Jitter = _default_jitter,
) -> None:
```

Store one `_connect_task`, one per-attempt `_close_task`, one shared `_stop_task`,
`_started`, `_failure_count`, `_host_failure_count`, `_next_retry_at`,
`_last_reachable`, and `_generation`. Initial and terminal state is `STOPPED`.

Expose read-only `state`, `generation`, and `ready` properties; `ready` is true only
when state is `READY` and `FrameArt.is_alive()` is true. Add:

```python
def set_state_callback(self, callback: StateCallback | None) -> None:
    self._state_callback = callback
```

Every real state change invokes the current callback once; assigning a callback does
not replay the current state.

- [ ] **Step 5: Implement single-flight connection and state transitions**

`async_start()` sets `_started = True`, resets counters, and enters `BACKOFF` with
`_next_retry_at = 0` without opening.

`observe_power(reachable, power_state, reachable_edge)` must:

```python
if not self._started:
    return
if reachable_edge:
    self._reset_failures()
    self._next_retry_at = 0
if not reachable or power_state != "on":
    return
if self.state is ArtSessionState.READY and not self._art.is_alive():
    self._record_failure(ConnectionFailure("Art receiver stopped"))
if self._background_attempt_due():
    self._schedule_connect(ArtSessionTrigger.POWER_EDGE if reachable_edge else ArtSessionTrigger.BACKGROUND)
```

`async_ensure_ready(trigger)` shares `_connect_task`. Only `USER` and
`POWER_EDGE` bypass a future retry deadline, each by creating at most one task.
`_connect_once()` sets `CONNECTING`, calls `await self._art.start_listening()`,
requires `self._art.is_alive()`, then enters `READY`, increments generation, resets
all counters, and invokes the state callback. It catches
`ArtHostUnavailable` separately, records the hostless delay/state, closes the
transport, and returns `False`; cancellation propagates after cleanup.

`_record_failure()` must index the exact delay tuples without overflow. On the third
consecutive `ArtHostUnavailable`, set `DORMANT` and
`_next_retry_at = clock() + ART_DORMANT_SECONDS`. Other failures set `BACKOFF`
and use `_jitter(delay)`.

`async_connection_failed(error)` records failure and joins one shared physical
close unless the session is stopped. Failure cleanup and terminal shutdown must
share the per-attempt close task so a race never closes the same generation twice;
a new connect waits for prior close completion. The physical close has an outer
`ART_CLOSE_DEADLINE` that includes lifecycle-lock acquisition.

`async_stop()` creates one task-factory-owned completion shared and shielded by all
callers. It marks the adapter stopped before draining the connect task, bounds that
drain by `ART_CONNECT_DEADLINE`, joins the bounded close task, and publishes
`STOPPED` in terminal cleanup. Caller, stop-owner, close-owner, and repeated connect
cancellation must not abandon or poison cleanup. If a cancellation-resistant
connect returns after the terminal close, it performs one forced late close and
cannot publish `READY`.

- [ ] **Step 6: Verify GREEN, deterministic timing, and no leaked tasks**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q tests/test_art_session.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q tests/test_frame_art.py tests/test_art_session.py
```

Expected: all tests pass; test output contains no pending-task or unclosed-socket
warning.

- [ ] **Step 7: Commit the independently unused, green supervisor**

```bash
git add custom_components/samsungtv_frame/art_session.py \
  custom_components/samsungtv_frame/const.py tests/test_art_session.py
git commit -m "feat: supervise frame art session recovery"
```

---

### Task 3: Route Runtime Art Operations Through the Session

**Files:**
- Modify: `custom_components/samsungtv_frame/frame_art.py:157-200`
- Modify: `custom_components/samsungtv_frame/device.py:20-434`
- Modify: `tests/test_frame_art.py`
- Modify: `tests/test_device.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `ArtSession` and `ArtSessionTrigger` from Task 2.
- Produces on `FrameDevice`: `art_ready`, `art_generation`, `art_session_state`,
  `observe_art_power()`, `set_art_session_state_callback()`, and
  `async_start_art_session()`.
- Preserves all existing public Art operation method names used by entities and
  services.

- [ ] **Step 1: Write ready-only transport and facade tests**

Add red-first tests with these exact arrangements and assertions:

- `test_request_does_not_open_when_receiver_is_not_ready`: create `FrameArt`, patch
  `start_listening`, call `request("get_artmode_status")`, expect
  `ConnectionFailure("Art session is not ready")`, and assert start was not awaited.
- `test_upload_does_not_open_when_receiver_is_not_ready`: create `FrameArt`, patch
  `start_listening`, call `upload(...)`, expect the same exact not-ready failure,
  and assert neither a start nor an upload wire/D2D helper was attempted.
- `test_background_art_getter_returns_none_without_session_open`: set session
  `ready=False`, call `async_get_artmode()`, assert `None`, and assert both
  `async_ensure_ready` and `FrameArt.get_artmode` were not awaited.
- `test_user_mutation_requests_one_user_probe_and_executes_once`: make
  `async_ensure_ready(USER)` return true, call `async_set_artmode(True)`, and assert
  one ensure plus one `set_artmode(True)` await.
- `test_user_mutation_failure_is_not_retried`: raise `OSError` from
  `set_artmode`, assert the error propagates, operation await count is one, and
  `async_connection_failed` receives the same error once.
- `test_upload_failure_is_not_retried`: make the USER readiness check return true,
  raise after one `upload` await, and assert one readiness check, one upload, the
  same error reported once to the session and propagated, with no retry.
- `test_ready_operation_failure_is_reported_to_session`: make a ready background
  getter fail and assert `None` plus one `async_connection_failed` await.
- `test_observe_art_power_delegates_without_awaiting_network`: call the synchronous
  facade and assert `ArtSession.observe_power(True, "on", True)` exactly once.
- `test_device_stop_stops_session_and_remote_once`: invoke `async_stop()` twice and
  concurrently, and assert all callers await one shared bounded shutdown with one
  session stop and one remote close total. Cancelling one waiter must not abandon
  the shared shutdown.

For the no-hidden-open regressions, patch `FrameArt.start_listening` and assert it is
not awaited when either `request()` or `upload()` is called without a live receiver.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q \
  tests/test_frame_art.py::test_request_does_not_open_when_receiver_is_not_ready \
  tests/test_frame_art.py::test_upload_does_not_open_when_receiver_is_not_ready \
  tests/test_device.py
```

Expected: the new request and upload tests show the current hidden
`start_listening()` calls; new device tests fail because no session facade exists.

- [ ] **Step 3: Make `FrameArt.request()` and `FrameArt.upload()` ready-only**

Replace the hidden start with an explicit precondition while retaining the operation
lock and correlation code:

```python
async with self._operation_lock:
    self._raise_if_stopped()
    if not self.is_alive():
        raise ConnectionFailure("Art session is not ready")
    return await self._request_unlocked(
        request,
        expected_sub_event=expected_sub_event,
        request_id=request_id,
        **params,
    )
```

Apply the same `is_alive()` precondition inside `upload()`'s existing operation
lock, replacing its hidden `start_listening()` call. Keep its direct
`_request_unlocked()` calls because the operation lock is not reentrant, and do not
change either D2D or websocket-binary upload implementation.

Update existing transport tests to call `await art.start_listening()` before issuing
commands; do not weaken response-correlation assertions. Transport-local fatal
cleanup (request/upload timeouts and receiver exit) remains allowed; session
ownership forbids independent runtime opens and facade-initiated reset/recovery.

- [ ] **Step 4: Construct and expose the session in `FrameDevice`**

After creating `FrameArt`, create:

```python
self._art_session = ArtSession(
    self._art,
    task_factory=task_factory,
)
```

Add thin properties/delegates:

```python
@property
def art_ready(self) -> bool:
    return self._art_session.ready

@property
def art_generation(self) -> int:
    return self._art_session.generation

@property
def art_session_state(self) -> ArtSessionState:
    return self._art_session.state

def observe_art_power(self, reachable: bool, power_state: str | None, reachable_edge: bool) -> None:
    self._art_session.observe_power(reachable, power_state, reachable_edge)

async def async_start_art_session(self) -> None:
    await self._art_session.async_start()
```

`set_art_session_state_callback(callback: StateCallback | None)` delegates to the
session callback setter.

- [ ] **Step 5: Separate background reads from one-shot user mutations**

Replace retrying `_async_art_command` with two helpers:

```python
async def _async_art_read(self, operation: Callable[[], Awaitable[Any]]) -> Any:
    if self._stopped or not self._art_session.ready:
        return None
    try:
        return await operation()
    except Exception as err:
        await self._art_session.async_connection_failed(err)
        return None


async def _async_art_mutation(self, operation: Callable[[], Awaitable[Any]]) -> Any:
    if self._stopped:
        raise ConnectionFailure("Art device is stopped")
    if not await self._art_session.async_ensure_ready(ArtSessionTrigger.USER):
        raise ConnectionFailure("Art session is unavailable")
    try:
        return await operation()
    except Exception as err:
        await self._art_session.async_connection_failed(err)
        raise
```

Art mode, current art, brightness, and color-temperature getters use
`_async_art_read`. Preserve `None` from an unavailable Art-mode read instead of
turning it into `False`. Thumbnail remains a ready-gated special read that logs and
returns `None` without reporting D2D failures to the session, preserving its
existing failure isolation. All mutations, including upload, use
`_async_art_mutation` exactly once; protocol-local fallbacks inside one low-level
delegate call remain unchanged.

Task 4 owns the remaining coordinator/setup caller cutover. To keep this commit
runnable, retain temporary compatibility shims until Task 4: the optional
`attempts` argument is accepted but ignored, `listener_alive` delegates to
`art_ready`, and legacy start/restart methods arm the session and request a
deadline-respecting `BACKGROUND` ensure without directly opening or closing the
transport. Task 4 removes those shims and callers together. Remove all retrying
facade close/reset logic now.

`FrameDevice.async_stop()` uses one task-factory-owned shared shutdown task. Every
concurrent/repeated caller shields and awaits it; cancelling a waiter cannot abandon
cleanup. The owned shutdown marks the device stopped and stops the Art session and
remote once, concurrently, within existing deadlines.

- [ ] **Step 6: Verify the transport/device suites and full existing regressions**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q \
  tests/test_frame_art.py tests/test_art_session.py tests/test_device.py tests/test_init.py
```

Expected: all selected tests pass. No mutation test may show an operation await count
greater than one.

- [ ] **Step 7: Commit the runtime ownership cutover**

```bash
git add custom_components/samsungtv_frame/frame_art.py \
  custom_components/samsungtv_frame/device.py tests/test_frame_art.py \
  tests/test_device.py tests/conftest.py
git commit -m "refactor: route art operations through session"
```

---

### Task 4: Push Cache and READY-Only Coordinator Reconciliation

**Files:**
- Modify: `custom_components/samsungtv_frame/art_session.py`
- Modify: `custom_components/samsungtv_frame/device.py`
- Modify: `custom_components/samsungtv_frame/coordinator.py:39-440`
- Modify: `custom_components/samsungtv_frame/__init__.py:12-34`
- Modify: `tests/test_art_session.py`
- Modify: `tests/test_device.py`
- Modify: `tests/test_coordinator.py`
- Modify: `tests/test_init.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `FrameDevice.observe_art_power()`, `art_ready`, `art_generation`, and
  `ArtSessionState` from Tasks 2-3.
- Produces: `FrameCoordinator.handle_art_session_state()` and cached, READY-only
  `_async_reconcile_art()` behavior.
- Removes together: coordinator `restart_listener`, `_listener_task`,
  `_async_kick_listener_restart()`, `_restart_listener_safe()`; device
  `listener_alive`, `async_start_art_listener()`, `async_restart_art_listener()`;
  and the temporary `attempts` argument from `async_get_artmode()`.

- [ ] **Step 1: Write the coordinator regression tests first**

Add separate tests with a controllable clock and these exact assertions:

- `test_healthy_heartbeats_use_cached_art_without_art_io`: seed generation `1`, run
  the initial reconciliation, poll six more times below 300 seconds, and assert each
  Art getter was awaited exactly once total.
- `test_new_ready_generation_reconciles_once`: change generation from `1` to `2`
  below the time deadline and assert one additional set of getter awaits.
- `test_reconcile_is_spaced_300_seconds`: hold generation stable, assert no read at
  `299.9`, advance to `300.0`, and assert one read.
- `test_reconcile_never_runs_when_session_not_ready`: set `art_ready=False` with a
  new generation and assert all Art getters remain unawaited.
- `test_dead_listener_observation_does_not_parallel_art_query`: set the session not
  ready, poll once, assert one `observe_art_power` call and zero Art getters.
- `test_reachable_edge_delegates_one_power_trigger`: transition REST from unreachable
  to on and assert `observe_art_power(True, "on", True)` once.
- `test_initial_on_observation_schedules_due_session_probe`: arm a real fake-backed
  `ArtSession`, observe initial reachable/on with `reachable_edge=False`, and assert
  its zero-deadline BACKOFF schedules exactly one entry-owned connection attempt.
- `test_cold_standby_observation_allows_initial_probe`: arm the same session, observe
  reachable/standby, and assert one supervised probe. This preserves 2025 Frames
  whose REST power is `standby` during normal Art mode.
- `test_session_ready_callback_schedules_one_refresh`: invoke the READY callback
  twice while its refresh is blocked and assert one entry-owned refresh task.
- `test_push_updates_cache_between_reconciliations`: push `art_mode_changed=on` and
  `image_selected`, then poll below 300 seconds; assert pushed values remain and no
  getters run.
- `test_reconcile_does_not_overwrite_newer_push`: block a due mode read, deliver
  newer mode and image-selection pushes, release stale getter responses, and assert
  field revisions preserve the pushed cache.
- `test_unavailable_session_becomes_unknown_without_network_attempts`: start from a
  stable Art cache, keep REST on and session unavailable for
  `ART_FAIL_UNKNOWN_COUNT` polls, assert `TvMode.UNKNOWN`, zero Art getter awaits,
  and no open-capable or listener-restart work.
- `test_failed_due_reconcile_counts_once`: make the due mode read return `None` and
  readiness fall false; assert that heartbeat increments the failure streak once,
  not both as reconciliation failure and generic unavailability.
- `test_reachable_edge_and_off_reset_failure_episode`: prove a new reachability/power
  episode receives a fresh exact `ART_FAIL_UNKNOWN_COUNT` budget.
- `test_push_resets_failure_streak`: any recognized live Art push resets the stale
  failure episode without opening or reconciling.
- `test_push_art_on_then_learned_standby_is_off`: from latest REST power `on`, push
  Art on, then poll REST `standby`; assert the learned 2022-24 trait makes standby
  win as OFF and no READY reconciliation runs during shutdown.
- `test_unreachable_ready_session_never_reconciles`: leave the socket fake READY but
  REST unreachable and due; assert zero Art getter awaits.
- `test_cancelled_reconcile_reserves_window`: cancel/time out a reconciliation after
  its first blocked getter, then poll again inside 300 seconds and assert no second
  reconciliation for the same generation.
- `test_reachable_heartbeat_captures_token_while_art_unavailable`: with
  `art_ready=False`, assert the cheap token capture/update still occurs.
- Setup tests must cover callback wiring order, session arming before first refresh,
  bounded device cleanup on start/first-refresh failure, and state-callback removal
  during unload.

The central steady-state assertion must run multiple polls and assert all four Art
getter mocks remain at the count from the initial reconciliation:

```python
for _ in range(6):
    await coord._async_update_data()
assert mock_device.async_get_artmode.await_count == 1
assert mock_device.async_get_current_art.await_count == 1
assert mock_device.async_get_art_brightness.await_count == 1
assert mock_device.async_get_color_temperature.await_count == 1
```

- [ ] **Step 2: Run the coordinator/setup tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q \
  tests/test_coordinator.py tests/test_init.py
```

Expected: existing per-poll getter expectations conflict with the new tests, and the
new session callbacks/delegates are absent.

- [ ] **Step 3: Replace listener restart ownership with session observations**

Allow `ArtSession.observe_power()` to treat both `on` and `standby` as Art-capable;
the retry budget makes the cold standby probe safe. In `_async_poll()`, after REST
power state is known, suppress a known 2022-24 shutdown standby while preserving a
cold/2025 standby probe:

```python
was_reachable = self._was_reachable
self._was_reachable = reachable
reachable_edge = reachable and not was_reachable
session_power_state = (
    None
    if power_state == "standby" and self._art_implies_power_on
    else power_state
)
self.device.observe_art_power(reachable, session_power_state, reachable_edge)
```

Delete coordinator listener-restart fields and methods. Retain app-fetch reset on a
reachable edge. Remove the temporary device listener shims and getter `attempts`
argument in this same commit so no broken intermediate interface remains.

- [ ] **Step 4: Add generation-based seed and 300-second reconciliation**

Initialize:

```python
self._art_generation = -1
self._next_art_reconcile = 0.0
self._clock = time.monotonic
self._art_ready_refresh_task: asyncio.Task | None = None
self._art_mode_revision = 0
self._current_art_revision = 0
```

Use this gate only after the power observation:

```python
generation = self.device.art_generation
reconcile_due = (
    reachable
    and self.device.art_ready
    and power_state in {"on", "standby"}
    and not (
        power_state == "standby" and self._art_implies_power_on
    )
    and (
        generation != self._art_generation
        or self._clock() >= self._next_art_reconcile
    )
)
if reconcile_due:
    await self._async_reconcile_art(generation)
```

`_async_reconcile_art()` reserves `_art_generation` and the next 300-second deadline
*before* its first await, then calls the four existing getters sequentially once.
This reservation survives outer poll cancellation, so the next heartbeat cannot
start another full reconciliation for the same generation. Capture per-field
revisions before I/O; do not overwrite mode/current-art fields changed by a newer
push while getters were awaiting. Do not parallelize reads.

Return whether the mode sample was live (`None` is failure unless superseded by a
newer push). Update the existing failure streak in one mutually exclusive branch per
heartbeat: successful mode read or recognized push resets; a failed due read counts
once; otherwise reachable/power-on plus unavailable session counts once. A reachable
edge or non-power-on/unreachable heartbeat resets the consecutive episode.

Keep `_art_mode` as the last pushed/reconciled cache. At
`ART_FAIL_UNKNOWN_COUNT`, derive and export with an effective Art signal of `None`
without destroying that cache; this is what actually produces `TvMode.UNKNOWN`.
Below the threshold, preserve the existing last-stable hold. Keep cheap token capture
on every reachable heartbeat, independent of reconciliation/readiness.

- [ ] **Step 5: Schedule one refresh on READY**

Add:

```python
@callback
def handle_art_session_state(self, state: ArtSessionState) -> None:
    if state is not ArtSessionState.READY:
        return
    if self._art_ready_refresh_task is not None and not self._art_ready_refresh_task.done():
        return
    self._art_ready_refresh_task = self.config_entry.async_create_background_task(
        self.hass,
        self.async_request_refresh(),
        f"{DOMAIN}-art-ready-refresh",
    )
```

In push handling, increment the changed field revision and reset Art failure state.
An Art-on push received while the latest REST power is `on` learns
`_art_implies_power_on`; later standby then wins as shutdown.

In setup, wire event then state callbacks, call
`await device.async_start_art_session()`, then run the first coordinator refresh.
Do not directly start the Art listener. If start or first refresh fails, clear the
state callback and run bounded `device.async_stop()` before re-raising. Clear the
state callback during unload (restore it if platform unload fails) so late READY
cannot schedule a refresh across teardown.

- [ ] **Step 6: Update old polling tests without weakening state semantics**

Tests that expected `async_get_artmode(attempts=1)` or
`async_get_artmode(attempts=2)` every heartbeat must instead
set `mock_device.art_ready`/`art_generation`, control the clock, and assert cached
state or due reconciliation. Preserve all OFF debounce, standby-trait, app detection,
volume, token capture, wake probe, and push-event assertions. Fixture readiness
defaults remain false so hidden Art I/O cannot pass accidentally.

- [ ] **Step 7: Verify coordinator, setup, and entity behavior**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q \
  tests/test_art_session.py tests/test_device.py tests/test_coordinator.py \
  tests/test_init.py tests/test_binary_sensor.py \
  tests/test_sensor.py tests/test_switch.py tests/test_image.py \
  tests/test_number.py tests/test_media_player.py
```

Expected: all selected tests pass; no heartbeat test causes an Art open or more than
one reconciliation inside 300 seconds.

- [ ] **Step 8: Commit the coordinator destination architecture**

```bash
git add custom_components/samsungtv_frame/coordinator.py \
  custom_components/samsungtv_frame/__init__.py \
  custom_components/samsungtv_frame/art_session.py \
  custom_components/samsungtv_frame/device.py tests/test_art_session.py \
  tests/test_device.py tests/test_coordinator.py tests/test_init.py \
  tests/conftest.py
git commit -m "fix: pace frame art recovery and polling"
```

---

### Task 5: Release Metadata and Verification Gates

**Files:**
- Modify: `custom_components/samsungtv_frame/manifest.json:24`
- Modify: `README.md:40-55`
- Verify: all production and test files changed by Tasks 1-4

**Interfaces:**
- Produces: HACS-installable release `0.6.8` with no config migration.
- Consumes: all completed, task-reviewed implementation commits.

- [ ] **Step 1: Run the full suite before release metadata**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q
```

Expected: the entire suite passes with no warnings about pending tasks, unclosed
sockets, unhandled futures, or leaked writers.

- [ ] **Step 2: Update release metadata and README**

Set manifest version exactly:

```json
"version": "0.6.8"
```

Add a `0.6.8` reliability paragraph immediately before the existing `0.6.7`
upgrade note:

```markdown
Version 0.6.8 supervises the Art websocket as one long-lived session. When the TV's
internal Art host is unavailable, Home Assistant now backs off instead of reconnecting
on every heartbeat; healthy state remains push-driven with periodic reconciliation over
the existing socket. No configuration migration is required when upgrading.
```

- [ ] **Step 3: Run final local verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q
git diff --check
git status --short
```

Expected: all tests pass, `git diff --check` is silent, and status lists only the
intentional plan/spec, implementation, tests, README, and manifest changes.

- [ ] **Step 4: Commit release metadata**

```bash
git add README.md custom_components/samsungtv_frame/manifest.json \
  docs/superpowers/specs/2026-07-13-art-session-recovery-design.md \
  docs/superpowers/plans/2026-07-13-art-session-recovery.md
git commit -m "release: v0.6.8 supervised art recovery"
```

- [ ] **Step 5: Complete review gates before publishing**

Generate a whole-branch review package from the pre-task base and obtain:

1. task review after every implementation task;
2. whole-branch review using the most capable available subagent;
3. final external Opus review through `claude-lesina --model opus -p` using public
   code and sanitized evidence only;
4. a final full pytest run after every Important/Critical review fix.

Expected: no unresolved Critical or Important finding.

- [ ] **Step 6: Publish and deploy through HACS**

After verification, push the branch, tag `v0.6.8`, publish the GitHub release, refresh
the existing HACS custom repository, install `0.6.8`, and restart/reload Home
Assistant through the supervised production console. Do not copy a tar archive into
`custom_components`.

- [ ] **Step 7: Run the live acceptance matrix**

Verify, in order:

1. HA frontend root and static assets respond before and after reload;
2. Samsung entry is `loaded` and all nine existing entities are available;
3. TV in Art reports media player `on`, Art binary sensor/switch `on`, and TV mode
   `art_mode`;
4. one isolated secure Art probe sees `isHost: true`, `ready`, and status `on`;
5. WATCHING-to-ART and ART-to-WATCHING pushes update without waiting for heartbeat;
6. one full TV power cycle performs at most one immediate edge probe and paced
   recovery thereafter;
7. clean entry unload/reload leaves no pending task or socket warning;
8. a production soak shows no repeating listener warning and no Art handshake storm.

If any hard gate fails, reinstall `v0.6.7` through HACS, restart Core, and power-cycle
the TV only if its Art service is already hostless.
