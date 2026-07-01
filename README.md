# Samsung Frame TV (Home Assistant)

Accurate OFF / WATCHING / ART-MODE state for Samsung Frame TVs, plus power control.

## Entities
- `media_player.samsung_frame_tv` — power on (Wake-on-LAN) / off (3 s hold)
- `binary_sensor.samsung_frame_tv_art_mode` — art mode on/off
- `sensor.samsung_frame_tv_tv_mode` — `off` / `watching` / `art_mode` (use this in automations)

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
```
