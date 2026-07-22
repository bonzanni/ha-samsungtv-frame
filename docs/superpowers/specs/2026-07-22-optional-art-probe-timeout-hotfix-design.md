# Optional Art Probe Timeout Hotfix Design

**Date:** 2026-07-22

**Status:** Approved (user and Fable)

**Target release:** v0.7.1

## Objective

Restore Local Art Settings Phase 1 on Samsung firmware that silently ignores
unsupported optional Art commands. Optional capability discovery must fall
back without retiring a healthy Art websocket, while genuine transport loss
must still close the session and enter the existing supervised backoff path.

The hotfix must preserve mutation safety, authoritative Art-mode reads,
generation freshness, and the single-owner websocket lifecycle.

## Production Evidence

The HACS-installed v0.7.0 release was tested on a Samsung
`QE65LS03BAUXXH` (2022 Frame) while it was showing Art. Sanitized isolated
probes established this command matrix:

| Command | Result |
| --- | --- |
| `get_artmode_status` | Correlated success: `on` |
| `get_current_artwork` | Correlated success |
| `get_artmode_settings` | Correlated aggregate success |
| `get_brightness` | Silent timeout |
| `get_color_temperature` | Silent timeout |
| `get_auto_rotation_status` | Silent timeout |
| `get_slideshow_status` | Correlated legacy success: `off` |
| `set_auto_rotation_status` with the existing `off` state | Silent timeout |
| `set_slideshow_status` restoring `off` | Correlated legacy success |

The production failure is deterministic:

1. reconciliation successfully reads Art mode, current artwork, and aggregate
   settings;
2. the modern slideshow getter receives no response;
3. the normal 20-second request timeout closes the websocket;
4. `FrameDevice` treats the timeout as transport loss instead of dialect
   evidence;
5. the coordinator rejects all optional data because that Art generation is
   no longer READY;
6. every new generation repeats the same failure and backoff cycle.

This evidence also rejects model-year dialect tables. One firmware generation
supports a modern aggregate-settings command and a legacy slideshow command;
dialect support is per command and firmware, not per model year.

## Chosen Design

Add an explicit, opt-in capability-probe policy at the Art transport boundary.
Only read-only optional discovery commands may use it.

### Probe transport contract

Add `ART_PROBE_DEADLINE = 5` and a dedicated `ArtProbeTimeout` exception that
inherits directly from `Exception`, not `TimeoutError`, `ResponseError`, or a
transport exception. This prevents existing broad timeout and protocol-error
handlers from silently applying the wrong policy.
`FrameArt.request()` and `_request_unlocked()` gain a keyword-only probe flag
whose default is false.

For a normal request:

- retain the existing 20-second deadline;
- close the websocket on timeout;
- preserve all current mutation and core-read semantics.

For a probe request:

- use the five-second deadline;
- remove and cancel only that probe's request waiter through the existing
  `finally` cleanup;
- raise `ArtProbeTimeout` without closing the websocket;
- never retry or report success at the transport layer.

The probe flag is permitted only for:

- aggregate Art settings;
- legacy brightness and color-temperature discovery;
- modern slideshow discovery;
- legacy slideshow discovery.

It is forbidden for mutations, uploads, `get_artmode_status`,
`get_current_artwork`, and D2D transfers. Those operations remain
authoritative or state-changing, so silence is indeterminate and must retire
the generation.

### Late-response safety

Every request retains a unique UUID. When a correlated timed-out probe
response arrives late, its waiter has already been removed, so it cannot
satisfy a newer request. Probe response event names are not interpreted as Art
push events by the coordinator and are ignored.

Tests must pin both properties for correlated frames: a late correlated probe
frame cannot resolve a later waiter and cannot mutate coordinator state. An
id-less late error remains subject to the existing `_uuidless_pending`
compatibility path and cannot be universally attributed after its waiter is
retired; the hotfix does not claim to solve that broader protocol ambiguity.
No general-purpose timeout-preservation API may be exposed to authoritative
reads or mutations.

## Dialect Discovery and Liveness Proof

`FrameDevice._async_art_read_response()` propagates both `ResponseError` and
`ArtProbeTimeout` to the dialect-discovery method. Other exceptions continue
to retire the Art session through `async_connection_failed()`.

A silent timeout is ambiguous: the command may be unsupported, or the socket
may be half-open. Therefore dialect conclusions require at least one
correlated response, whether a value or `ResponseError`, during the same
discovery pass and current generation. Once liveness is proven, silent
alternatives in that pass may be classified as unsupported. With no correlated
response, the transport is retired and no dialect conclusion is cached.

### Slideshow

For an unknown dialect:

1. probe `get_auto_rotation_status`;
2. on success, cache `AUTO_ROTATION` for the current generation;
3. on correlated `ResponseError` or `ArtProbeTimeout`, probe
   `get_slideshow_status`;
4. on legacy success, cache `LEGACY` and return the parsed state;
5. on legacy correlated `ResponseError`, cache `UNSUPPORTED` and return no
   slideshow state;
6. if modern returned a correlated `ResponseError` and legacy is silent, cache
   `UNSUPPORTED` because the same-pass modern response proved liveness;
7. if both modern and legacy probes are silent, cache nothing, call
   `async_connection_failed()` exactly once, and return unknown state.

A successful or explicitly rejected response from either dialect is the
liveness proof for that discovery pass. A generation change at any point
aborts discovery without caching a result for the stale generation.

### Art settings

Probe aggregate settings first. On a valid aggregate response, cache
`AGGREGATE` and preserve the current complete-list capability semantics.

After aggregate `ResponseError` or `ArtProbeTimeout`, probe the legacy
brightness and color-temperature getters. Each correlated value marks that
setting supported; each correlated `ResponseError` marks that legacy getter
unsupported. Collect both fallback outcomes before judging silence. A silent
legacy getter may be treated as unsupported when the aggregate request or
either fallback getter produced a correlated value or error in the same
current generation.

If the aggregate and every legacy fallback getter are silent, there is no
liveness proof: cache nothing, call `async_connection_failed()` exactly once,
and return unknown. Otherwise cache `LEGACY` only after all fallback outcomes
have been collected, and return the supported subset. A correlated aggregate
`ResponseError` followed by two silent legacy getters therefore yields a live
`LEGACY` dialect with an empty supported set instead of reconnect churn.

All dialect caches remain scoped to the Art-session generation. Mutations do
not promote or demote capabilities.

### Known-dialect failures

Cached-dialect reads use the same five-second probe transport, but their
timeout handling is explicit in `FrameDevice`. Cached modern slideshow and
aggregate-settings timeouts enter their existing legacy fallback discovery;
a successful correlated fallback may replace the cached dialect. Cached
legacy slideshow has no further fallback. Cached legacy settings collect both
getter outcomes. In every case, a pass with no correlated response calls
`async_connection_failed()` exactly once, returns unknown, and does not escape
into the coordinator. The implementation must never cache `UNSUPPORTED`
solely from silence when no response in the pass proved liveness.

If a fallback returns the existing `_ART_READ_FAILED` sentinel, a generic
transport failure has already called `async_connection_failed()`. Discovery
returns unknown immediately and must not retire the session a second time.

### Slideshow mutation routing

The live TV also silently ignores the modern slideshow setter and acknowledges
the legacy setter. `FrameDevice.async_set_slideshow()` therefore reads the
generation-scoped slideshow dialect learned by the status getter:

- cached `LEGACY`: send only `set_slideshow_status`;
- cached `AUTO_ROTATION`: send only `set_auto_rotation_status`;
- unknown or unsupported: preserve the existing modern-first,
  correlated-`ResponseError` fallback behavior.

This routing changes only command order. The mutation never promotes,
demotes, or otherwise writes the dialect cache. Both exact setters retain the
normal 20-second timeout and close-on-silence behavior because a timed-out
mutation has indeterminate TV state. Before routing, the device applies the
existing generation reset; a write arriving after reconnection but before
new-generation discovery must treat the previous generation's dialect as
unknown.

## Coordinator Semantics

No coordinator state-model change is part of this hotfix.

The existing reconciliation already publishes aggregate settings when
slideshow discovery returns `None` while the session remains READY. Once
unsupported-command timeouts become non-destructive and the legacy fallback
succeeds, settings and slideshow commit normally under the existing shared
generation fence.

Assigning settings before slideshow would not improve externally visible
state: if slideshow suffers genuine transport loss, `art_ready` becomes false;
after recovery, the generation increments. Both entity availability and the
publish gate reject the earlier settings in either state. Making independent
publication real would require split generation stamps or weaker freshness,
which is a separate redesign and is intentionally excluded.

## Safety and Timing

- Five seconds is the probe deadline. Healthy LAN responses observed live are
  sub-second; five seconds leaves margin for TV load without imposing the
  20-second mutation deadline on capability discovery.
- Worst-case live optional discovery consumes 15 seconds for settings
  (aggregate plus two legacy probes) and 10 seconds for slideshow. A settings
  pass with a late correlated fallback can continue into slideshow, so the
  conservative combined optional budget remains 25 seconds inside the
  existing 45-second coordinator poll deadline. An all-silent settings pass
  retires earlier and does not run slideshow.
- Probe calls retain the existing serialized operation lock. A user command
  arriving during first-generation capability discovery may wait for the
  bounded probe sequence, up to the conservative optional budget, before it
  is admitted.
- If all alternatives are silent, the device explicitly closes the session
  and uses the existing backoff state machine. Half-open sockets are not
  retained indefinitely.
- Normal request timeout, close, generation invalidation, and non-retry
  behavior are unchanged.
- No model table, user option, additional websocket, or new background retry
  loop is introduced.

## Alternatives Rejected

### Model-year routing

Rejected because the live TV mixes modern settings with legacy slideshow.
Firmware updates would also make a static table stale. At most, model data
could someday influence probe order; it must never replace live capability
evidence.

### Publish settings before slideshow

Rejected as behaviorally inert under the current READY and generation gates.
It does not stop websocket churn and cannot expose settings from a dead or
superseded generation.

### Preserve every timeout

Rejected because mutations and authoritative reads have indeterminate state
after silence. Keeping those sockets alive would weaken recovery and could
claim success without acknowledgement.

## Verification Requirements

Transport tests must prove:

1. a probe timeout uses five seconds, raises `ArtProbeTimeout`, leaves the
   socket alive, and removes and cancels only its own pending waiter;
2. a late correlated timed-out response cannot resolve a newer request or
   mutate state;
3. a normal timeout still closes the transport;
4. only the allowlisted optional getters opt into probe behavior.

Device tests must prove:

5. modern slideshow timeout plus legacy success selects and caches `LEGACY`;
6. modern timeout plus legacy `ResponseError` selects `UNSUPPORTED` without
   closing the socket;
7. both slideshow probes timing out retires the session exactly once and
   caches nothing;
8. aggregate timeout plus at least one live legacy response returns and caches
   the supported subset;
9. every legacy settings probe timing out retires the session exactly once and
   caches nothing;
10. aggregate correlated `ResponseError` plus two silent legacy getters caches
    a live empty `LEGACY` settings dialect without retiring the session;
11. a cached `LEGACY` slideshow timeout and a cached `AGGREGATE` settings
    timeout each retire the session exactly once and do not escape to the
    coordinator;
12. a timeout followed by `_ART_READ_FAILED` does not retire the session a
    second time;
13. a generation change during fallback caches and publishes nothing;
14. a cached `LEGACY` slideshow mutation sends only the legacy setter and does
    not modify the dialect cache;
15. after a generation bump, a slideshow mutation does not reuse the previous
    generation's cached dialect;
16. the live production matrix completes one reconciliation with unchanged
    generation, available settings, legacy slideshow `off`, and no transport
    close.

The complete test suite, Ruff, compile checks, hassfest, and HACS validation
must pass before release.

## Release and Production Acceptance

Release v0.7.1 through the existing HACS custom repository; do not copy a tar
archive or branch tree into production.

1. bump the manifest and changelog;
2. commit, push, tag, and publish v0.7.1 after all reviews pass;
3. refresh only this HACS repository and install the exact tag;
4. verify the production tree hashes exactly match the release;
5. validate HA configuration and restart Core;
6. confirm the Art session remains READY and diagnostics advertise the live
   supported settings and legacy slideshow;
7. round-trip sleep-after, motion sensitivity, brightness sensor, brightness,
   color temperature, and legacy-routed slideshow where supported;
8. restore every original TV value and verify the restored readback;
9. confirm no new Samsung errors, transport churn, or UI responsiveness issue.

If v0.7.1 cannot pass live acceptance, restore v0.6.9 through HACS rather than
leaving the production TV on the known-churning v0.7.0 path.

## Review Record

Fable reviewed the sanitized live evidence and v0.7.0 implementation. It
endorsed the opt-in five-second probe, liveness-proof dialect caching,
both-silent transport recovery, and the minimum safety tests. A follow-up
review agreed that the proposed coordinator assignment reorder was a no-op
under the existing READY/generation freshness rules, so that change was
removed from the hotfix. Its written-spec review then identified cached-dialect
timeouts, mixed failure ownership, correlated liveness evidence, and
slideshow-mutation routing as missing contracts; the revision above resolves
those findings and incorporates the live setter evidence. Fable's final
re-review approved the revised spec, confirmed the conservative 25-second
optional-read budget, and suggested the generation-bump setter regression
test now included above.
