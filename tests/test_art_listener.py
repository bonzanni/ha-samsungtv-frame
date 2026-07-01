from unittest.mock import MagicMock

from custom_components.samsungtv_frame.art_listener import make_art_bridge


async def test_bridge_decodes_d2d_and_forwards_inner(hass):
    """Real TV shape: event is d2d_service_message, inner payload in data["data"]."""
    coordinator = MagicMock()
    bridge = make_art_bridge(hass, coordinator)
    bridge(
        "d2d_service_message",
        {"event": "d2d_service_message", "data": '{"event": "art_mode_changed", "value": "on"}'},
    )
    await hass.async_block_till_done()
    coordinator.handle_art_event.assert_called_once_with(
        "d2d_service_message", {"event": "art_mode_changed", "value": "on"}
    )


async def test_bridge_ignores_non_d2d(hass):
    """Non-D2D events (e.g. channel connect) must be silently dropped."""
    coordinator = MagicMock()
    bridge = make_art_bridge(hass, coordinator)
    bridge("ms.channel.clientConnect", {"event": "ms.channel.clientConnect"})
    await hass.async_block_till_done()
    coordinator.handle_art_event.assert_not_called()


async def test_bridge_ignores_unparseable(hass):
    """Malformed JSON in data["data"] must be silently dropped."""
    coordinator = MagicMock()
    bridge = make_art_bridge(hass, coordinator)
    bridge("d2d_service_message", {"event": "d2d_service_message", "data": "not json{"})
    await hass.async_block_till_done()
    coordinator.handle_art_event.assert_not_called()


async def test_bridge_ignores_missing_data_key(hass):
    """d2d_service_message frame with no data key is silently dropped."""
    coordinator = MagicMock()
    bridge = make_art_bridge(hass, coordinator)
    bridge("d2d_service_message", {"event": "d2d_service_message"})
    await hass.async_block_till_done()
    coordinator.handle_art_event.assert_not_called()


async def test_bridge_ignores_non_dict_data(hass):
    """data["data"] that parses to a non-dict (e.g. a list) is dropped."""
    coordinator = MagicMock()
    bridge = make_art_bridge(hass, coordinator)
    bridge("d2d_service_message", {"event": "d2d_service_message", "data": '["not", "a", "dict"]'})
    await hass.async_block_till_done()
    coordinator.handle_art_event.assert_not_called()
