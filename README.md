# Samsung Frame TV (Home Assistant)

Accurate OFF / WATCHING / ART-MODE state for Samsung Frame TVs, plus power and basic controls.

## Entities
- `media_player.samsung_frame_tv` — power on (Wake-on-LAN) / off (3 s hold), absolute volume +
  real mute state (via the TV's UPnP service) plus step keys, play/pause/stop, channel up/down,
  and source selection from a curated built-in app catalog (e.g. Netflix). The dropdown is
  not a discovered list of apps installed on the TV, so it can include apps unavailable on
  a particular set. While watching, `source`/`app_name` report a recognized foreground app
  ("TV" when on live TV or an HDMI input). What plays *inside* an app is not exposed by the
  TV's local API.
- `remote.samsung_frame_tv` — send arbitrary key sequences:
  `remote.send_command` with `command: [KEY_HOME, KEY_RIGHT, KEY_ENTER]`, plus
  `num_repeats` / `delay_secs` / `hold_secs` support.
- `binary_sensor.samsung_frame_tv_art_mode` — art mode on/off
- `sensor.samsung_frame_tv_tv_mode` — `off` / `watching` / `art_mode` (use this in automations)
- `switch.samsung_frame_tv_art_mode_switch` — the clickable art ⇄ watching toggle
  (unavailable while the TV is off)
- `sensor.samsung_frame_tv_current_art` — content id of the artwork currently selected
- `image.samsung_frame_tv_current_art_image` — thumbnail of the current artwork (for
  dashboards). Note: Samsung Store artworks are DRM-protected and show a placeholder;
  your own uploaded images display normally.
- `number.samsung_frame_tv_art_brightness` — art-mode panel brightness (0–10)
- `number.samsung_frame_tv_art_color_temperature` — art-mode color temperature (-5…5)
- `select.samsung_frame_tv_art_sleep_after` — local Sleep After control with `off`, 5, 15,
  30, 60, 120, and 240-minute choices
- `select.samsung_frame_tv_art_motion_sensitivity` — local motion-sensitivity control
  using the TV's verified neutral protocol states `1`, `2`, and `3`
- `switch.samsung_frame_tv_art_brightness_sensor` — enable or disable the TV's local
  automatic art-brightness sensor
- `sensor.samsung_frame_tv_art_slideshow` — read-only slideshow state (`off`,
  `sequential`, or `shuffle`) with `duration_minutes` and `category_id` attributes

The brightness, color-temperature, Sleep After, motion-sensitivity, brightness-sensor,
and slideshow entities use authoritative state from the current local Art session; no
SmartThings or other cloud service is required. An unavailable entity means the TV,
Art session, or feature does not currently have an authoritative value—for example,
the TV is off, the session is not ready, or the setting is unsupported or invalid.

## Services
- `samsung_tv_frame.send_key` — send any Samsung remote key code (e.g. `KEY_HOME`, `KEY_MENU`)
- `samsung_tv_frame.set_art_mode` — switch art mode on/off directly (TV must be on)
- `samsung_tv_frame.select_art` — show an artwork by content id
- `samsung_tv_frame.upload_art` — upload a local image to the TV's collection (path must be
  inside `allowlist_external_dirs`); optionally shows it immediately
- `samsung_tv_frame.delete_art` — remove an artwork by content id (irreversible)
- `samsung_tv_frame.set_slideshow` — rotate art every N minutes (0 disables); categories:
  `MY-C0002` my pictures, `MY-C0004` favourites, `MY-C0008` store. This remains the
  only writable slideshow surface and applies duration, shuffle order, and category
  together as one atomic change; `sensor.samsung_frame_tv_art_slideshow` is read-only.
- `samsung_tv_frame.change_matte` / `set_photo_filter` / `set_favourite` — style an artwork
  (all default to the currently displayed one)
- `media_player.play_media` with `media_content_type: app` — launch a catalog app name or a
  raw Tizen app id, optionally with deep-link content:
  `extra: {meta_tag: "v=VIDEO_ID"}` (support varies per app)

## Device triggers
"Turned off", "Started watching" and "Entered art mode" are available directly in the
automation editor (Device → Samsung Frame TV), each with an optional duration filter.

## Options
Settings → Devices & Services → Samsung Frame TV → Configure: polling heartbeat (5–60 s,
default 10). Push events (art mode changes) arrive instantly regardless of the heartbeat.
The IP address can be changed later via the entry's Reconfigure menu.

## Reliability
Version 0.6.9 pairs the remote-control channel first and uses its returned token for
both remote control and Art. A changed remote token is stored before a successful
foreground command returns, including power-off. Ordinary stale remote connections
close the exact failed client before one same-credential retry. An
`ms.channel.timeOut` protocol response during a foreground remote command starts Home
Assistant reauthorization instead of silently retrying without a token. This response
is indeterminate and is not treated as proof that the stored credential is invalid;
background polling never opens a pairing prompt.

Since version 0.6.8, the integration supervises the Art websocket as one
long-lived session. When the TV's internal Art host is unavailable, Home Assistant
backs off instead of reconnecting on every heartbeat; healthy state remains push-driven
with periodic reconciliation over the existing socket. No configuration migration is
required when upgrading.

Art commands, push events, thumbnails, uploads, pairing, and shutdown use cancellable
async I/O.

## Diagnostics

Home Assistant's **Download diagnostics** action reports an allowlisted snapshot of
integration health, Art-session readiness, and known local feature support without
contacting the TV. It excludes the TV address, MAC, tokens, entry identifiers, artwork
IDs, app/media state, raw payloads, and arbitrary configuration data.

## Setup
Settings → Devices & Services → Add Integration → "Samsung Frame TV" → enter the IP.
During first setup or reauthorization, show normal TV or app content, accept the
**"Allow"** prompt on the TV, and leave **Access Notification** set to **First Time
Only**. The integration pairs the remote-control channel and validates Art with the
returned token. It never opens a pairing prompt from background polling.

## UI notes
- **Volume**: the media player exposes absolute volume, so HA shows a slider; the mute
  button is the speaker icon at its left end. ⏮/⏭ are channel down/up while watching.
- **Remote entity toggle**: toggling `remote.*` powers the TV on/off; its main purpose is
  `remote.send_command` for key sequences in automations.
- A ready-made remote-control dashboard card (d-pad, apps, art toggle, current-art image)
  is in [`examples/remote-card.yaml`](examples/remote-card.yaml).

## Example automation
```yaml
triggers:
  - trigger: state
    entity_id: sensor.samsung_frame_tv_tv_mode
    from: art_mode
    to: watching
    for: "00:00:10"  # powering off from art briefly passes through 'watching'
```
