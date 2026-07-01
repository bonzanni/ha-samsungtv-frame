"""Config flow stub for Samsung Frame TV (full UI flow is a later task)."""
from __future__ import annotations

from homeassistant import config_entries

from .const import DOMAIN


class SamsungFrameConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Minimal config flow so HA can load config entries."""

    VERSION = 1
