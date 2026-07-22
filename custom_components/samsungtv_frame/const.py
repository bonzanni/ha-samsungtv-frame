"""Constants for the Samsung Frame TV integration."""
from __future__ import annotations

import logging

from homeassistant.const import Platform

DOMAIN = "samsungtv_frame"
LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.BINARY_SENSOR,
    Platform.IMAGE,
    Platform.NUMBER,
    Platform.REMOTE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Config entry keys
CONF_HOST = "host"
CONF_MAC = "mac"
CONF_TOKEN = "token"
CONF_MODEL = "model"

# Options
OPT_HEARTBEAT = "heartbeat_seconds"
DEFAULT_HEARTBEAT_SECONDS = 10

# Entity services (registered on media_player)
SERVICE_SEND_KEY = "send_key"
SERVICE_SET_ART_MODE = "set_art_mode"
SERVICE_SELECT_ART = "select_art"
SERVICE_UPLOAD_ART = "upload_art"
SERVICE_DELETE_ART = "delete_art"
SERVICE_SET_SLIDESHOW = "set_slideshow"
SERVICE_CHANGE_MATTE = "change_matte"
SERVICE_SET_PHOTO_FILTER = "set_photo_filter"
SERVICE_SET_FAVOURITE = "set_favourite"
ATTR_MATTE_ID = "matte_id"
ATTR_FILTER_ID = "filter_id"
ATTR_FAVOURITE = "favourite"
ATTR_KEY = "key"
ATTR_ENABLED = "enabled"
ATTR_CONTENT_ID = "content_id"
ATTR_SHOW = "show"
ATTR_PATH = "path"
ATTR_MATTE = "matte"
ATTR_DURATION = "duration_minutes"
ATTR_SHUFFLE = "shuffle"
ATTR_CATEGORY_ID = "category_id"

# Fixed websocket client name — the TV's token grant is keyed to this. Never change.
CLIENT_NAME = "Home Assistant"

PORT_REST = 8001
PORT_WS = 8002

# Consecutive unreachable heartbeats before declaring OFF (debounce transient drops).
OFF_DEBOUNCE_COUNT = 2
# Consecutive failed art queries (TV on) before tv_mode stops holding
# last-stable and reports unknown (a permanently dead art channel must not
# freeze state forever).
ART_FAIL_UNKNOWN_COUNT = 6
# Consecutive failed UPnP volume reads (TV on) before warning once.
UPNP_FAIL_WARN_COUNT = 6
# Art transport deadlines.
ART_CONNECT_DEADLINE = 10
ART_REQUEST_DEADLINE = 20
ART_PROBE_DEADLINE = 5
ART_D2D_DEADLINE = 20
ART_CLOSE_DEADLINE = 5
ART_CLEANUP_RECHECK = 2.0
REMOTE_CLOSE_DEADLINE = 5
# Foreground remote work includes an 8 s open plus a normal 3 s power hold.
# Give cooperative work time to finish before unload requests cancellation.
REMOTE_DRAIN_DEADLINE = 15
# Once cancellation is requested, shutdown must not wait indefinitely for a
# misbehaving library/network coroutine to release device operation ownership.
REMOTE_CANCEL_DEADLINE = 2
ART_RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0)
ART_HOST_RETRY_DELAYS = (60.0, 120.0, 300.0)
ART_DORMANT_SECONDS = 900.0
ART_RETRY_JITTER = 0.20
ART_RECONCILE_SECONDS = 300.0
PAIRING_DEADLINE = 30
# One wedged call must never kill the coordinator: whole-poll deadline.
POLL_DEADLINE = 45

# Curated built-in source catalog: well-known Tizen app ids from
# https://github.com/jaruba/ha-samsungtv-tizen/blob/master/App_IDs.md
DEFAULT_APP_MAP: dict[str, dict[str, str | int]] = {
    "Netflix": {"appId": "11101200001", "app_type": 2},
    "YouTube": {"appId": "111299001912", "app_type": 2},
    "Prime Video": {"appId": "3201910019365", "app_type": 2},
    "Disney+": {"appId": "3201901017640", "app_type": 2},
    "Spotify": {"appId": "3201606009684", "app_type": 2},
    "Plex": {"appId": "3201512006963", "app_type": 2},
    "Apple TV": {"appId": "3201807016597", "app_type": 2},
}

# Wake probe: after turn_on (WoL) the TV takes 10-20 s to boot, and the regular
# heartbeat reacts slowly (each poll of a still-booting TV burns the full REST
# timeout before the next is scheduled). Instead, probe the REST port cheaply
# until it opens, then refresh immediately.
WAKE_PROBE_ATTEMPTS = 30
WAKE_PROBE_DELAY = 1.0
WAKE_PROBE_TIMEOUT = 2.0
