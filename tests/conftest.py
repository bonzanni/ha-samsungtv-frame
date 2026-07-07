"""Shared fixtures for Samsung Frame TV tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture
def mock_device() -> MagicMock:
    """A mocked FrameDevice with async methods."""
    device = MagicMock()
    device.async_device_info = AsyncMock(
        return_value={"PowerState": "on", "FrameTVSupport": "true",
                      "wifiMac": "A0:D0:5B:86:CE:B7", "modelName": "QE65LS03BAUXXH"}
    )
    device.async_get_artmode = AsyncMock(return_value=False)
    device.async_set_artmode = AsyncMock()
    device.async_turn_on = AsyncMock()
    device.async_turn_off = AsyncMock()
    device.async_start_art_listener = AsyncMock()
    device.async_stop = AsyncMock()
    device.async_send_key = AsyncMock()
    device.async_launch_app = AsyncMock()
    device.async_app_list = AsyncMock(return_value=None)
    device.async_get_current_art = AsyncMock(return_value=None)
    device.async_get_art_brightness = AsyncMock(return_value=None)
    device.async_set_art_brightness = AsyncMock()
    device.async_select_art = AsyncMock()
    device.async_upload_art = AsyncMock(return_value="MY_F9999")
    device.async_delete_art = AsyncMock()
    device.async_set_slideshow = AsyncMock()
    device.newest_token = None  # plain attr: MagicMock's default would be truthy
    return device
