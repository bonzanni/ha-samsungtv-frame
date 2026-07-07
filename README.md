# Samsung Frame TV (Home Assistant)

Accurate OFF / WATCHING / ART-MODE state for Samsung Frame TVs, plus power and basic controls.

## Entities
- `media_player.samsung_frame_tv` — power on (Wake-on-LAN) / off (3 s hold), volume up/down/mute,
  play/pause, and source selection (launches the TV's installed apps, e.g. Netflix)
- `binary_sensor.samsung_frame_tv_art_mode` — art mode on/off
- `sensor.samsung_frame_tv_tv_mode` — `off` / `watching` / `art_mode` (use this in automations)

## Services
- `samsungtv_frame.send_key` — send any Samsung remote key code (e.g. `KEY_HOME`, `KEY_MENU`)
- `samsungtv_frame.set_art_mode` — switch art mode on/off directly (TV must be on)

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
