"""Bridge the sync art-listener thread onto the HA event loop."""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .coordinator import FrameCoordinator


def make_art_bridge(
    hass: HomeAssistant, coordinator: FrameCoordinator
) -> Callable[[str, Any], None]:
    """Return a thread-safe callback for SamsungTVArt.start_listening.

    The library invokes this from its own receive thread, so we hop onto the
    event loop before touching coordinator state.
    """

    def _callback(event: str, data: Any) -> None:
        hass.loop.call_soon_threadsafe(coordinator.handle_art_event, event, data)

    return _callback
