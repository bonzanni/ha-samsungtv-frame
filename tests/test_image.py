# tests/test_image.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_tv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)

ENTITY = "image.samsung_frame_tv_current_art_image"


async def _setup(hass, mock_device):
    mock_device.art_ready = True
    mock_device.art_generation = 1
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "t"},
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsung_tv_frame.FrameDevice", return_value=mock_device
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_image_serves_current_art_thumbnail(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0034"
    mock_device.async_get_art_thumbnail.return_value = b"jpeg-bytes"
    entry = await _setup(hass, mock_device)
    state = hass.states.get(ENTITY)
    assert state is not None
    assert state.state != "unknown"  # timestamp set since current_art known

    coordinator = entry.runtime_data
    entity = hass.data["image"].get_entity(ENTITY)
    image = await entity.async_image()
    assert image == b"jpeg-bytes"
    mock_device.async_get_art_thumbnail.assert_awaited_once_with("MY_F0034")

    # Second call is served from cache — no new fetch.
    await entity.async_image()
    assert mock_device.async_get_art_thumbnail.await_count == 1

    # Art change invalidates the cache and bumps the timestamp.
    mock_device.async_get_art_thumbnail.return_value = b"jpeg-2"
    coordinator.handle_art_event(
        "d2d_service_message",
        {"event": "image_selected", "content_id": "MY_F0042"},
    )
    assert await entity.async_image() == b"jpeg-2"
    assert mock_device.async_get_art_thumbnail.await_count == 2


async def test_image_serves_placeholder_when_thumbnail_refused(hass, mock_device):
    """Store artworks refuse thumbnails (DRM); the bundled placeholder must
    be served instead of a broken image."""
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "SAM-S1110879"
    mock_device.async_get_art_thumbnail.return_value = None
    await _setup(hass, mock_device)
    entity = hass.data["image"].get_entity(ENTITY)
    image = await entity.async_image()
    assert image is not None
    assert image[:3] == b"\xff\xd8\xff"  # bundled JPEG placeholder


async def test_image_keeps_last_thumbnail_on_fetch_failure(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    mock_device.async_get_current_art.return_value = "MY_F0034"
    mock_device.async_get_art_thumbnail.return_value = b"jpeg-bytes"
    entry = await _setup(hass, mock_device)
    entity = hass.data["image"].get_entity(ENTITY)
    assert await entity.async_image() == b"jpeg-bytes"

    mock_device.async_get_art_thumbnail.reset_mock()
    mock_device.async_get_art_thumbnail.return_value = None
    entry.runtime_data.handle_art_event(
        "d2d_service_message",
        {"event": "image_selected", "content_id": "MY_F0042"},
    )
    assert await entity.async_image() == b"jpeg-bytes"
    mock_device.async_get_art_thumbnail.assert_awaited_once_with("MY_F0042")
