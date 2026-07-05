from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "tok"},
        unique_id="a0:d0:5b:86:ce:b7",
    )


async def test_setup_and_unload(hass, mock_device):
    entry = _make_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data is not None
        # TV reachable at setup -> listener started (as a background task).
        mock_device.async_start_art_listener.assert_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)
        mock_device.async_stop.assert_awaited()


async def test_setup_tv_off_skips_listener(hass, mock_device):
    """A powered-off TV must not stall setup on the listener connect.

    The listener socket has no timeout, so start_listening against an off TV
    blocks on the OS TCP connect timeout (~2 min). Setup must skip it; the
    coordinator starts it later on the unreachable -> reachable edge.
    """
    mock_device.async_device_info.return_value = None
    entry = _make_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data is not None
        mock_device.async_start_art_listener.assert_not_awaited()
        # The recovery hook is still wired for the reachable edge.
        assert entry.runtime_data.restart_listener is not None
