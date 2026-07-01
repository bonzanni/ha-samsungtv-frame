# Samsung Frame TV — Home Assistant Integration: Design Spec

**Date:** 2026-07-01
**Status:** Approved (brainstorming) — pending implementation plan
**Target hardware validated against:** Samsung `QE65LS03BAUXXH` (`22_PONTUSM_FTV`, 2022 LS03B, 65", WiFi)

---

## 1. Goal & Anchor

Build a standalone, HACS-published Home Assistant custom integration for Samsung Frame
TVs. The **primary, distinguishing goal** is an **accurate, low-latency
OFF / WATCHING / ART tri-state** suitable for reliable automations on state
transitions (e.g. `art_mode → watching`, `off → watching`, and their inverses).
Around that core it provides a full `media_player` control surface and (in a later
phase) art-gallery management.

Why this integration exists: no current integration exposes a clean tri-state.
Core `samsungtv` does ON/OFF only; `ollo69/ha-samsungtv-smart` surfaces art mode as an
*attribute* (coarse state); SmartThings cloud **cannot** distinguish art mode at all.

## 2. Key Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Relationship to existing integrations | **Standalone, full but pragmatic** — self-contained media_player + state, built on the `samsungtvws` library rather than reinventing TV control |
| State exposure | **`media_player` + `binary_sensor.frame_art_mode` + ENUM `sensor.frame_tv_mode`** (conforming *and* automation-ergonomic) |
| Control surface | Full: power + art toggle, source/volume/playback, art management, remote-key passthrough |
| Publishing | **HACS public** — build to community standards |
| Data acquisition | **Approach A — push-first hybrid**: persistent art websocket (push) + reachability heartbeat |
| Art-mode control | Dedicated `set_art_mode` service (NOT overloaded onto `turn_off`) |

## 3. Hardware-Validated State Behavior (this TV)

Probed live 2026-07-01 with `samsungtvws` 3.0.5:

| Physical state | Reachable? | REST `PowerState` | `art.get_artmode()` |
|---|---|---|---|
| WATCHING (Netflix) | yes | `on` | `off` |
| ART MODE | yes | `on` | `on` |
| OFF (3s power hold) | **no — drops off WiFi** (ping 100% loss, TCP 8001/8002 closed, REST timeout) | — | — |

**Derivation rule for this TV:**
```
if not reachable:                     tv_mode = OFF
elif art_mode is True:                tv_mode = ART_MODE      # art WS is source of truth
elif art_mode is False and power_on:  tv_mode = WATCHING
else:                                 tv_mode = UNKNOWN       # transitional → debounce, hold last stable
```
Notes / traps encoded:
- Do **not** gate art detection on `PowerState == "on"` (upstream issue #185 breaks on 2025 LS03F; harmless but avoided here). On this TV `PowerState` is always `on` when reachable, so `art_mode` is the discriminator and **unreachable = OFF**.
- **Caveat:** OFF-via-unreachable is a WiFi standby disconnect. If the TV is ever wired via Ethernet it may stay reachable when off — re-test and fall back to `PowerState == "standby"` for OFF in that case.

## 4. Architecture

```
                     ┌─────────────────────────────────────────┐
                     │  FrameCoordinator  (entry.runtime_data)   │
 art websocket ─push─▶  state machine  ──produces──▶  FrameData  │
 (:8002, art-app)    │                                           │
 reachability ─poll─▶│  (ping/REST :8001, ~10s heartbeat)        │
                     └───────────────────┬───────────────────────┘
                     ┌───────────────────┼───────────────────────┐
                     ▼                   ▼                        ▼
          media_player.frame_tv   binary_sensor.frame_art_mode  sensor.frame_tv_mode
          off/on/playing          on/off                        off/watching/art_mode
          + full control          (by-the-book art boolean)     (automation source of truth)
```

- **Stack:** `samsungtvws[async,encrypted]==3.0.5` (pin; the lib HA core uses, actively
  maintained). Local network only. Ports **8001** (REST `/api/v2/`) + **8002** (token wss,
  channel `com.samsung.art-app`).
- **One config entry per TV.** `entry.runtime_data` holds the coordinator (modern idiom;
  not `hass.data[DOMAIN]`). Typed alias `type FrameConfigEntry = ConfigEntry[FrameCoordinator]`.
- **Manifest:** `integration_type: "device"`, `iot_class: "local_push"`,
  `quality_scale: "silver"`, `version` (CalVer/SemVer), `requirements`, `ssdp`/`dhcp`
  discovery matchers, `loggers: ["samsungtvws"]`.

### 4.1 `FrameData` (single fan-in dataclass)
```python
@dataclass(frozen=True)
class FrameData:
    reachable: bool
    power_state: str | None     # "on" | "standby" | None
    art_mode: bool | None
    tv_mode: TvMode             # OFF | WATCHING | ART_MODE | UNKNOWN (enum)
    current_art: str | None     # content_id in art mode
    source: str | None
    volume: float | None
    muted: bool | None
```
`__eq__` (frozen dataclass) enables coordinator `always_update=False`.

### 4.2 Entities
| Entity | Platform | State/role | Notes |
|---|---|---|---|
| `media_player.frame_tv` | `MediaPlayerEntity`, device_class `TV` | `off`/`on`/`playing` (standard `MediaPlayerState` ONLY) | Control surface. **Never** carries a fake `art_mode` state. |
| `binary_sensor.frame_art_mode` | `BinarySensorEntity` | `on`/`off` | Canonical read-only art boolean; conditions + history. |
| `sensor.frame_tv_mode` | `SensorEntity`, device_class `ENUM`, options `["off","watching","art_mode"]` | tri-state | **Automation source of truth** for transition triggers. |
| `sensor.frame_current_art` (P2) | `SensorEntity` | current `content_id` | Descriptive. |

All grouped under one device (`device_info` identifiers = MAC/`duid`), `has_entity_name = True`,
`PARALLEL_UPDATES = 0` (coordinator-fed).

Example target automation (the payoff):
```yaml
triggers:
  - trigger: state
    entity_id: sensor.frame_tv_mode
    from: art_mode
    to: watching
```

### 4.3 Coordinator & connection lifecycle (Approach A)
- **Persistent art websocket** with callbacks: `art_mode_changed`/`artmode_status` → set
  `art_mode` + `async_set_updated_data`; `go_to_standby` → `art_mode=False`; `wakeup` →
  re-query `get_artmode()`.
- **Reachability + REST heartbeat** (~10s, configurable): confirms OFF (socket dead **and**
  unreachable) and wake-from-off; refreshes `power_state`/source/volume.
- **Connection manager:** connect → listen → on disconnect, exponential-backoff reconnect.
- **OFF debounce:** WiFi drop errors the socket; wait a short settle window / one heartbeat
  before declaring OFF (avoid misreading transient reconnects). `UNKNOWN` is held internally —
  never written as a user-visible flapping state; last stable state is retained until resolved.
- **Read REST before opening a fresh socket** (opening one can wake the TV).
- **Turn-on from OFF = Wake-on-LAN** (MAC from `device.wifiMac`). **Turn-off = `KEY_POWER`
  3s hold** (single press only toggles art mode).

## 5. Config Flow, Discovery & One-Time Pairing

1. **Discovery:** `async_step_ssdp`, `async_step_dhcp` (hostname `samsung*`); manual
   `async_step_user` (IP) fallback.
2. **Identify & dedupe:** REST `/api/v2/`; require `FrameTVSupport == "true"` (reject
   non-Frame Samsungs with a clear error); `unique_id` = `duid`/`format_mac(MAC)`;
   `_abort_if_unique_id_configured()`.
3. **Pairing (one-time Allow):** connect port 8002 with fixed client name **`"Home Assistant"`**;
   TV shows Allow; **capture the returned token**; store in `entry.data[CONF_TOKEN]`.
4. **`test-before-configure`:** prove REST + websocket + token before creating the entry.
5. **Reauth** (`async_step_reauth`): on token rejection, re-pair, update token in place.
6. **Reconfigure** (`async_step_reconfigure`): change IP without deleting the entry.
7. **Options** (`OptionsFlowWithReload`): heartbeat interval, OFF-debounce seconds, WoL
   on/off, whether to create art-management entities.

### 5.1 One-Time-Pairing Guarantee (explicit requirement)
- The user must click **Allow on the TV exactly once**, at setup.
- The token is captured and persisted in the config entry (survives HA restarts/reloads/upgrades).
- Runtime and pairing use the **same fixed client name** (`"Home Assistant"`) — the TV's grant
  is keyed to the name; a differing name would re-prompt.
- Re-prompt happens **only** if the TV revokes the token (factory reset, Reset Smart Hub,
  manual device removal, or firmware token expiry) → `ConfigEntryAuthFailed` → reauth →
  one more Allow.
- **Acceptance test (must pass on real TV):** pair once → restart HA → reconnect with
  **zero TV prompt**. (During throwaway probing the token did not persist to file; the real
  integration must provably capture-and-store it — same mechanism HA core uses.)

## 6. Services / Control Surface

| Capability | Exposed as | Phase |
|---|---|---|
| Power on / off | `media_player.turn_on` (WoL) / `turn_off` (3s hold) | P1 |
| Art mode on/off | `samsungtv_frame.set_art_mode` (also drives state) | P1 |
| Source select | `media_player.select_source` (list from REST/app list) | P1 |
| Volume / mute | `media_player.volume_set/mute/step` | P1 |
| Playback | `media_player.media_play/pause/stop/next/previous` | P1 |
| Remote keys | `samsungtv_frame.send_key` (arbitrary `KEY_*`) | P1 |
| Art management | `select_image`, `upload_image`, `set_brightness`, `set_matte`, `set_slideshow` + `sensor.frame_current_art` | P2 |

Services validate input and raise `ServiceValidationError`/`HomeAssistantError` on bad args or
when the TV state makes the action invalid (e.g. `select_image` while OFF) — Silver
`action-exceptions` rule. No silent no-ops.

## 7. Error Handling & Resilience
- Coordinator raises `ConfigEntryNotReady` (setup retry), `ConfigEntryAuthFailed` (reauth),
  `UpdateFailed` (transient → `unavailable`).
- **OFF vs broken are distinct:** unreachable-when-expected-off = clean `off`;
  unreachable-with-errors mid-operation = `unavailable`. Debounce separates them.
- No silent failures: websocket drops, token rejection, WoL failure logged once at the right
  level and surfaced.

## 8. Testing & Quality
- `pytest` + `pytest-homeassistant-custom-component`, `samsungtvws` mocked.
- **State-derivation truth-table unit tests** (the core value): every row of §3.
- Config-flow tests: discovery, pairing/token capture, dedupe, reauth, reconfigure.
- **Live acceptance tests on the real TV:** one-time-pairing (§5.1), and OFF→WATCHING→ART→OFF
  transition firing on `sensor.frame_tv_mode`.
- **Quality scale:** target **Silver**; path to Gold (discovery, reconfigure, diagnostics,
  translations) later.

## 9. Packaging / HACS
- Repo: `ha-samsungtv-frame`, integration at `custom_components/samsungtv_frame/`.
- `hacs.json`, `manifest.json` (`version`, `documentation`, `issue_tracker`, `codeowners`),
  README, GitHub releases, brand-assets PR to `home-assistant/brands`.
- **Naming:** domain `samsungtv_frame` · display "Samsung Frame TV" · repo `bonzanni/ha-samsungtv-frame`.
- **Manifest identity:** `codeowners: ["@bonzanni"]`,
  `documentation: https://github.com/bonzanni/ha-samsungtv-frame`,
  `issue_tracker: https://github.com/bonzanni/ha-samsungtv-frame/issues`.

## 10. Phasing
| Phase | Contents | Done when |
|---|---|---|
| **P1 — State + core control** | Coordinator (push-first hybrid), 3 entities, tri-state derivation, config flow + one-time pairing, WoL on / 3s-hold off, `set_art_mode`, `send_key`, source/volume/playback | Accurate transitions on real TV; zero-reprompt after restart |
| **P2 — Art management** | `select_image`, `upload_image`, `set_brightness`, `set_matte`, `set_slideshow`, `sensor.frame_current_art` | Curate art from HA |
| **P3 — Release polish** | Diagnostics, translations, full test coverage, brands PR, HACS submission, docs | Publishable |

## 11. Out of Scope (YAGNI)
- SmartThings/cloud path (local-only; cloud can't see art mode anyway). May revisit only as an
  optional OFF fallback for Ethernet-wired or pre-2022 Frames.
- Multi-model quirk handling beyond this TV's behavior — the derivation is written to degrade
  gracefully (unreachable=OFF, `PowerState` fallback) but only validated on the LS03B.
