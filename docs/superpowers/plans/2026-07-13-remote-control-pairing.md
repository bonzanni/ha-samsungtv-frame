# Remote-Control Pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the token issued by `samsung.remote.control` the canonical TV credential, persist it before foreground commands return, and replace the runtime tokenless fallback with Home Assistant reauthorization.

**Architecture:** Add a bounded native-async `FrameRemote` adapter that owns remote handshake cleanup and classifies `ms.channel.timeOut` without mutating credentials. Config flow pairs remote first and validates Art with the returned token; runtime callbacks synchronously persist newer remote tokens and start reauthorization only from failed user commands.

**Tech Stack:** Python 3.14, Home Assistant config-entry/config-flow APIs, `asyncio`, `websockets`, `samsungtvws[async,encrypted]==3.0.5`, pytest, pytest-homeassistant-custom-component.

## Global Constraints

- Keep the v0.6.8 Art transport, request framing, handshake rules, retry pacing, supervised session, D2D transfers, and mutation semantics unchanged.
- Keep one config-entry `token` value and config-flow version `1`; do not add separate Art/remote token fields or a migration.
- Pair `samsung.remote.control` first with client name `Home Assistant`, require its returned token, and validate `com.samsung.art-app` with that same token before setup or reauthorization succeeds.
- Treat `ms.channel.timeOut` as an indeterminate reauthorization condition, never as permission to erase credentials or automatically retry tokenless.
- Persist a changed remote-issued token synchronously before a successful foreground remote command returns.
- Never open a tokenless remote connection or authorization prompt from polling, app-list fetching, or another background operation.
- Ordinary stale remote failures receive at most one reconnect on the same credential; the captured failed client must be the client that is closed.
- Close temporary pairing sockets on success, failure, timeout, and cancellation; never log addresses, MACs, or token values.
- Use strict red-green-refactor for every production behavior change.
- Version the finished release as `0.6.9` and deploy it through the existing HACS custom repository.
- Invoke Claude only through the `claude-lesina` shell function.

---

## File Map

- Create `custom_components/samsungtv_frame/frame_remote.py`: bounded remote handshake, local socket cleanup, typed reauthorization signal.
- Create `tests/test_frame_remote.py`: real frame-shaped handshake and cleanup tests.
- Modify `custom_components/samsungtv_frame/config_flow.py`: pair remote first, validate Art with its token, add reauthorization.
- Modify `custom_components/samsungtv_frame/strings.json` and `translations/en.json`: pairing and reauthorization copy.
- Modify `tests/test_config_flow.py`: setup, reconfigure, cleanup, and reauthorization coverage.
- Modify `custom_components/samsungtv_frame/device.py`: use `FrameRemote`, remove tokenless swap, immediate token callback, one stale retry.
- Modify `custom_components/samsungtv_frame/coordinator.py` and `__init__.py`: persist callback, reauthorization callback, setup/unload lifecycle.
- Modify `tests/test_device.py`, `tests/test_coordinator.py`, `tests/test_init.py`, and `tests/conftest.py`: runtime ordering and lifecycle regressions.
- Modify `README.md`, `custom_components/samsungtv_frame/manifest.json`, and `CHANGELOG.md`: v0.6.9 release.

---

### Task 1: Bounded Native-Async Remote Handshake

**Files:**
- Create: `custom_components/samsungtv_frame/frame_remote.py`
- Create: `tests/test_frame_remote.py`
- Modify: `custom_components/samsungtv_frame/const.py`

**Interfaces:**
- Produces: `class RemotePairingRequired(ConnectionFailure)`.
- Produces: `FrameRemote(host, *, token, ssl_context, port=PORT_WS, name=CLIENT_NAME, timeout=8)` with inherited `send_commands()`, `app_list()`, `token`, and `close()` behavior.
- Consumed by: config flow in Task 2 and `FrameDevice` in Task 3.

- [ ] **Step 1: Write real frame-shaped RED tests**

Create `tests/test_frame_remote.py` with a controllable websocket double matching the real `recv()`, `send()`, `close()`, and `State` surface. Add tests proving:

```python
async def test_open_captures_token_after_ignored_broadcasts():
    ws = FakeWebSocket([
        {"event": "ms.channel.clientConnect"},
        {"event": "ms.channel.connect", "data": {"token": "remote-token"}},
    ])
    remote = make_remote()
    with patch("custom_components.samsungtv_frame.frame_remote.connect", AsyncMock(return_value=ws)):
        assert await remote.open() is ws
    assert remote.token == "remote-token"
    await remote.close()


async def test_timeout_event_requires_reauth_and_closes_local_socket():
    ws = FakeWebSocket([{"event": "ms.channel.timeOut"}])
    remote = make_remote()
    with (
        patch("custom_components.samsungtv_frame.frame_remote.connect", AsyncMock(return_value=ws)),
        pytest.raises(RemotePairingRequired),
    ):
        await remote.open()
    assert ws.closed
    assert remote.connection is None
```

Add a parameterized test with `ms.channel.unauthorized` expecting
`UnauthorizedError` and `unexpected` expecting `ConnectionFailure`; each case
must assert `ws.closed is True` and `remote.connection is None`. Add a delayed
`recv()` case under `timeout=0.01` expecting `TimeoutError` with the same cleanup
assertions. Assert secure `connect()` receives the exact injected SSL context
and `open_timeout`, and two concurrent `open()` calls share one `connect()`
call through an instance lock.

- [ ] **Step 2: Run the transport tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_frame_remote.py
```

Expected: collection fails because `frame_remote`, `FrameRemote`, and
`RemotePairingRequired` do not exist.

- [ ] **Step 3: Add the bounded transport implementation**

Add `REMOTE_CLOSE_DEADLINE = 5` to `const.py`. Implement
`frame_remote.py` using `SamsungTVWSAsyncRemote` and the same healthy URL and
command framing supplied by `samsungtvws`. Its `open()` must:

```python
async with self._open_lock:
    if self.connection is not None:
        return self.connection
    websocket = None
    try:
        deadline = self.timeout or 8
        async with asyncio.timeout(deadline):
            websocket = await connect(
                self._format_websocket_url(self.endpoint),
                open_timeout=deadline,
                ssl=self._ssl_context,
            )
            event = None
            while event is None or event in IGNORE_EVENTS_AT_STARTUP:
                frame = helper.process_api_response(await websocket.recv())
                event = frame.get("event", "*")
                self._websocket_event(event, frame)
            if event == MS_CHANNEL_UNAUTHORIZED:
                raise UnauthorizedError(frame)
            if event == "ms.channel.timeOut":
                raise RemotePairingRequired("Remote authorization required")
            if event != MS_CHANNEL_CONNECT_EVENT:
                raise ConnectionFailure(frame)
            self._check_for_token(frame)
        self.connection = websocket
        return websocket
    except BaseException:
        if websocket is not None:
            with contextlib.suppress(Exception, TimeoutError):
                async with asyncio.timeout(REMOTE_CLOSE_DEADLINE):
                    await websocket.close()
        self.connection = None
        raise
```

Do not override inherited command or app-list framing.

- [ ] **Step 4: Verify GREEN and neighboring transport tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_frame_remote.py tests/test_frame_art.py
```

Expected: both transport suites pass with no unclosed socket/task warnings.

- [ ] **Step 5: Commit Task 1**

```bash
git add custom_components/samsungtv_frame/frame_remote.py \
  custom_components/samsungtv_frame/const.py tests/test_frame_remote.py
git commit -m "fix: own remote control handshake lifecycle"
```

---

### Task 2: Canonical Setup and Reauthorization Pairing

**Files:**
- Modify: `custom_components/samsungtv_frame/config_flow.py`
- Modify: `custom_components/samsungtv_frame/strings.json`
- Modify: `custom_components/samsungtv_frame/translations/en.json`
- Modify: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `FrameRemote` and `RemotePairingRequired` from Task 1.
- Preserves: `validate_and_pair(hass, host) -> dict[str, Any]` and config-flow version `1`.
- Produces: `async_step_reauth()` and `async_step_reauth_confirm()`.
- The returned `CONF_TOKEN` is always the non-empty remote-issued token.

- [ ] **Step 1: Replace Art-only pairing expectations with RED canonical tests**

Update the test helper to supply both a mocked `FrameRemote` and `FrameArt`.
Add focused tests asserting:

```python
async def test_pair_remote_token_is_used_to_validate_art(hass):
    ssl_context = object()
    remote = MagicMock(token="remote-token", open=AsyncMock(), close=AsyncMock())
    art = MagicMock(token="ignored-art-token", open=AsyncMock(), close=AsyncMock())
    with pairing_patches(
        hass, remote=remote, art=art, ssl_context=ssl_context
    ) as art_constructor:
        result = await validate_and_pair(hass, "1.2.3.4")
        assert result[CONF_TOKEN] == "remote-token"
        remote.open.assert_awaited_once()
        art_constructor.assert_called_once_with(
            "1.2.3.4", token="remote-token", ssl_context=ssl_context,
            task_factory=None, event_callback=None, timeout=PAIRING_DEADLINE,
        )
        art.open.assert_awaited_once()
        remote.close.assert_awaited_once()
        art.close.assert_awaited_once()
```

Implement `pairing_patches()` as a test-local context manager over the existing
complete REST Frame response, `FrameRemote`, `FrameArt`, `get_ssl_context`, and
`hass.async_add_executor_job`. Add a missing-token case where `remote.open()`
succeeds with `remote.token is None`: expect `CannotConnect`, no `FrameArt`
construction, and one remote close. Parameterize remote-open and Art-open
failures with `OSError` and `asyncio.CancelledError`; `OSError` maps to
`CannotConnect`, cancellation propagates unchanged, and every created client
is closed exactly once.

For reauth success, create an existing `MockConfigEntry`, start a
`SOURCE_REAUTH` flow, submit `{}`, and patch `validate_and_pair()` to return the
same MAC/model plus `remote-token`; assert `reauth_successful`, the token update,
and scheduled reload. For reauth failure, raise `CannotConnect`, assert the
`reauth_confirm` form has `errors == {"base": "cannot_connect"}`, and assert the
stored token is unchanged.

- [ ] **Step 2: Run focused config-flow tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_config_flow.py
```

Expected: failures show setup still pairs Art first, accepts a missing token,
and has no reauthorization steps.

- [ ] **Step 3: Implement remote-first pairing and reauth**

Refactor `validate_and_pair()` after REST validation to:

```python
ssl_context = await hass.async_add_executor_job(get_ssl_context)
remote = FrameRemote(
    host, token=None, ssl_context=ssl_context,
    timeout=PAIRING_DEADLINE,
)
art = None
try:
    async with asyncio.timeout(PAIRING_DEADLINE):
        await remote.open()
    token = remote.token
    if not token:
        raise CannotConnect
    art = FrameArt(
        host, token=token, ssl_context=ssl_context,
        task_factory=None, event_callback=None,
        timeout=PAIRING_DEADLINE,
    )
    async with asyncio.timeout(PAIRING_DEADLINE):
        await art.open()
except Exception as err:
    raise CannotConnect from err
finally:
    await _async_close_pairing_client(art)
    await _async_close_pairing_client(remote)
```

Implement `_async_close_pairing_client()` with `contextlib.suppress(Exception,
TimeoutError)` and `asyncio.timeout(REMOTE_CLOSE_DEADLINE)` so cleanup cannot
replace the pairing result.

Add `async_step_reauth()` delegating to a confirm form. On form submission,
call `validate_and_pair()` with the existing host, verify the MAC through
`async_set_unique_id()` plus `_abort_if_unique_id_mismatch()`, and call
`async_update_reload_and_abort()` with `CONF_TOKEN` and `CONF_MODEL` updates.

Update setup/reconfigure so successful pairing adopts the returned canonical
token. Add `reauth_confirm` text instructing the user to show normal content
and approve the Allow prompt; add `reauth_successful` abort text to both JSON
translation files. Keep errors sanitized.

- [ ] **Step 4: Verify GREEN and JSON validity**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_config_flow.py
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/python -m json.tool \
  custom_components/samsungtv_frame/strings.json >/dev/null
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/python -m json.tool \
  custom_components/samsungtv_frame/translations/en.json >/dev/null
```

Expected: config-flow suite and both JSON parses pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add custom_components/samsungtv_frame/config_flow.py \
  custom_components/samsungtv_frame/strings.json \
  custom_components/samsungtv_frame/translations/en.json \
  tests/test_config_flow.py
git commit -m "fix: pair canonical remote control token"
```

---

### Task 3: Immediate Runtime Persistence and Reauthorization Signal

**Files:**
- Modify: `custom_components/samsungtv_frame/device.py`
- Modify: `custom_components/samsungtv_frame/coordinator.py`
- Modify: `custom_components/samsungtv_frame/__init__.py`
- Modify: `tests/test_device.py`
- Modify: `tests/test_coordinator.py`
- Modify: `tests/test_init.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `FrameRemote` and `RemotePairingRequired` from Task 1.
- Produces on `FrameDevice`: `set_remote_token_callback(callback | None)` and `set_remote_reauth_callback(callback | None)`.
- Produces on `FrameCoordinator`: `handle_remote_token(token: str)` and `handle_remote_reauth()` callbacks.
- Preserves: `update_token()` updates both remote and Art clients because the live TV accepted the canonical token on both endpoints.

- [ ] **Step 1: Write runtime RED tests for the real outcomes**

Replace the test that merely asserts a tokenless object swap. Add tests proving:

```python
async def test_remote_timeout_requests_reauth_without_retry(device):
    remote = MagicMock()
    error = RemotePairingRequired("remote authorization required")
    remote.send_commands = AsyncMock(side_effect=error)
    remote.close = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)
    with patch.object(device, "_remote", remote), pytest.raises(RemotePairingRequired):
        await device.async_send_key("KEY_HOME")
    remote.send_commands.assert_awaited_once()
    reauth.assert_called_once_with()


async def test_stale_remote_retry_closes_captured_client_once(device):
    remote = MagicMock()
    remote.send_commands = AsyncMock(side_effect=[OSError("stale"), None])
    remote.close = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")
    assert remote.send_commands.await_count == 2
    remote.close.assert_awaited_once()


async def test_successful_remote_command_persists_new_token_before_return(device):
    order = []
    remote = MagicMock(token="new-token")
    remote.send_commands = AsyncMock(side_effect=lambda commands: order.append("sent"))
    persist = MagicMock(side_effect=lambda token: order.append("persisted"))
    device.set_remote_token_callback(persist)
    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")
        order.append("returned")
    assert order == ["sent", "persisted", "returned"]
    persist.assert_called_once_with("new-token")
```

Also cover unchanged/no token, timeout on the second stale retry, background
`async_app_list()` never starting reauth, coordinator entry updates, duplicate
reauth suppression through Home Assistant, setup callback wiring, setup-failure
clearing, unload-failure restoration, and successful unload clearing callbacks
before device stop.

- [ ] **Step 2: Run focused runtime tests and verify RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_device.py tests/test_coordinator.py \
  tests/test_init.py
```

Expected: failures show `FrameDevice` still uses the library remote directly,
swaps tokenless on `timeOut`, defers token persistence to heartbeat, and has no
reauth callback lifecycle.

- [ ] **Step 3: Implement callback-owned runtime behavior**

Construct `FrameRemote` with the entry SSL context. Remove
`_remote_tokenless` and `_maybe_drop_rejected_remote_token()` entirely. Add
nullable synchronous callbacks and setters. After a successful foreground
remote command:

```python
self.remote_confirmed = True
token = self._remote.token
if token and token != self._token and self._remote_token_callback is not None:
    self._remote_token_callback(token)
```

Call this before returning. `async_app_list()` may capture a changed token only
after success; if it sees `RemotePairingRequired`, set `remote_confirmed = False`
and return `None` without starting reauth.

In `_async_remote_commands()`, handle `RemotePairingRequired` separately before
the generic stale retry. For a generic failure, save `remote = self._remote`,
close that exact object within the existing close deadline, retry once, and if
the retry raises `RemotePairingRequired`, start reauth and re-raise. Never create
a tokenless runtime client.

In the coordinator, make `handle_remote_token()` the sole persistence path for
remote-issued tokens: update both clients through `device.update_token()`, then
update `entry.data[CONF_TOKEN]`. Change heartbeat compatibility capture to read
only the remote token, not Art. `handle_remote_reauth()` calls
`config_entry.async_start_reauth(hass)`; Home Assistant suppresses duplicate
active flows.

Wire both callbacks in `async_setup_entry()`. Clear them on setup failure and
before successful unload/stop; restore them if platform unload fails or raises,
matching the existing Art state-callback lifecycle.

- [ ] **Step 4: Verify GREEN and the full integration suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q tests/test_device.py tests/test_coordinator.py \
  tests/test_init.py tests/test_media_player.py tests/test_remote.py
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
```

Expected: focused suites pass, then the full suite passes with no warning,
unclosed task, or socket output.

- [ ] **Step 5: Commit Task 3**

```bash
git add custom_components/samsungtv_frame/device.py \
  custom_components/samsungtv_frame/coordinator.py \
  custom_components/samsungtv_frame/__init__.py tests/test_device.py \
  tests/test_coordinator.py tests/test_init.py tests/conftest.py
git commit -m "fix: persist remote authorization before shutdown"
```

---

### Task 4: Release Documentation and Acceptance Contract

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `custom_components/samsungtv_frame/manifest.json`

**Interfaces:**
- Consumes: finished and reviewed Tasks 1-3.
- Produces: HACS release `0.6.9` and explicit reauthorization instructions.

- [ ] **Step 1: Write the release contract before changing metadata**

Update README setup/reliability text to state:

```text
Show normal TV content during first setup or reauthorization, accept the Allow
prompt, and leave Access Notification set to First Time Only. The integration
pairs the remote-control channel and validates Art with the returned token; it
never opens a pairing prompt from background polling.
```

Add a top `0.6.9` changelog entry covering remote-first pairing, immediate
token persistence before power-off, exact-client stale cleanup, and HA reauth
instead of silent tokenless retry. Do not claim `ms.channel.timeOut` proves an
invalid token.

- [ ] **Step 2: Bump and validate release metadata**

Set manifest `version` to `0.6.9`, then run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/python -m json.tool \
  custom_components/samsungtv_frame/manifest.json >/dev/null
git diff --check
```

Expected: JSON and whitespace checks pass.

- [ ] **Step 3: Run final pre-review verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest \
  -p no:cacheprovider -q
git status --short
```

Expected: the full suite passes and status lists only the three Task 4 files.

- [ ] **Step 4: Commit Task 4**

```bash
git add README.md CHANGELOG.md custom_components/samsungtv_frame/manifest.json
git commit -m "release: v0.6.9 canonical remote pairing"
```

- [ ] **Step 5: Complete review and production acceptance**

After task review and whole-branch review are clean, run Claude Opus through
`claude-lesina` on the complete v0.6.8..HEAD range and resolve every Critical
or Important defect through a reviewed TDD fix. Then push the branch, fast
forward `main`, create annotated tag `v0.6.9`, publish the GitHub release, let
HACS refresh/download `v0.6.9`, restart Core, and verify:

```text
entry loaded; nine entities healthy; reauth completed if requested; one full
turn_off/Wake-on-LAN cycle; automatic return to Art; exactly one established
secure Art socket; no Samsung task/socket warnings; HA frontend HTTP 200.
```
