# Samsung Frame TV (Home Assistant)

Accurate OFF / WATCHING / ART-MODE state for Samsung Frame TVs, plus power and basic controls.

## Entities
- `media_player.samsung_frame_tv` — power on (Wake-on-LAN) / off (3 s hold), absolute volume +
  real mute state (via the TV's UPnP service) plus step keys, play/pause/stop, channel up/down,
  and source selection (launches the TV's installed apps, e.g. Netflix). While watching,
  `source`/`app_name` report the foreground app ("TV" when on live TV or an HDMI input).
  What plays *inside* an app is not exposed by the TV's local API.
- `remote.samsung_frame_tv` — send arbitrary key sequences:
  `remote.send_command` with `command: [KEY_HOME, KEY_RIGHT, KEY_ENTER]`, plus
  `num_repeats` / `delay_secs` / `hold_secs` support.
- `binary_sensor.samsung_frame_tv_art_mode` — art mode on/off
- `sensor.samsung_frame_tv_tv_mode` — `off` / `watching` / `art_mode` (use this in automations)
- `sensor.samsung_frame_tv_current_art` — content id of the artwork currently selected
- `number.samsung_frame_tv_art_brightness` — art-mode panel brightness (0–10)

## Services
- `samsungtv_frame.send_key` — send any Samsung remote key code (e.g. `KEY_HOME`, `KEY_MENU`)
- `samsungtv_frame.set_art_mode` — switch art mode on/off directly (TV must be on)
- `samsungtv_frame.select_art` — show an artwork by content id
- `samsungtv_frame.upload_art` — upload a local image to the TV's collection (path must be
  inside `allowlist_external_dirs`); optionally shows it immediately
- `samsungtv_frame.delete_art` — remove an artwork by content id (irreversible)
- `samsungtv_frame.set_slideshow` — rotate art every N minutes (0 disables); categories:
  `MY-C0002` my pictures, `MY-C0004` favourites, `MY-C0008` store

## Options
Settings → Devices & Services → Samsung Frame TV → Configure: polling heartbeat (5–60 s,
default 10). Push events (art mode changes) arrive instantly regardless of the heartbeat.
The IP address can be changed later via the entry's Reconfigure menu.

## Setup
Settings → Devices & Services → Add Integration → "Samsung Frame TV" → enter the IP.
**Accept the "Allow" prompt on the TV once** (do it while the TV is showing normal content, not
art mode). The token is stored; you won't be asked again unless you reset the TV.

## Example automation
```yaml
triggers:
  - trigger: state
    entity_id: sensor.samsung_frame_tv_tv_mode
    from: art_mode
    to: watching
    for: "00:00:10"  # powering off from art briefly passes through 'watching'
```
