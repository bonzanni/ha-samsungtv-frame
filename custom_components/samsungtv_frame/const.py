"""Constants for the Samsung Frame TV integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "samsungtv_frame"
LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

# Config entry keys
CONF_HOST = "host"
CONF_MAC = "mac"
CONF_TOKEN = "token"
CONF_MODEL = "model"

# Fixed websocket client name — the TV's token grant is keyed to this. Never change.
CLIENT_NAME = "Home Assistant"

PORT_REST = 8001
PORT_WS = 8002

DEFAULT_HEARTBEAT = timedelta(seconds=10)
# Consecutive unreachable heartbeats before declaring OFF (debounce transient drops).
OFF_DEBOUNCE_COUNT = 2
