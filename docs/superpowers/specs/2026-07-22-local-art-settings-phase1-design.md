# Local Art Settings Phase 1 Design

**Date:** 2026-07-22

**Status:** Approved

## Objective

Add the first independently releasable group of local-only Samsung Frame
features without SmartThings:

- privacy-safe Home Assistant diagnostics;
- slideshow state readback;
- Sleep After configuration;
- motion-sensitivity configuration;
- brightness-sensor configuration.

The change must preserve the existing supervised Art websocket, must not add
per-entity polling, and must not make optional Art features capable of
poisoning the integration's core OFF/WATCHING/ART state.

This increment also fixes an existing state-coherency defect: current Art
brightness and color-temperature writes request a coordinator refresh, but a
normal refresh does not re-read Art values until the five-minute Art
reconciliation deadline. The slideshow service does not request any refresh.

## Scope

### Included

- One aggregate read of all advertised Art settings.
- Legacy fallback reads for brightness and color temperature on older
  firmware that explicitly rejects the aggregate settings request.
- Slideshow status readback with the modern/legacy getter dialects.
- First-class Home Assistant entities for supported settings.
- Generation-scoped capability discovery.
- An explicit post-mutation Art reconciliation path.
- Strictly allowlisted, zero-I/O diagnostics.
- Tests and user documentation for every new surface.

### Excluded

- SmartThings or any other cloud dependency.
- Night Mode, motion-detected events, or raw ambient-light readings.
- Multiple writable slideshow entities.
- Artwork metadata, collection browsing, thumbnails, dynamic mattes, and
  dynamic filters. Those belong to the next Art-gallery increment.
- App/input/channel/browser/text/discovery work. Those belong to the separate
  TV-control track.
- Deployment, reload, or acceptance testing on the production N150.

## Architecture Decision

Extend the existing `FrameCoordinator` and its single supervised Art session.
Do not create a second coordinator and do not let entities access the
websocket directly.

The coordinator performs one serialized reconciliation over a captured READY
Art-session generation:

1. read Art mode;
2. read the current artwork;
3. read the complete Art-settings list once;
4. read slideshow state once;
5. publish the results only while the same generation is still READY.

This reconciliation runs when a new generation becomes READY, every existing
`ART_RECONCILE_SECONDS` interval, and immediately after a successful local
mutation. The ordinary heartbeat continues to use cached Art details, so the
increment does not add websocket traffic to every REST poll.

Alternatives rejected:

- A separate Art-details coordinator duplicates READY/backoff/generation and
  unload lifecycle, while still needing to serialize through the same socket.
- Entity-local reads and optimistic state are smaller initially but duplicate
  the same aggregate request, produce inconsistent snapshots, and retain the
  existing post-write staleness defect.

## State Model

Add the following immutable models to `models.py`.

### Art settings

`ArtSettingKey` is a `StrEnum` containing exactly:

- `brightness`
- `color_temperature`
- `motion_timer`
- `motion_sensitivity`
- `brightness_sensor_setting`

`ArtSettingsSnapshot` is a frozen dataclass containing:

- `supported: frozenset[ArtSettingKey]`
- `brightness: int | None`
- `color_temperature: int | None`
- `motion_timer: str | None`
- `motion_sensitivity: str | None`
- `brightness_sensor_enabled: bool | None`

`motion_sensitivity` stores the normalized protocol value, not a guessed
human meaning. If live evidence establishes the exact mapping, the entity
adapter may expose semantic option keys (`low`, `medium`, `high`) while the
protocol layer alone owns the conversion to Samsung values. Without that
evidence the UI exposes stable neutral options (`1`, `2`, `3`).

### Slideshow

`SlideshowMode` is a `StrEnum` with `off`, `sequential`, and `shuffle`.

`SlideshowState` is a frozen dataclass containing:

- `mode: SlideshowMode`
- `duration_minutes: int`
- `category_id: str | None`

`FrameData` gains defaulted `art_settings` and `slideshow` fields. Existing
brightness and color-temperature entities are migrated to the canonical
`ArtSettingsSnapshot`. If temporary compatibility projections remain during
implementation, they must be derived from that snapshot rather than cached
independently.

Art push handling must update `FrameData` with `dataclasses.replace` or a
single snapshot-builder helper. It must not manually reconstruct every field;
otherwise any new field is silently erased by `art_mode_changed` or
`image_selected` events.

When the TV is OFF or the Art generation is not READY, entities do not expose
cached optional values as current. Internally retained last-known values may
support recovery, but published values are unavailable until fresh for the
current READY generation.

## Protocol Boundary

### Aggregate Art settings

`FrameArt` gains a complete-list parser for `get_artmode_settings`. It parses
the JSON string nested under `data`, recognizes known items, ignores unknown
items, and validates each known value independently. A valid complete-list
response is the only response in which an absent key can mean unsupported for
that generation.

Invalid individual values make only that value unknown; the advertised key is
still supported. Malformed whole responses, transport failures, timeouts, and
generation loss leave capability status unknown.

If the aggregate getter receives a correlated `ResponseError`, the device may
fall back to the legacy direct brightness and color-temperature getters. It
must not use legacy fallbacks for timeouts, disconnects, or malformed
responses, because those are not evidence of an older protocol.

### Slideshow readback

Read `get_auto_rotation_status` first. Fall back to
`get_slideshow_status` only after a correlated `ResponseError`.

Normalize:

- `value == "off"` to mode `off`, duration `0`;
- a positive numeric value plus `type == "slideshow"` to `sequential`;
- a positive numeric value plus `type == "shuffleslideshow"` to `shuffle`.

Unknown types or invalid values produce unknown slideshow state without
damaging core coordinator state.

### Mutations

Add exact device operations for:

- motion timer: `off`, `5`, `15`, `30`, `60`, `120`, `240`;
- motion sensitivity: protocol values `1`, `2`, `3`;
- brightness sensor: `on`, `off`.

Validate domains before sending. Mutations run through the existing USER
readiness path, while background reads must never open, pair, or retry a
session.

A mutation is successful only when one of these protocol behaviours has been
verified and fixture-tested:

1. a terminal response correlates by `request_id`/`id`;
2. an exact UUID-less terminal sub-event is registered before send; or
3. the command is deliberately implemented as serialized send followed by an
   authoritative readback that confirms the requested value.

Do not guess an acknowledgement event and do not treat a timeout as success.
An acknowledgement timeout is indeterminate: remove its waiter, retire the
generation, do not retry the mutation, do not update capability/state caches,
and raise a generic Home Assistant error. Only a correlated `ResponseError`
can select a protocol fallback.

Before writable settings are released, capture sanitized LS03B protocol
evidence for the acknowledgement shape. This is a protocol verification gate,
not permission to deploy integration code to the production N150.

## Capability Semantics

Capability knowledge is scoped to an Art-session generation and resets to
unknown when the generation changes.

For each setting, the internal state is unknown, supported, or unsupported:

- present in a validated complete-list response: supported;
- absent from a validated complete-list response: unsupported for that
  generation;
- malformed response, timeout, disconnect, or generation loss: unknown.

For slideshow, track the generation-scoped dialect as unknown,
auto-rotation, legacy, or unsupported. A successful correlated getter chooses
the dialect. One correlated modern `ResponseError` permits the legacy probe;
two correlated `ResponseError` results mean unsupported. Mutations never
promote or demote capability state.

All optional-feature failures are isolated from Art-mode failure accounting.
They cannot increment `_art_fail_streak`, make `tv_mode` unknown, or close a
healthy socket merely because a feature is absent.

## Coordinator Mutation Refresh

Add `async_request_art_reconcile()` to the coordinator. It marks Art details
due immediately and then awaits the coordinator refresh. All successful Art
setting mutations use this method, including the existing brightness and
color-temperature entities and the slideshow service.

The authoritative readback wins over optimistic state. If the command was
acknowledged but the follow-up read fails, the command remains successful and
the affected entity becomes unavailable until reconciliation recovers; the
integration must not claim an unverified value.

## Home Assistant Surfaces

Add `Platform.SELECT` and `select.py`.

### Sleep After select

- Entity category: configuration.
- Options: `off`, `5`, `15`, `30`, `60`, `120`, `240`.
- Stable unique-id suffix: `_art_sleep_after`.
- Translations provide user-friendly durations while state keys remain stable.

### Motion sensitivity select

- Entity category: configuration.
- Stable unique-id suffix: `_art_motion_sensitivity`.
- Preferred options are translated semantic keys only if their protocol
  mapping is verified; otherwise use neutral `1`, `2`, `3` options.
- Unknown or out-of-domain values make the entity unavailable rather than
  dynamically adding protocol data as options.

### Brightness sensor switch

- Entity category: configuration.
- Stable unique-id suffix: `_art_brightness_sensor`.

### Slideshow status sensor

- Read-only ENUM sensor with states `off`, `sequential`, `shuffle`.
- Attributes: `duration_minutes`, `category_id`.
- Stable unique-id suffix: `_art_slideshow`.

All optional entities are created unconditionally because Art capabilities
may be unknown during platform setup. They are available only when the
coordinator is healthy, the TV is not OFF, the Art generation is READY, the
capability is supported, and the current value is valid.

The existing atomic slideshow service remains the only writable slideshow
surface. Splitting duration, order, enabled state, and category into separate
entities would create read-modify-write races and ambiguous disabled-state
defaults.

All new entities use translation keys and stable unique IDs. Existing entity
IDs are not changed. The config-entry schema and version are unchanged.

## Diagnostics

Add `diagnostics.py` with `async_get_config_entry_diagnostics`. It is
auto-discovered and is not added to `PLATFORMS`.

Diagnostics perform no network or device calls and use a strict allowlist.
Safe output includes:

- `loaded`;
- model;
- configured heartbeat;
- coordinator `last_update_success`;
- reachable, power-state, TV-mode, and whether Art state is known;
- Art session state, readiness, and generation;
- Art, UPnP, and unreachable failure counters;
- learned standby precedence;
- sorted supported setting names;
- whether slideshow state is known;
- whether the remote has been confirmed.

Never include:

- host, MAC, token, config-entry title or identifiers;
- current artwork/content IDs or entity-picture URLs;
- running app, volume, or mute state;
- raw request/response/websocket payloads;
- exception text or arbitrary entry options;
- arbitrary device/coordinator private attributes.

Expose an explicit coordinator diagnostics snapshot instead of having
`diagnostics.py` crawl private state. If `runtime_data` is absent, return only
a minimal `loaded: false` result plus safe static information.

## Error Handling

- A correlated `ResponseError` means an unsupported command or protocol
  variant; it does not by itself mark the transport failed.
- A transport error follows the existing Art-session recovery path.
- Malformed optional-feature data invalidates only that optional state.
- Generation checks occur after every awaited Art request; obsolete results
  are never committed.
- Entity mutation errors are generic `HomeAssistantError` messages with
  exception chaining. They do not include host, protocol values, artwork IDs,
  or raw response details.
- Unknown Art push events remain ignored. Settings/slideshow are reconciled on
  READY, timer, and successful mutation rather than inferred from unverified
  push schemas.
- `slideshow_image_changed` must not cause a settings refresh storm.

## Testing Strategy

Implementation is test-driven. Required coverage includes:

### Pure models and parsing

- strings, integers, booleans, missing and duplicate setting items;
- unknown settings and invalid ranges;
- malformed nested JSON and malformed complete-list payloads;
- valid/invalid slideshow modes and durations;
- modern and legacy slideshow dialect selection;
- mapping rules for semantic sensitivity labels, if enabled.

### Transport and device boundary

- exactly one aggregate settings request per reconciliation;
- legacy brightness/color fallback only on correlated `ResponseError`;
- no fallback on timeout, transport failure, or malformed data;
- exact setter payloads and verified acknowledgement fixtures;
- background reads never ensure readiness;
- user mutations do ensure readiness;
- `ResponseError` does not reset a healthy Art session;
- transport/indeterminate failures retire the affected generation without
  retrying a mutation.

### Coordinator

- READY, five-minute, and post-mutation reconciliation cadence;
- no new Art-detail reads on every heartbeat;
- same-generation atomic publication;
- generation loss rejects obsolete results;
- optional-feature failures leave `tv_mode` intact;
- all Art push paths preserve settings and slideshow fields;
- existing brightness/color and slideshow writes force authoritative
  readback.

### Entities

- states, options, attributes, translations, categories, and stable unique IDs;
- TV-off, unready, unknown, unsupported, and malformed-value availability;
- successful calls followed by forced reconciliation;
- generic error messages.

### Diagnostics and regression

- inject canaries into host, MAC, token, entry title/data/options, current art,
  app name, and arbitrary private attributes; prove none appear in serialized
  diagnostics;
- prove diagnostics await no device coroutine;
- extend the explicit `mock_device` fixture so `MagicMock` cannot invent
  truthy optional capabilities;
- run the complete test and lint suites.

## Release Gates

The increment is complete only when:

1. the new tests fail before their implementation and pass afterward;
2. the full regression and lint suites pass;
3. Terra and Fable review the implementation/spec conformance;
4. setter acknowledgement fixtures reflect sanitized observed LS03B protocol
   evidence, or the affected writable feature is deferred;
5. no production N150 deployment or reload has occurred.

