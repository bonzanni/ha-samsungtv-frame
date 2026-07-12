from unittest.mock import call, patch

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
    callback_was_wired = False

    def _capture_callback(_callback):
        nonlocal callback_was_wired
        callback_was_wired = True

    async def _device_info():
        assert callback_was_wired
        return {
            "PowerState": "on",
            "FrameTVSupport": "true",
            "wifiMac": "A0:D0:5B:86:CE:B7",
            "modelName": "QE65LS03BAUXXH",
        }

    ssl_context = object()
    mock_device.listener_alive = False
    mock_device.set_art_event_callback.side_effect = _capture_callback
    mock_device.async_device_info.side_effect = _device_info
    with (
        patch(
            "custom_components.samsungtv_frame.FrameDevice",
            return_value=mock_device,
        ) as device_cls,
        patch(
            "custom_components.samsungtv_frame.get_ssl_context",
            return_value=ssl_context,
        ) as get_context,
        patch.object(
            hass,
            "async_add_executor_job",
            wraps=hass.async_add_executor_job,
        ) as executor_job,
        patch.object(
            entry,
            "async_create_background_task",
            wraps=entry.async_create_background_task,
        ) as create_background_task,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data is not None

        assert executor_job.call_args_list.count(call(get_context)) == 1
        task_factory = device_cls.call_args.kwargs["task_factory"]
        assert device_cls.call_args.kwargs["ssl_context"] is ssl_context
        mock_device.set_art_event_callback.assert_called_once_with(
            entry.runtime_data.handle_art_event
        )
        assert (
            entry.runtime_data.restart_listener
            == mock_device.async_restart_art_listener
        )
        mock_device.async_start_art_listener.assert_not_awaited()
        assert all(
            task_call.args[2] != "samsungtv_frame-listener-restart"
            for task_call in create_background_task.call_args_list
        )
        mock_device.async_restart_art_listener.assert_not_awaited()

        create_background_task.reset_mock()

        async def _owned_work():
            return None

        coroutine = _owned_work()
        task = task_factory(coroutine, "owned-work")
        create_background_task.assert_called_once_with(hass, coroutine, "owned-work")
        await task

        assert await hass.config_entries.async_unload(entry.entry_id)
        mock_device.async_stop.assert_awaited()


async def test_setup_tv_off_skips_listener(hass, mock_device):
    """A powered-off TV must not stall setup on the listener connect.

    The native receiver starts only when a reachable poll requests art state.
    Setup while the TV is off must therefore perform no art connection.
    """
    mock_device.async_device_info.return_value = None
    entry = _make_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
        ),
        patch(
            "custom_components.samsungtv_frame.get_ssl_context",
            return_value=object(),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.runtime_data is not None
        mock_device.async_get_artmode.assert_not_awaited()
        mock_device.async_start_art_listener.assert_not_awaited()
        # The recovery hook is still wired for the reachable edge.
        assert entry.runtime_data.restart_listener is not None
