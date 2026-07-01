from unittest.mock import MagicMock

from custom_components.samsungtv_frame.art_listener import make_art_bridge


async def test_bridge_marshals_event_to_loop(hass):
    coordinator = MagicMock()
    bridge = make_art_bridge(hass, coordinator)
    # Called from a non-loop thread in reality; here we just assert it schedules.
    bridge("d2d_service_message", {"event": "art_mode_changed", "value": "on"})
    await hass.async_block_till_done()
    coordinator.handle_art_event.assert_called_once_with(
        "d2d_service_message", {"event": "art_mode_changed", "value": "on"}
    )
