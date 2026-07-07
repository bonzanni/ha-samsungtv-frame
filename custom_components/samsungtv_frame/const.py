"""Constants for the Samsung Frame TV integration."""
from __future__ import annotations

import logging

from homeassistant.const import Platform

DOMAIN = "samsungtv_frame"
LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.REMOTE,
    Platform.SENSOR,
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

# Wake probe: after turn_on (WoL) the TV takes 10-20 s to boot, and the regular
# heartbeat reacts slowly (each poll of a still-booting TV burns the full REST
# timeout before the next is scheduled). Instead, probe the REST port cheaply
# until it opens, then refresh immediately.
WAKE_PROBE_ATTEMPTS = 30
WAKE_PROBE_DELAY = 1.0
WAKE_PROBE_TIMEOUT = 2.0
