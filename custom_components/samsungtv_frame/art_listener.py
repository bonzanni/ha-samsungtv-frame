"""Bridge the sync art-listener thread onto the HA event loop."""
from __future__ import annotations

import json
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .const import LOGGER
from .coordinator import FrameCoordinator


def make_art_bridge(
    hass: HomeAssistant, coordinator: FrameCoordinator
) -> Callable[[str, Any], None]:
    """Return a thread-safe callback for SamsungTVArt.start_listening.

    The library invokes this from its own receive thread with frames shaped as:
        event == "d2d_service_message"
        data  == {"event": "d2d_service_message", "data": '<json string>'}

    The actual art payload lives in data["data"] as a JSON string, e.g.:
        {"event": "art_mode_changed", "value": "on"}

    We decode that inner payload here before hopping onto the event loop, so
    coordinator.handle_art_event receives the inner dict directly.
    """

    def _callback(event: str, data: Any) -> None:
        if event != "d2d_service_message" or not isinstance(data, dict):
            return
        raw = data.get("data")
        if not isinstance(raw, str):
            LOGGER.debug("Ignoring art frame without string payload: %r", data)
            return
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            LOGGER.debug("Ignoring undecodable art payload: %r", raw)
            return
        if not isinstance(payload, dict):
            LOGGER.debug("Ignoring non-dict art payload: %r", payload)
            return
        hass.loop.call_soon_threadsafe(coordinator.handle_art_event, event, payload)

    return _callback
