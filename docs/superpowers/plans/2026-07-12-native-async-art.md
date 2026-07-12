# Native Async Art Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every synchronous Samsung art-protocol operation with bounded async I/O so a silent or chatty TV cannot leak Home Assistant executor threads or stall unload.

**Architecture:** Add an integration-owned `FrameArt` adapter on top of the pinned `SamsungTVWSAsyncConnection` construction helpers. It owns a single persistent websocket receiver, correlates request futures while delivering push events, and performs D2D transfers with asyncio streams. `FrameDevice` remains the coordinator-facing facade, while config-entry setup supplies HA-owned task creation and config flow uses a short-lived async pairing connection.

**Tech Stack:** Python 3.13+, Home Assistant config-entry APIs, `asyncio`, `websockets`, `samsungtvws[async,encrypted]==3.0.5`, pytest, pytest-homeassistant-custom-component.

## Global Constraints

- Keep `samsungtvws[async,encrypted]==3.0.5`; do not vendor the NickWaterton fork or add a second Samsung websocket dependency.
- No Samsung art request, listener, thumbnail, upload, pairing, or close operation may use `hass.async_add_executor_job()` after this change.
- Every websocket handshake, request wait, D2D connect/read/write, and close wait must have an absolute asyncio deadline.
- Runtime receiver and refresh tasks must be owned by `ConfigEntry.async_create_background_task` and cancelled with the entry.
- Preserve entity IDs, services, config-entry schema version `1`, stored token behavior, polling state derivation, and remote/REST/UPnP behavior.
- Retry one ordinary stale-connection failure; never retry a deadline timeout or an upload that may have partially completed.
- Version the release as `0.6.7`; install it in production through HACS custom repositories, not the previous tar-package path.
- Invoke Claude reviews only as `claude-lesina --model fable`; never invoke `claude` directly.

---

## File Map

- Create `custom_components/samsungtv_frame/frame_art.py`: websocket lifecycle, request correlation, art commands, async D2D thumbnail/upload.
- Modify `custom_components/samsungtv_frame/device.py`: delegate art work to `FrameArt`; retain remote, REST, UPnP, and Wake-on-LAN responsibilities.
- Delete `custom_components/samsungtv_frame/art_listener.py`: thread bridge is unnecessary because push callbacks now run on the HA event loop.
- Modify `custom_components/samsungtv_frame/__init__.py`: inject entry-owned task factory and wire the loop-native callback before first refresh.
- Modify `custom_components/samsungtv_frame/config_flow.py`: pair with a short-lived async `FrameArt` and close in `finally`.
- Modify `custom_components/samsungtv_frame/coordinator.py`: track standby refresh through the config entry and use native receiver liveness/restart.
- Modify `custom_components/samsungtv_frame/const.py`: replace sync-listener timing constants with explicit async handshake/request/D2D deadlines.
- Create `tests/test_frame_art.py`: focused fake-websocket and fake-stream transport tests.
- Modify `tests/test_device.py`, `tests/test_config_flow.py`, `tests/test_coordinator.py`, `tests/test_init.py`, and `tests/conftest.py`: facade and HA lifecycle regressions.
- Delete `tests/test_art_listener.py`: its decoding assertions move to loop-native receiver tests.
- Modify `custom_components/samsungtv_frame/manifest.json` and `README.md`: release version and async reliability note.

---

### Task 1: Async Websocket Lifecycle and Correlation Core

**Files:**
- Create: `custom_components/samsungtv_frame/frame_art.py`
- Create: `tests/test_frame_art.py`
- Modify: `custom_components/samsungtv_frame/const.py`

**Interfaces:**
- Consumes: `SamsungTVWSAsyncConnection`, its `_format_websocket_url()`, `_check_for_token()`, and `_websocket_event()` helpers from `samsungtvws==3.0.5`.
- Produces: `FrameArt(host, token, ssl_context, task_factory, event_callback)`, `set_event_callback()`, `async open()`, `async start_listening()`, `async request()`, `async close()`, `is_alive()`, and `token`.
- Produces type aliases `ArtEventCallback` and `TaskFactory`, used by runtime setup in Task 5.

- [ ] **Step 1: Add transport deadlines to constants**

Replace the sync listener constants with named async deadlines:

```python
ART_CONNECT_DEADLINE = 10
ART_REQUEST_DEADLINE = 20
ART_D2D_DEADLINE = 20
ART_CLOSE_DEADLINE = 5
PAIRING_DEADLINE = 30
```

Retain `ART_CALL_DEADLINE` temporarily until Task 4 removes the last sync facade code, so intermediate commits remain importable.

- [ ] **Step 2: Write failing handshake and lifecycle tests**

Create `tests/test_frame_art.py` with a controllable websocket and task factory:

```python
class FakeWebSocket:
    def __init__(self, frames):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(json.dumps(frame))
        self.sent = []
        self.closed = False
        self.state = State.OPEN

    async def recv(self):
        return await self.frames.get()

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True
        self.state = State.CLOSED


def task_factory(coroutine, name):
    return asyncio.create_task(coroutine, name=name)


def make_art(*, callback=None):
    return FrameArt(
        "1.2.3.4", token="tok", ssl_context=MagicMock(),
        task_factory=task_factory, event_callback=callback,
    )
```

Add tests that patch `custom_components.samsungtv_frame.frame_art.connect` and assert:

```python
async def test_open_ignores_broadcasts_captures_token_and_waits_for_ready():
    ws = FakeWebSocket([
        {"event": "ms.channel.clientConnect"},
        {"event": "ms.channel.connect", "data": {"token": "fresh"}},
        {"event": "ms.channel.clientDisconnect"},
        {"event": "ms.channel.ready"},
    ])
    art = make_art()
    with patch("custom_components.samsungtv_frame.frame_art.connect", AsyncMock(return_value=ws)):
        assert await art.open() is ws
    assert art.token == "fresh"
    assert not ws.closed


@pytest.mark.parametrize("event", ["ms.channel.unauthorized", "unexpected"])
async def test_open_closes_local_socket_on_failed_handshake(event):
    ws = FakeWebSocket([{"event": event}])
    art = make_art()
    with patch("custom_components.samsungtv_frame.frame_art.connect", AsyncMock(return_value=ws)):
        with pytest.raises((UnauthorizedError, ConnectionFailure)):
            await art.open()
    assert ws.closed
    assert art.connection is None
```

Also cover an endless broadcast stream under a patched short `ART_CONNECT_DEADLINE`, successful idempotent open, idempotent start, and `is_alive()` becoming false when the receiver exits.

- [ ] **Step 3: Run the new tests and verify they fail**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_frame_art.py -q
```

Expected: collection fails because `custom_components.samsungtv_frame.frame_art` does not exist.

- [ ] **Step 4: Implement the bounded handshake and one HA-owned receiver**

Create the module with these concrete structures:

```python
type ArtEventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
type TaskFactory = Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]]


@dataclass(slots=True)
class _PendingResponse:
    future: asyncio.Future[dict[str, Any]]
    expected_sub_event: str | None


class FrameArt(SamsungTVWSAsyncConnection):
    def __init__(self, host, *, token, ssl_context, task_factory, event_callback,
                 port=PORT_WS, name=CLIENT_NAME, timeout=ART_CONNECT_DEADLINE):
        super().__init__(host, endpoint=ART_ENDPOINT, token=token, port=port,
                         name=name, timeout=timeout)
        self._ssl_context = ssl_context
        self._task_factory: TaskFactory | None = task_factory
        self._event_callback = event_callback
        self._operation_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._pending: dict[str, _PendingResponse] = {}
        self._uuidless_pending: _PendingResponse | None = None
        self._closing = False

    async def open(self):
        async with self._lifecycle_lock:
            if self.connection is not None:
                return self.connection
            websocket = None
            try:
                async with asyncio.timeout(ART_CONNECT_DEADLINE):
                    kwargs = {"ssl": self._ssl_context} if self._is_ssl_connection() else {}
                    websocket = await connect(
                        self._format_websocket_url(self.endpoint),
                        open_timeout=ART_CONNECT_DEADLINE,
                        **kwargs,
                    )
                    await self._wait_for_handshake(websocket)
                self.connection = websocket
                return websocket
            except BaseException:
                if websocket is not None:
                    with contextlib.suppress(Exception, TimeoutError):
                        async with asyncio.timeout(ART_CLOSE_DEADLINE):
                            await websocket.close()
                self.connection = None
                raise

    async def start_listening(self):
        if self._task_factory is None:
            raise RuntimeError("A task factory is required to start the receiver")
        await self.open()
        if self._recv_loop is not None and not self._recv_loop.done():
            return
        self._closing = False
        self._recv_loop = self._task_factory(
            self._receive_loop(), f"{DOMAIN}-art-receiver"
        )

    def set_event_callback(self, callback: ArtEventCallback | None) -> None:
        self._event_callback = callback
```

`_wait_for_handshake()` must tolerate `IGNORE_EVENTS_AT_STARTUP` plus client connect/disconnect broadcasts before the connect acknowledgement and before `MS_CHANNEL_READY_EVENT`. It must call `_check_for_token()` on the acknowledgement and raise `UnauthorizedError` or `ConnectionFailure` for other terminal events.

The receiver must parse each websocket frame once, call `_websocket_event()`, decode D2D JSON, route responses, and invoke the callback for push payloads:

```python
async def _receive_loop(self):
    connection = self.connection
    try:
        while connection is not None:
            frame = helper.process_api_response(await connection.recv())
            event = frame.get("event", "*")
            self._websocket_event(event, frame)
            await self._dispatch_frame(event, frame)
    except asyncio.CancelledError:
        raise
    except Exception as err:
        LOGGER.debug("Art receiver exited: %s", err)
    finally:
        await self._receiver_finished(connection)
```

`_receiver_finished()` must close only the captured connection under
`ART_CLOSE_DEADLINE`, clear `self.connection` only when it still refers to that object,
fail all pending futures with `ConnectionFailure`, clear both pending containers, and
set `_recv_loop = None` when the current task owns it.

- [ ] **Step 5: Add and satisfy request-correlation tests**

Add tests for UUID correlation, UUID-less expected sub-events, error payloads, push/request coexistence, disconnect failure, and cancellation. The request assertion must inspect the outer command:

```python
request_task = asyncio.create_task(art.request("get_artmode_status"))
await asyncio.sleep(0)
outer = json.loads(ws.sent[-1])
inner = json.loads(outer["params"]["data"])
request_id = inner["request_id"]
await ws.frames.put(json.dumps({
    "event": "d2d_service_message",
    "data": json.dumps({"request_id": request_id, "value": "on"}),
}))
assert await request_task == {"request_id": request_id, "value": "on"}
```

Implement `request()` and dispatch with these invariants:

```python
async def request(self, request: str, *, expected_sub_event=None,
                  request_id=None, **params):
    async with self._operation_lock:
        await self.start_listening()
        return await self._request_unlocked(
            request, expected_sub_event=expected_sub_event,
            request_id=request_id, **params
        )
```

`_request_unlocked()` registers its future before sending `ArtChannelEmitCommand.art_app_request()`, adds both `id` and `request_id`, awaits under `ART_REQUEST_DEADLINE`, translates a D2D `event == "error"` to `ResponseError`, and calls `await self.close()` on timeout before re-raising `TimeoutError`. Its `finally` removes only its own pending registration.

- [ ] **Step 6: Implement idempotent bounded close**

Use a close path that never awaits itself:

```python
async def close(self):
    self._closing = True
    receiver = self._recv_loop
    self._recv_loop = None
    if receiver is not None and receiver is not asyncio.current_task():
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            async with asyncio.timeout(ART_CLOSE_DEADLINE):
                await receiver
    connection, self.connection = self.connection, None
    if connection is not None:
        with contextlib.suppress(Exception, TimeoutError):
            async with asyncio.timeout(ART_CLOSE_DEADLINE):
                await connection.close()
    self._fail_pending(ConnectionFailure("Art connection closed"))
```

`is_alive()` returns true only when the websocket exists, is not closed, and `_recv_loop` exists and is not done.

- [ ] **Step 7: Run transport tests and commit**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_frame_art.py -q
git diff --check
```

Expected: all Task 1 tests pass and `git diff --check` is silent.

Commit:

```bash
git add custom_components/samsungtv_frame/frame_art.py custom_components/samsungtv_frame/const.py tests/test_frame_art.py
git commit -m "feat: add cancellable async art transport"
```

---

### Task 2: Async Art Command Surface

**Files:**
- Modify: `custom_components/samsungtv_frame/frame_art.py`
- Modify: `tests/test_frame_art.py`

**Interfaces:**
- Consumes: `FrameArt.request()` and `_operation_lock` from Task 1.
- Produces async methods: `get_artmode()`, `set_artmode()`, `get_current()`, `get_brightness()`, `set_brightness()`, `get_color_temperature()`, `set_color_temperature()`, `select_image()`, `delete()`, `change_matte()`, `set_photo_filter()`, `set_favourite()`, and `set_slideshow()`.

- [ ] **Step 1: Write failing command payload and normalization tests**

Patch `FrameArt.request` with `AsyncMock` and assert exact requests, including:

```python
assert await art.get_artmode() == "on"
art.request.assert_awaited_with("get_artmode_status")

await art.set_artmode(True)
art.request.assert_awaited_with("set_artmode_status", value="on")

await art.select_image("MY_F0001", show=True)
art.request.assert_awaited_with(
    "select_image", category_id=None, content_id="MY_F0001", show=True
)

assert await art.delete("MY_F0001") is True
```

Cover boolean/on-off validation, numeric getters, nested JSON `get_artmode_settings`, delete response validation, favourite status, matte/filter payloads, and slideshow fallback only on `ResponseError`.

- [ ] **Step 2: Verify command tests fail**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_frame_art.py -q -k 'artmode or brightness or color or select or delete or matte or favourite or slideshow'
```

Expected: failures report missing `FrameArt` command methods.

- [ ] **Step 3: Implement the command methods as thin async protocol mappings**

Use small helpers, with no executor calls:

```python
@staticmethod
def _on_off(value: bool | str) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str) and value.lower() in {"on", "off"}:
        return value.lower()
    raise ValueError("Expected bool or 'on'/'off' string")

async def _get_value(self, request, key="value", **params):
    payload = await self.request(request, **params)
    return payload.get(key) if isinstance(payload, dict) else payload

async def set_artmode(self, value):
    return await self.request("set_artmode_status", value=self._on_off(value))

async def set_slideshow(self, duration, shuffle, category_id):
    params = {
        "value": str(duration) if duration > 0 else "off",
        "category_id": category_id,
        "type": "shuffleslideshow" if shuffle else "slideshow",
    }
    try:
        return await self.request("set_auto_rotation_status", **params)
    except ResponseError:
        return await self.request("set_slideshow_status", **params)
```

Mirror the pinned sync library’s request names exactly. Parsing errors return `None` only in the `FrameDevice` facade; transport methods raise so retry policy stays centralized.

- [ ] **Step 4: Run command tests and commit**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_frame_art.py -q
git diff --check
```

Expected: all transport and command tests pass.

Commit:

```bash
git add custom_components/samsungtv_frame/frame_art.py tests/test_frame_art.py
git commit -m "feat: implement async frame art commands"
```

---

### Task 3: Async Thumbnail and Upload Transfers

**Files:**
- Modify: `custom_components/samsungtv_frame/frame_art.py`
- Modify: `tests/test_frame_art.py`

**Interfaces:**
- Consumes: request registration and operation serialization from Tasks 1–2, injected `ssl.SSLContext`.
- Produces: `async get_thumbnail(content_id: str) -> bytes | None` and `async upload(data: bytes, file_type: str, matte: str) -> str`.

- [ ] **Step 1: Add fake stream helpers and failing thumbnail tests**

Use real asyncio stream-shaped fakes:

```python
class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False
        self.waited_closed = False
    def write(self, data): self.data.extend(data)
    async def drain(self): pass
    def close(self): self.closed = True
    async def wait_closed(self): self.waited_closed = True


def d2d_file(name="thumb", body=b"jpeg", num=0, total=1):
    header = json.dumps({
        "fileLength": len(body), "fileID": name,
        "fileType": "jpg", "num": num, "total": total,
    }).encode()
    return len(header).to_bytes(4, "big") + header + body
```

Patch `asyncio.open_connection`, feed `StreamReader` bytes, and test success, secured SSL argument, DRM `ResponseError` returning `None`, truncated header/body raising `IncompleteReadError`, timeout, cancellation, and `close()` plus `wait_closed()` in every path.

- [ ] **Step 2: Verify thumbnail tests fail**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_frame_art.py -q -k thumbnail
```

Expected: failures report missing async thumbnail implementation.

- [ ] **Step 3: Implement bounded D2D primitives and thumbnail download**

Add:

```python
async def _open_d2d(self, conn_info):
    kwargs = {}
    if conn_info.get("secured"):
        kwargs["ssl"] = self._ssl_context
    async with asyncio.timeout(ART_D2D_DEADLINE):
        return await asyncio.open_connection(
            conn_info["ip"], int(conn_info["port"]), **kwargs
        )

async def _read_d2d_file(self, reader):
    async with asyncio.timeout(ART_D2D_DEADLINE):
        header_size = int.from_bytes(await reader.readexactly(4), "big")
        header = json.loads(await reader.readexactly(header_size))
        body = await reader.readexactly(int(header["fileLength"]))
    return header, body
```

`get_thumbnail()` sends `get_thumbnail_list` with `content_id_list` and `conn_info` matching the pinned library, reads until `num + 1 == total`, returns the requested image bytes, and always closes/awaits the writer. Catch only a TV-declared DRM `ResponseError` and return `None`; transport errors propagate to `FrameDevice`.

- [ ] **Step 4: Write failing D2D and API 0.97 upload tests**

Assert the D2D header and body exactly:

```python
header = json.loads(bytes(writer.data[4:4 + header_len]))
assert header == {
    "num": 0, "total": 1, "fileLength": len(image),
    "fileName": "image", "fileType": "jpg",
    "secKey": "secret", "version": "0.0.1",
}
assert bytes(writer.data[4 + header_len:]) == image
```

Cover ready-to-use correlation, registering the `image_added` future before sending bytes, final content ID, no upload retry, cancellation cleanup, writer cleanup, and the API `0.97` websocket binary frame format (`uint16 header length + compact outer JSON + raw image`).

- [ ] **Step 5: Implement upload without releasing the operation lock between phases**

Structure upload as one serialized transaction:

```python
async def upload(self, data, file_type, matte):
    async with self._operation_lock:
        await self.start_listening()
        try:
            version = await self._request_unlocked("api_version")
        except ResponseError:
            version = None
        if isinstance(version, dict) and version.get("version") == "0.97":
            return await self._upload_ws_binary_unlocked(data, file_type, matte)
        return await self._upload_d2d_unlocked(data, file_type, matte)
```

For D2D, register the UUID-less `image_added` future before writing, request `send_image`/`ready_to_use`, perform bounded `write()` plus `drain()`, close and await the writer, then await the already-registered completion future under `ART_REQUEST_DEADLINE`. Any timeout closes the websocket and re-raises. For API 0.97 use `await connection.send(binary_payload)` and await the UUID-correlated `image_added` response.

- [ ] **Step 6: Run all transport tests and commit**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_frame_art.py -q
git diff --check
```

Expected: thumbnail/upload cleanup, framing, timeout, and cancellation tests all pass.

Commit:

```bash
git add custom_components/samsungtv_frame/frame_art.py tests/test_frame_art.py
git commit -m "feat: use async streams for frame art transfers"
```

---

### Task 4: Replace the Sync Art Facade

**Files:**
- Modify: `custom_components/samsungtv_frame/device.py`
- Modify: `tests/test_device.py`
- Delete: `custom_components/samsungtv_frame/art_listener.py`
- Delete: `tests/test_art_listener.py`

**Interfaces:**
- Consumes: all `FrameArt` methods from Tasks 1–3.
- Produces: the existing coordinator/entity-facing `FrameDevice` public API unchanged, plus `set_art_event_callback(callback)`; listener start/restart become zero-argument methods and `listener_alive` delegates to `FrameArt.is_alive()`.

- [ ] **Step 1: Rewrite device fixtures and add failing delegation/retry tests**

Construct `FrameDevice` with injected runtime dependencies:

```python
device = FrameDevice(
    hass,
    host="1.2.3.4",
    mac="A0:D0:5B:86:CE:B7",
    token="tok",
    ssl_context=MagicMock(),
    task_factory=lambda coro, name: asyncio.create_task(coro, name=name),
)
```

Patch `_art` with an async mock and replace the old sync/executor tests with:

```python
async def test_get_artmode_retries_stale_failure_once(device):
    device._art.get_artmode = AsyncMock(side_effect=[OSError("stale"), "on"])
    device._art.close = AsyncMock()
    assert await device.async_get_artmode() is True
    device._art.close.assert_awaited_once()

async def test_get_artmode_does_not_retry_timeout(device):
    device._art.get_artmode = AsyncMock(side_effect=TimeoutError)
    assert await device.async_get_artmode() is None
    assert device._art.get_artmode.await_count == 1

async def test_upload_is_never_retried(device):
    device._art.upload = AsyncMock(side_effect=OSError("partial"))
    with pytest.raises(OSError):
        await device.async_upload_art(b"image", "jpg", "none")
    device._art.upload.assert_awaited_once()
```

Also assert no art operation calls `hass.async_add_executor_job`, token capture reads only `_art` and `_remote`, restart closes/reopens the same adapter, stop is bounded/idempotent, and callbacks are loop-native.

- [ ] **Step 2: Run the device tests and verify they fail against the sync facade**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_device.py -q
```

Expected: constructor and async mock assertions fail because `FrameDevice` still builds `SamsungTVArt` and executor-wraps its methods.

- [ ] **Step 3: Delete sync imports, monkeypatches, locks, and listener objects**

Remove `SamsungTVArt`, all `_stv_*` monkeypatch code, `Callable[..., Any]` executor helpers, `_art_lock`, `_listener_lock`, `_art_listener`, `_new_listener()`, `_finish_listener_start()`, `_async_listener_job()`, and `_thumb_busy`.

Construct one `FrameArt`:

```python
self._art = FrameArt(
    host,
    token=token,
    ssl_context=ssl_context,
    task_factory=task_factory,
    event_callback=None,
)

def set_art_event_callback(self, callback: ArtEventCallback) -> None:
    self._art.set_event_callback(callback)

@property
def listener_alive(self) -> bool:
    return self._art.is_alive()
```

- [ ] **Step 4: Implement async retry policy and thin public delegation**

Centralize ordinary stale retry while preserving polling return conventions:

```python
async def _async_art_command(self, operation, *, retry=True):
    try:
        return await operation()
    except TimeoutError:
        await self._art.close()
        raise
    except Exception:
        await self._art.close()
        if not retry:
            raise
        try:
            return await operation()
        except Exception:
            await self._art.close()
            raise
```

`async_get_artmode(attempts)` loops at most `attempts`, returns `None` immediately on `TimeoutError`, and closes between ordinary failures. Other getters log and return `None`; setters raise after retry; upload passes `retry=False`; thumbnail logs transport errors and returns `None` while `FrameArt` handles DRM refusal.

Listener methods become:

```python
async def async_start_art_listener(self):
    await self._art.start_listening()

async def async_restart_art_listener(self):
    await self._art.close()
    await self._art.start_listening()

async def async_stop(self):
    self._stopped = True
    await asyncio.gather(self._remote.close(), self._art.close(), return_exceptions=True)
```

- [ ] **Step 5: Delete the thread bridge and move decoding ownership**

Delete `art_listener.py` and `test_art_listener.py`. Ensure the equivalent malformed JSON/non-dict/push decoding cases exist in `test_frame_art.py`, where `_dispatch_frame()` now performs this work directly on the event loop.

- [ ] **Step 6: Run facade and transport tests, scan for sync art usage, and commit**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_frame_art.py tests/test_device.py -q
rg -n "SamsungTVArt|_art_listener|_async_art_call|start_listening\(|art.*async_add_executor_job" custom_components tests
git diff --check
```

Expected: tests pass; search finds only native `FrameArt.start_listening()` references and no `SamsungTVArt`, executor-wrapped art call, or thread listener.

Commit:

```bash
git add custom_components/samsungtv_frame/device.py custom_components/samsungtv_frame/frame_art.py tests/test_device.py tests/test_frame_art.py
git rm custom_components/samsungtv_frame/art_listener.py tests/test_art_listener.py
git commit -m "refactor: route frame art through async transport"
```

---

### Task 5: Home Assistant Task Ownership and Async Pairing

**Files:**
- Modify: `custom_components/samsungtv_frame/__init__.py`
- Modify: `custom_components/samsungtv_frame/config_flow.py`
- Modify: `custom_components/samsungtv_frame/coordinator.py`
- Modify: `tests/test_init.py`
- Modify: `tests/test_config_flow.py`
- Modify: `tests/test_coordinator.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `FrameDevice(..., ssl_context, task_factory)` and `set_art_event_callback()` from Task 4.
- Produces: entry-owned receiver/restart/standby-refresh tasks and executor-free pairing that always closes.

- [ ] **Step 1: Add failing setup ownership tests**

In `test_init.py`, assert SSL context creation happens once through HA’s executor during setup, the device receives an entry task factory, callback wiring precedes first refresh, and unload closes the device. The task-factory assertion should execute the captured callable and verify it delegates to:

```python
entry.async_create_background_task(hass, coroutine, name)
```

Keep the TV-off test: no connection is attempted until a reachable poll requests art state.

- [ ] **Step 2: Add failing pairing cleanup tests**

Replace patches of `SamsungTVArt` with `FrameArt` and cover success, `open()` failure, and cancellation:

```python
async def test_pair_always_closes(hass):
    art = MagicMock(token="new-token")
    art.open = AsyncMock()
    art.close = AsyncMock()
    with patch("custom_components.samsungtv_frame.config_flow.FrameArt", return_value=art):
        result = await validate_and_pair(hass, "1.2.3.4")
    assert result[CONF_TOKEN] == "new-token"
    art.close.assert_awaited_once()
```

Assert pairing uses `hass.async_add_executor_job` exactly once for `get_ssl_context`,
never for an art operation, and the short-lived object receives `task_factory=None`,
`event_callback=None`, and the prebuilt SSL context.

- [ ] **Step 3: Add failing entry-scoped standby refresh test**

Update `test_art_event_go_to_standby_holds_but_refreshes` to assert `config_entry.async_create_background_task` receives `async_request_refresh()` with name `samsungtv_frame-standby-refresh`; add deduplication so repeated standby pushes do not stack refresh tasks.

- [ ] **Step 4: Run lifecycle tests and verify failures**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_init.py tests/test_config_flow.py tests/test_coordinator.py -q
```

Expected: failures show old thread bridge wiring, sync pairing, and untracked `hass.async_create_task`.

- [ ] **Step 5: Wire entry-owned tasks and prebuilt SSL context**

In `async_setup_entry()`:

```python
ssl_context = await hass.async_add_executor_job(get_ssl_context)

def task_factory(coroutine, name):
    return entry.async_create_background_task(hass, coroutine, name)

device = FrameDevice(
    hass, host=entry.data[CONF_HOST], mac=entry.data[CONF_MAC],
    token=entry.data.get(CONF_TOKEN), ssl_context=ssl_context,
    task_factory=task_factory,
)
coordinator = FrameCoordinator(hass, entry, device)
device.set_art_event_callback(coordinator.handle_art_event)
coordinator.restart_listener = device.async_restart_art_listener
await coordinator.async_config_entry_first_refresh()
```

Remove `make_art_bridge` and the separate post-refresh listener-start task. The first reachable art request starts the receiver; an unreachable first refresh performs no art connection.

- [ ] **Step 6: Convert config flow pairing to async `FrameArt`**

Create the SSL context through the executor because SSL context construction is the one permitted blocking setup operation, then pair and close directly:

```python
ssl_context = await hass.async_add_executor_job(get_ssl_context)
art = FrameArt(host, token=None, ssl_context=ssl_context,
               task_factory=None, event_callback=None,
               timeout=PAIRING_DEADLINE)
try:
    async with asyncio.timeout(PAIRING_DEADLINE):
        await art.open()
    token = art.token
except Exception as err:
    raise CannotConnect from err
finally:
    await art.close()
```

Do not call `start_listening()` during pairing. Preserve reconfigure behavior that does not overwrite a stored token with `None`.

- [ ] **Step 7: Track and deduplicate standby refresh**

Add `self._standby_refresh_task: asyncio.Task | None = None` and schedule through the entry:

```python
if sub == "go_to_standby":
    if self._standby_refresh_task is None or self._standby_refresh_task.done():
        self._standby_refresh_task = self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(),
            f"{DOMAIN}-standby-refresh",
        )
    return
```

Keep listener restart deduplication, but update comments and tests from “recv thread” to “receiver task.”

- [ ] **Step 8: Run lifecycle tests and commit**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest tests/test_init.py tests/test_config_flow.py tests/test_coordinator.py tests/test_device.py tests/test_frame_art.py -q
git diff --check
```

Expected: all selected tests pass and no task/unclosed coroutine warnings appear.

Commit:

```bash
git add custom_components/samsungtv_frame/__init__.py custom_components/samsungtv_frame/config_flow.py custom_components/samsungtv_frame/coordinator.py custom_components/samsungtv_frame/device.py tests/conftest.py tests/test_init.py tests/test_config_flow.py tests/test_coordinator.py tests/test_device.py
git commit -m "fix: make art lifecycle entry scoped"
```

---

### Task 6: Full Regression, Release Metadata, and Fable Review

**Files:**
- Modify: `custom_components/samsungtv_frame/manifest.json`
- Modify: `README.md`
- Modify: implementation/tests only for technically verified review findings.

**Interfaces:**
- Consumes: completed async transport and HA lifecycle.
- Produces: green full suite and release-ready `0.6.7` tree.

- [ ] **Step 1: Run the complete suite before metadata changes**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest -q
```

Expected: the complete existing and new suite passes with no resource, pending-task, or unclosed-socket warnings.

- [ ] **Step 2: Run static checks available in the repository**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/python -m compileall -q custom_components tests
git diff --check
rg -n "SamsungTVArt|run_in_executor|async_add_executor_job.*art|threading|call_soon_threadsafe" custom_components/samsungtv_frame tests
```

Expected: compile and diff checks succeed; search has no sync art/thread bridge implementation. `async_add_executor_job` remains only for SSL-context construction, Wake-on-LAN, and unrelated allowed operations.

- [ ] **Step 3: Ask Claude Fable for a focused implementation review**

Run from an interactive Bash PTY, with the diff and design in scope:

```bash
/bin/bash -ic 'claude-lesina --model fable -p "Review the native async Samsung Frame art implementation in this repository against docs/superpowers/specs/2026-07-12-native-async-art-design.md. Inspect the full changed files and tests. Focus on cancellation, receiver/close races, request correlation, D2D framing and cleanup, HA config-entry task ownership, retry semantics, and compatibility with samsungtvws 3.0.5. Report only concrete defects with file/line evidence; do not edit files."'
```

Expected: a finite review report and no surviving Claude process.

- [ ] **Step 4: Verify each Fable finding before changing code**

For every reported issue, reproduce it with one focused failing test in
`tests/test_frame_art.py`, `tests/test_device.py`, or the relevant HA lifecycle test
file. Reject findings contradicted by the pinned library source or existing behavior.
Run that exact node ID before the change, implement the smallest correction, then run
the same node ID again. Expected: the new test fails before and passes after the
correction. Record the exact command and result in the implementation log before
moving to the next finding.

- [ ] **Step 5: Bump version and document the reliability change**

Set manifest version:

```json
"version": "0.6.7"
```

Add a README reliability note stating that art commands, push events, thumbnails, uploads, pairing, and shutdown use cancellable async I/O; no configuration migration is required.

- [ ] **Step 6: Run final local verification and commit**

Run:

```bash
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest -q
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/python -m compileall -q custom_components tests
git diff --check
git status --short
```

Expected: all tests pass, compile/diff checks are clean, and status contains only intended release changes.

Commit:

```bash
git add custom_components tests README.md
git commit -m "release: v0.6.7 native async art transport"
```

---

### Task 7: Publish Through GitHub and Deploy Through HACS

**Files:**
- No source changes expected.
- Production mutation scope: HACS repository registration/install and supervised Home Assistant Core restart/reload.

**Interfaces:**
- Consumes: verified branch tip containing manifest version `0.6.7`.
- Produces: pushed branch/main release, tag `v0.6.7`, GitHub release, production HACS installation, and production acceptance evidence.

- [ ] **Step 1: Rebase or merge cleanly onto current origin main and reverify**

Fetch, inspect divergence, and integrate without destructive reset:

```bash
git fetch origin
git log --oneline --left-right origin/main...HEAD
git rebase origin/main
/home/nicola/Projects/ha-samsungtv-frame/.venv/bin/pytest -q
```

Expected: no unreviewed remote changes are lost and the full suite remains green.

- [ ] **Step 2: Push the implementation branch and merge intentionally**

Push `fix/native-async-art`, review the final GitHub diff/checks, then fast-forward or merge to `main` according to repository protection. Do not tag a commit that has not passed CI.

```bash
git push -u origin fix/native-async-art
```

Expected: GitHub CI and HACS validation pass for the exact release tree.

- [ ] **Step 3: Tag and publish `v0.6.7`**

After the verified release commit is on `main`:

```bash
git tag -a v0.6.7 -m "Samsung Frame TV v0.6.7"
git push origin v0.6.7
gh release create v0.6.7 --title "Samsung Frame TV v0.6.7" --notes "Replace synchronous art-protocol calls with cancellable async websocket and D2D I/O, preventing Home Assistant executor exhaustion and improving reconnect/unload reliability."
```

Expected: GitHub exposes a source archive for `v0.6.7` and HACS can resolve the release.

- [ ] **Step 4: Capture production baseline before mutation**

Through the HA production console, record Home Assistant Core health, Samsung integration log errors, current `SyncWorker` thread count, frontend root/static asset responses, HACS state, and Hindsight health. Expected: UI and Hindsight are healthy before deployment; any pre-existing warnings are recorded separately.

- [ ] **Step 5: Register the GitHub repository as a HACS custom integration and install `v0.6.7`**

Use HACS’s custom repository workflow for `bonzanni/ha-samsungtv-frame`, category `Integration`, then install/redownload `v0.6.7`. Confirm `/config/custom_components/samsungtv_frame/manifest.json` reports `0.6.7` and the installed files match the release. Do not copy or extract a tar archive manually.

- [ ] **Step 6: Restart Home Assistant Core through the supervised path**

Use the production console’s confirmed mutation mode and normal supervisor command. Wait for Core health and frontend root plus one hashed static asset to return HTTP 200 before exercising the TV.

- [ ] **Step 7: Execute the real-TV acceptance matrix**

Record evidence for:

```text
cold boot -> entities load and art query resolves
WATCHING -> ART_MODE -> WATCHING push transitions
OFF -> ON twice -> receiver reconnects twice
thumbnail: user image returns bytes; Samsung Store image returns placeholder/None
upload -> content id -> select -> thumbnail -> delete round-trip
integration reload/unload -> no pending task or unclosed socket warning
```

Expected: every operation completes within its configured deadline and no new authorization prompt appears from background work.

- [ ] **Step 8: Soak and verify the original production failure cannot recur**

During a multi-poll soak, repeatedly probe HA root/static assets and sample process threads/tasks. Expected: frontend responses stay healthy; no accumulation of `SyncWorker` threads in `samsungtvws.art`; receiver task count remains one per entry; Hindsight stays healthy; logs contain no pending-task, unclosed-socket, or repeated reconnect storm.

- [ ] **Step 9: Apply rollback only if a hard gate fails**

If a hard gate fails, use HACS to reinstall `v0.6.6`, restart Core, and confirm frontend/static assets recover. Do not restore the tar installation or reintroduce synchronous transport code. Preserve failure logs and add a focused regression test before correcting `v0.6.7`.
