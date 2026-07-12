# Native Async Art Transport — Design

**Date:** 2026-07-12  
**Status:** Approved for implementation  
**Release:** `v0.6.7`

## Goal

Replace every synchronous Samsung art-protocol path with cancellable async I/O so a
misbehaving TV cannot consume Home Assistant's executor threads, block shutdown, or
make the HA frontend unavailable. Preserve the existing entities, services, state
derivation, token behavior, and power-cycle recovery.

Production established the failure mechanism: 62 Home Assistant `SyncWorker` threads
were stuck in `SamsungTVArt.get_artmode()` while HA's static frontend requests waited
behind the exhausted shared executor. `asyncio.timeout()` cancelled only the awaiting
coroutine; the blocking library threads survived.

## Decision

Implement a small integration-owned `FrameArt` async adapter over the pinned
`SamsungTVWSAsyncConnection` from `samsungtvws==3.0.5`. Reuse only its stable URL,
token, and send helpers. Override connection establishment, receive-task ownership,
close, and liveness because the base lifecycle leaks failed-handshake sockets, rejects
starting a receiver on an open connection, retains completed receiver-task references,
and can report alive after the receiver has failed.

The NickWaterton async-art fork is a protocol reference, not a source dependency or a
vendored module. Its implementation depends on divergent fork internals and cannot be
dropped onto the published 3.0.5 base without importing most of the fork.

Rejected alternatives:

- Keeping sync art calls in HA's shared executor cannot provide cancellation or
  shutdown guarantees.
- A private executor contains HA-wide starvation but still relies on out-of-band
  socket closure to reclaim non-daemon threads and adds an architecture intended to
  be removed later.
- Vendoring the whole async fork creates split ownership of multiple connection,
  event, helper, and exception modules alongside the pinned PyPI package.

## Components

### `frame_art.py`

`FrameArt` subclasses `SamsungTVWSAsyncConnection` for stable construction and send
helpers, while owning the receive lifecycle completely.

- `open()` creates the websocket, tolerates client connect/disconnect broadcasts,
  validates connect and art-channel-ready events, captures issued tokens, and closes
  the local socket on every unsuccessful handshake.
- `start_listening(task_factory)` is idempotent, lazy-opens once, and creates exactly
  one receiver through the injected task factory. Runtime setup injects
  `ConfigEntry.async_create_background_task`, so Home Assistant owns and cancels the
  task with the entry; config-flow pairing never starts a receiver.
- The receiver continuously processes push events and correlated responses.
- Its `finally` block fails every pending request future, clears the request map,
  clears the receiver-task reference, and marks the connection unavailable regardless
  of how the receiver exited.
- `close()` marks intentional shutdown, cancels and awaits the receiver task, closes
  the websocket, fails pending futures, and leaves a fully reset state.
- `is_alive()` requires both an open websocket and a live receiver task.

`FrameArt` serializes operations with one `asyncio.Lock`. Each request creates a local
UUID and future before sending. Responses resolve by `request_id` or `id`; UUID-less
firmware replies use their expected sub-event while the operation lock prevents
collisions. Push events are delivered independently on the same persistent connection.
All waits have absolute `asyncio.timeout()` deadlines.

### Async D2D transfers

Thumbnail downloads and uploads use `asyncio.open_connection()` and
`StreamReader.readexactly()`/`StreamWriter` rather than executor jobs. Every connect,
read, drain, and response wait is deadline-bounded. Writers close in `finally` and
`wait_closed()` is awaited. `IncompleteReadError`, connection loss, cancellation, and
timeouts close the transfer and surface as normal integration errors.

The SSL context needed by secured D2D transfers is created once with
`hass.async_add_executor_job()` during runtime setup and then injected into `FrameArt`.
Config-flow pairing does not perform D2D work and does not create the context. Upload
and thumbnail protocol framing follows the working 2022 Frame behavior and the
external async-art reference, with explicit tests.

### `FrameDevice`

`FrameDevice` owns one `FrameArt` instance and delegates every art operation directly
to it. The following sync infrastructure is deleted:

- `SamsungTVArt` clients and imports;
- global handshake monkeypatches;
- executor-wrapped art calls and connection-reset helpers;
- `_art_lock`, listener threads, private `_recv_loop` probing, and forced thread joins;
- sync thumbnail and pairing operations.

The existing async remote, REST, UPnP, Wake-on-LAN, state facade, token adoption, and
remote authorization behavior remain. Deadline timeouts close the async art connection
and fail without retry; ordinary stale-connection failures may reconnect once.

### Coordinator and setup

The coordinator's state derivation, OFF debounce, model-trait learning, polling
deadline, app detection, and wake probing remain transport-independent.

- Cold start and post-reconnect query art mode once; steady-state art changes are
  push-driven on the persistent connection.
- Existing reachable-edge and listener-liveness recovery triggers reopen `FrameArt`.
- Runtime setup gives `FrameDevice` an entry-scoped task factory backed by
  `ConfigEntry.async_create_background_task`; `FrameArt` uses it for its sole receiver
  task, which Home Assistant cancels on unload.
- `go_to_standby` refreshes use an entry-scoped background task, eliminating the
  existing late-task leak.
- Config-flow pairing uses `FrameArt.open()` under an async deadline and always closes
  its connection.

## Error handling

- A request timeout cancels a real async wait, closes the connection, fails all pending
  futures, and leaves no thread or task behind.
- Receiver failure degrades push updates and triggers the existing polling/reconnect
  recovery without blocking the coordinator.
- Unload is idempotent and bounded even if setup, a request, or a D2D transfer is in
  flight.
- User-facing service failures continue to raise `HomeAssistantError` or
  `ServiceValidationError`; transient polling failures continue through
  `UpdateFailed`.
- No background operation may trigger a TV authorization prompt.

## Testing

Tests use a fake async websocket and fake D2D streams; no timing-dependent real threads
are permitted.

Required automated coverage:

1. Successful, unauthorized, unexpected-event, timeout, and broadcast-heavy handshake.
2. One receiver task only; idempotent start; truthful liveness after every exit mode.
3. UUID response correlation, UUID-less sub-event correlation, push/request coexistence,
   and pending-future failure on disconnect.
4. Cancellation and close during an in-flight request with no pending task afterward.
5. Reconnect after stale connection and no retry after a deadline timeout.
6. Thumbnail success, DRM refusal, timeout, truncated frame, and writer cleanup.
7. Upload framing, response correlation, timeout, cancellation, and writer cleanup.
8. Config-flow pairing closes on success and every failure.
9. Entry unload while receiver, request, and transfer are active.
10. Existing coordinator, entity, config-flow, and service regression suite.

Claude Fable reviews the implementation diff after the first complete green test run.
Only technically verified findings are applied, followed by another full test run.

## Release and production rollout

1. Bump the integration version to `0.6.7` and document the executor-exhaustion fix.
2. Commit, push, tag `v0.6.7`, and publish a GitHub release suitable for HACS.
3. Add `bonzanni/ha-samsungtv-frame` to HACS as a custom integration repository and
   install `v0.6.7`; do not use the previous tar-package installation path.
4. Restart/reload Home Assistant Core through the normal supervised path.
5. Verify setup, entities, services, logs, thread count, frontend root, frontend static
   assets, and Hindsight health.
6. Exercise the real 2022 Frame through two OFF→ON power cycles, WATCHING↔ART changes,
   thumbnail retrieval, and upload round-trip.
7. Run a production soak while probing HA static assets and confirming flat thread count
   and no pending-task/unclosed-socket warnings.

Hard release gates are cold-boot correctness, bounded silence/chatty-TV recovery,
push/request coexistence, two reconnect cycles, thumbnail success/refusal handling,
clean unload, flat thread count, and healthy HA static assets. Upload is fixed and tested
in this release; if live upload alone exposes an undocumented firmware variant, release
is paused for correction rather than falling back to the synchronous transport.

## Rollback

If a hard production gate fails, reinstall the previous HACS version and restart Core.
The config-entry schema and stored data are unchanged, so rollback requires no migration.
The failed release is corrected and revalidated before another production attempt; the
synchronous art transport is not reintroduced into `v0.6.7`.
