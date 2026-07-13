# Supervised Art Session Recovery — Design

**Date:** 2026-07-13
**Status:** Approved for implementation
**Target release:** `v0.6.8`

## Goal

Prevent a missing or wedged Samsung Art host from driving repeated websocket
handshakes that can keep the TV's Art service unavailable. Preserve the native
async transport introduced in `v0.6.7`, the existing entities and services, and
the live-proven 2022 Frame wire protocol.

## Production evidence

The affected TV visibly displayed Art while the Art websocket admitted the Home
Assistant client but did not expose its internal Art host:

- `ms.channel.connect` contained only the client with `isHost: false`;
- no internal `Smart Device` with `isHost: true` appeared;
- `ms.channel.ready` and D2D replies never arrived;
- unloading Home Assistant and leaving/re-entering Art did not recover it.

After the integration was unloaded and the TV was disconnected from power for
more than 30 seconds, the same tokenless port-8001 probe immediately received two
internal `isHost: true` clients, `ms.channel.ready`, and
`get_artmode_status = on`. An isolated probe using the integration's current
tokened TLS port-8002 path then produced the same successful exchange. This rules
out the native websocket implementation, saved token, client name, port, and Art
request framing as the primary incompatibility.

The current unhealthy path has two reconnection sources on each coordinator
heartbeat: `async_get_artmode(attempts=2)` can close and reopen the Art transport,
while listener-liveness recovery independently closes and restarts it. The native
async rewrite removed the old shared lock that accidentally serialized those
paths, but did not implement the approved push/cache polling design or add global
backoff. A hostless TV can therefore receive repeated connect/no-ready/close
cycles indefinitely.

## Decision

Implement the final architecture directly in one release. Do not ship an
intermediate polling-driven recovery design.

`FrameArt` remains the low-level websocket/D2D protocol adapter. A new
`ArtSession` is the sole high-level owner of Art websocket lifecycle and recovery.
`FrameDevice` delegates Art operations through that session. The coordinator
continues to poll REST power/availability, but it never directly opens, closes, or
restarts the Art websocket.

The existing `v0.6.7` wire behavior remains unchanged when the TV is healthy. The
new behavior is exercised only when the session is missing, dead, hostless, or
backed off.

Rejected alternatives:

- Restoring the synchronous `v0.6.6` client would reintroduce executor exhaustion,
  leaked blocking workers, slow unload, and Home Assistant UI starvation.
- Adding only a lock and backoff while retaining poll-driven opens would leave
  connection ownership split between the coordinator and listener recovery and
  require a second migration to reach the already-approved push/cache design.
- Changing websocket headers, token propagation, D2D framing, or upload
  correlation is unsupported by the live A/B evidence and broadens risk without
  addressing the observed failure.

## Components

### `frame_art.py`

`FrameArt` keeps responsibility for protocol framing, bounded handshake, one
receiver, response correlation, and D2D transfers.

Add `ArtHostUnavailable(ConnectionFailure)`. During the handshake:

- when `ms.channel.connect.data.clients` is an explicit non-empty list and no
  client has `isHost is True`, close the candidate socket and raise
  `ArtHostUnavailable` immediately;
- when `clients` is absent, empty, or malformed, retain the existing bounded wait
  for `ms.channel.ready` for compatibility with firmware that omits metadata;
- any explicit host followed by no `ready` remains a normal bounded timeout.

Once the session layer is integrated, ordinary Art requests require an already
live receiver. They must not call `start_listening()` themselves. Pairing may
still call `FrameArt.open()` directly because it owns a short-lived isolated
connection and always closes it in `finally`.

### `art_session.py`

`ArtSession` is the only runtime component allowed to initiate
`FrameArt.start_listening()` or recovery `FrameArt.close()` calls. It owns one
single-flight connection task and exposes readiness without performing hidden I/O.

States:

- `STOPPED`: not started or permanently unloaded; no work is accepted.
- `CONNECTING`: exactly one handshake task is active.
- `READY`: the websocket and receiver are live; requests may reuse them.
- `BACKOFF`: background opens are suppressed until `next_retry_at`.
- `DORMANT`: repeated explicit host absence suppresses background opens for the
  long cooldown; one half-open probe is allowed when it expires.

Transitions and delays:

- `async_start()` arms the session without opening before REST confirms the TV is
  available.
- A reachable observation reporting `on` or `standby` may request one background
  connection through the session. Supporting cold `standby` is required for 2025
  Frames that use it during normal Art mode; the same global retry budget keeps
  the probe safe. Once live evidence teaches the coordinator that Art implies
  REST `on` for a 2022-24 model, its shutdown `standby` is suppressed as a recovery
  input. A reachable edge resets failure counters and permits one immediate probe.
- A successful handshake through `ms.channel.ready` enters `READY`, increments a
  connection generation, and resets all failure state.
- Generic failures use `30, 60, 120, 300` second delays, capped at 300 seconds.
- Explicit `ArtHostUnavailable` failures use `60, 120, 300` seconds. The third
  consecutive hostless failure enters `DORMANT` for 900 seconds.
- Delays receive uniform jitter of plus or minus 20 percent. Clock and jitter
  functions are injectable so tests use deterministic values.
- A user-initiated Art operation may bypass `BACKOFF` or `DORMANT` once. It shares
  an in-flight connect if one exists, performs no automatic operation retry, and
  returns the real failure if the probe or command fails.
- Entry unload permanently enters `STOPPED`, closes admission synchronously, drains
  the entry-owned connect task within a deadline, calls `FrameArt.stop()`, and joins
  one per-generation close owner whose outer deadline includes lifecycle-lock
  acquisition. Caller or owner cancellation cannot abandon cleanup.

The session emits state-change callbacks. It does not own HA entities or derive TV
mode.

### `FrameDevice`

`FrameDevice` constructs one `FrameArt` and one `ArtSession`.

- Background getters return `None` without opening when the session is not
  `READY`.
- User Art mutations ask the session for one user-triggered readiness probe, then
  execute exactly once over the ready socket.
- A failed ready-socket operation is reported to the session so it closes the
  stale transport and enters paced recovery.
- Upload remains non-retryable because a partial upload may already have mutated
  the TV.
- Remote, REST, UPnP, Wake-on-LAN, config-entry data, and token behavior remain
  unchanged.

### Coordinator and setup

The coordinator remains the owner of cached semantic Art state:
`_art_mode`, `_current_art`, `_art_brightness`, and `_art_color_temp`.

- Every REST heartbeat reports reachability and an Art-eligible power state to
  `ArtSession`; this notification may schedule an entry-owned recovery task but the
  poll never directly awaits or performs an Art open. Cold `standby` remains
  eligible until live evidence identifies it as shutdown on the configured model.
- A new `READY` connection generation triggers one immediate coordinator refresh.
  That refresh seeds Art mode and extras through requests that are guaranteed not
  to open a connection.
- Push events continue to update the cache and publish `FrameData` immediately.
  Per-field revisions prevent an older in-flight reconciliation response from
  overwriting a newer push.
- Reconciliation occurs at most once every 300 seconds and only while the session
  is `READY`. It reuses the current socket and never opens one.
- The generation and next reconciliation window are reserved before I/O so a
  cancelled poll cannot repeat a full reconciliation on the next heartbeat.
- Between reconciliation windows the heartbeat derives state from the cache. If
  the session remains unavailable, an effective `None` overlay at the exact
  failure threshold transitions to `UNKNOWN` without destroying the last pushed
  cache or increasing network traffic. A successful read/live push or new power
  episode resets the failure streak.
- Reachable-edge and listener-liveness signals become inputs to `ArtSession`; the
  coordinator's independent listener-restart task and callback are removed.
- Setup wires the Art event callback and session-state callback before the first
  refresh, arms the session, and retains entry-owned task creation. Setup failure
  performs bounded device cleanup; unload removes the state callback before
  terminal shutdown so late READY cannot schedule a refresh across teardown.

## Invariants

1. At most one runtime Art websocket and one handshake may exist per config entry.
2. Only `ArtSession` initiates runtime open, close, or recovery.
3. Coordinator polls never directly open or await an Art websocket; only their
   synchronous session observation may cause the entry-owned supervisor to schedule
   a budgeted recovery task.
4. A `READY` session performs no reconnect work.
5. Background recovery obeys one global retry deadline independent of heartbeat
   frequency.
6. An explicit user Art operation is attempted at most once after at most one
   readiness probe.
7. Every wait and close remains deadline-bounded and entry unload is terminal.
8. Missing client-role metadata never becomes a false hostless failure.

## Testing

Tests use fake async websockets and deterministic clocks/jitter. Required red-first
coverage:

1. Explicit non-empty client list without `isHost: true` fails immediately with
   `ArtHostUnavailable`, closes the candidate socket, and performs no second recv.
2. Explicit host followed by silence remains a bounded timeout; absent/empty client
   metadata remains compatible with a later `ready`.
3. Multiple callers share one `CONNECTING` task and create one physical websocket.
4. Generic and hostless failures follow their exact retry schedules; heartbeat
   observations inside a delay create no socket.
5. Three hostless failures enter `DORMANT`; only one half-open probe is allowed
   after 900 seconds.
6. Reachable edge and user action each permit one immediate probe; a user operation
   is never automatically retried.
7. Dead receiver recovery and coordinator polling cannot produce parallel or
   sequential duplicate opens in one heartbeat.
8. Cold start/reconnect seeds once; healthy heartbeats use cache; reconciliation is
   READY-only and spaced by 300 seconds.
9. Push events update state throughout the reconciliation interval and win races
   with older reconciliation responses.
10. Cold 2025-style Art/`standby` startup probes once through the supervisor, while
    learned 2022-24 shutdown `standby` neither reconnects nor reconciles.
11. Unload during `CONNECTING`, `BACKOFF`, and `READY` leaves no task, socket, or
    pending future.

The complete existing test suite must remain green.

## Release and production validation

Release as `0.6.8` through the existing HACS custom repository; do not reinstall a
tar archive.

1. Run focused transport, session, device, coordinator, setup, and entity tests.
2. Run the full pytest suite and whitespace/static checks.
3. Obtain task-level reviews, a whole-branch review, and a final Opus review through
   `claude-lesina` using public code and sanitized evidence only.
4. Commit, push, tag `v0.6.8`, and publish the GitHub release.
5. Update the installed HACS custom integration and reload/restart Home Assistant
   through the supervised production path.
6. Confirm frontend responsiveness and flat HA executor usage.
7. On the real TV verify cold Art startup, WATCHING-to-ART and ART-to-WATCHING,
   current art/settings, one power cycle, clean unload/reload, and no reconnect storm.
8. During the soak, confirm one stable Art socket while healthy and retry spacing
   when intentionally unavailable.

## Rollback

If any release gate fails, reinstall `v0.6.7` through HACS and restart Home
Assistant Core. Config-entry schema and stored token data do not change. A rollback
may require one physical TV power cycle if the Art service was already wedged; the
synchronous transport is not restored.
