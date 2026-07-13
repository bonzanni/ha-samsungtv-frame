import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame import (
    async_setup_entry,
    async_unload_entry,
)
from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)
from custom_components.samsungtv_frame.coordinator import FrameCoordinator


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "tok"},
        unique_id="a0:d0:5b:86:ce:b7",
    )


async def test_setup_and_unload(hass, mock_device):
    entry = _make_entry()
    entry.add_to_hass(hass)
    setup_order: list[str] = []

    def _capture_event_callback(_callback):
        assert setup_order == []
        setup_order.append("event-callback")

    def _capture_state_callback(_callback):
        if _callback is None:
            setup_order.append("state-clear")
            return
        assert setup_order == ["event-callback"]
        setup_order.append("state-callback")

    def _capture_token_callback(_callback):
        if _callback is None:
            setup_order.append("token-clear")
            return
        assert setup_order == ["event-callback", "state-callback"]
        setup_order.append("token-callback")

    def _capture_reauth_callback(_callback):
        if _callback is None:
            setup_order.append("reauth-clear")
            return
        assert setup_order == [
            "event-callback",
            "state-callback",
            "token-callback",
        ]
        setup_order.append("reauth-callback")

    async def _start_session():
        assert setup_order == [
            "event-callback",
            "state-callback",
            "token-callback",
            "reauth-callback",
        ]
        setup_order.append("session-start")

    async def _device_info():
        assert setup_order == [
            "event-callback",
            "state-callback",
            "token-callback",
            "reauth-callback",
            "session-start",
        ]
        setup_order.append("first-refresh")
        return {
            "PowerState": "on",
            "FrameTVSupport": "true",
            "wifiMac": "A0:D0:5B:86:CE:B7",
            "modelName": "QE65LS03BAUXXH",
        }

    ssl_context = object()
    mock_device.set_art_event_callback.side_effect = _capture_event_callback
    mock_device.set_art_session_state_callback.side_effect = (
        _capture_state_callback
    )
    mock_device.set_remote_token_callback.side_effect = (
        _capture_token_callback
    )
    mock_device.set_remote_reauth_callback.side_effect = (
        _capture_reauth_callback
    )
    mock_device.async_start_art_session.side_effect = _start_session
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
        mock_device.set_art_session_state_callback.assert_called_once_with(
            entry.runtime_data.handle_art_session_state
        )
        mock_device.set_remote_token_callback.assert_called_once_with(
            entry.runtime_data.handle_remote_token
        )
        mock_device.set_remote_reauth_callback.assert_called_once_with(
            entry.runtime_data.handle_remote_reauth
        )
        mock_device.async_start_art_session.assert_awaited_once()
        assert setup_order == [
            "event-callback",
            "state-callback",
            "token-callback",
            "reauth-callback",
            "session-start",
            "first-refresh",
        ]
        assert all(
            task_call.args[2] != "samsungtv_frame-listener-restart"
            for task_call in create_background_task.call_args_list
        )

        create_background_task.reset_mock()

        async def _owned_work():
            return None

        coroutine = _owned_work()
        task = task_factory(coroutine, "owned-work")
        create_background_task.assert_called_once_with(hass, coroutine, "owned-work")
        await task

        assert await hass.config_entries.async_unload(entry.entry_id)
        mock_device.async_stop.assert_awaited()
        assert (
            mock_device.set_art_session_state_callback.call_args_list[-1]
            == call(None)
        )
        assert mock_device.set_remote_token_callback.call_args_list[-1] == call(None)
        assert mock_device.set_remote_reauth_callback.call_args_list[-1] == call(None)


async def test_setup_tv_off_arms_session_without_art_io(hass, mock_device):
    """A powered-off TV arms recovery without opening the Art transport.

    The session starts accepting observations before the first refresh, but a
    powered-off refresh must perform no Art request.
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
        mock_device.async_start_art_session.assert_awaited_once()
        mock_device.async_get_artmode.assert_not_awaited()
        mock_device.set_art_session_state_callback.assert_called_once_with(
            entry.runtime_data.handle_art_session_state
        )


async def test_setup_start_failure_clears_callback_and_bounds_cleanup(
    hass, mock_device, caplog
):
    entry = _make_entry()
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()
    start_error = RuntimeError("session start failed")
    mock_device.async_start_art_session.side_effect = start_error
    private_value = "private-remote-credential"

    def _fail_token_callback_clear(callback):
        if callback is None:
            raise RuntimeError(private_value)

    mock_device.set_remote_token_callback.side_effect = (
        _fail_token_callback_clear
    )

    async def _wedged_stop():
        cleanup_started.set()
        try:
            await never_finishes.wait()
        finally:
            cleanup_cancelled.set()

    mock_device.async_stop.side_effect = _wedged_stop
    with (
        patch(
            "custom_components.samsungtv_frame.FrameDevice",
            return_value=mock_device,
        ),
        patch(
            "custom_components.samsungtv_frame.get_ssl_context",
            return_value=object(),
        ),
        patch(
            "custom_components.samsungtv_frame.ART_CONNECT_DEADLINE",
            0.01,
            create=True,
        ),
        patch(
            "custom_components.samsungtv_frame.ART_CLOSE_DEADLINE",
            0.01,
            create=True,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(),
        ),
    ):
        with pytest.raises(RuntimeError, match="session start failed") as raised:
            await asyncio.wait_for(
                async_setup_entry(hass, entry), timeout=0.2
            )

    assert raised.value is start_error
    assert cleanup_started.is_set()
    assert cleanup_cancelled.is_set()
    assert (
        mock_device.set_art_session_state_callback.call_args_list[-1]
        == call(None)
    )
    assert mock_device.set_remote_token_callback.call_args_list[-1] == call(None)
    assert mock_device.set_remote_reauth_callback.call_args_list[-1] == call(None)
    assert private_value not in caplog.text


async def test_setup_first_refresh_failure_clears_callback_and_bounds_cleanup(
    hass, mock_device
):
    entry = _make_entry()
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()
    refresh_error = RuntimeError("first refresh failed")

    async def _wedged_stop():
        cleanup_started.set()
        try:
            await never_finishes.wait()
        finally:
            cleanup_cancelled.set()

    mock_device.async_stop.side_effect = _wedged_stop
    with (
        patch(
            "custom_components.samsungtv_frame.FrameDevice",
            return_value=mock_device,
        ),
        patch(
            "custom_components.samsungtv_frame.get_ssl_context",
            return_value=object(),
        ),
        patch.object(
            FrameCoordinator,
            "async_config_entry_first_refresh",
            AsyncMock(side_effect=refresh_error),
        ),
        patch(
            "custom_components.samsungtv_frame.ART_CONNECT_DEADLINE",
            0.01,
            create=True,
        ),
        patch(
            "custom_components.samsungtv_frame.ART_CLOSE_DEADLINE",
            0.01,
            create=True,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(),
        ),
    ):
        with pytest.raises(RuntimeError, match="first refresh failed") as raised:
            await asyncio.wait_for(
                async_setup_entry(hass, entry), timeout=0.2
            )

    assert raised.value is refresh_error
    mock_device.async_start_art_session.assert_awaited_once()
    assert cleanup_started.is_set()
    assert cleanup_cancelled.is_set()
    assert (
        mock_device.set_art_session_state_callback.call_args_list[-1]
        == call(None)
    )
    assert mock_device.set_remote_token_callback.call_args_list[-1] == call(None)
    assert mock_device.set_remote_reauth_callback.call_args_list[-1] == call(None)


async def test_setup_platform_forward_failure_cleans_device_session(
    hass, mock_device
):
    entry = _make_entry()
    cleanup_started = asyncio.Event()
    cleanup_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()
    forward_error = RuntimeError("platform forwarding failed")
    forwarded_runtime = []

    async def _wedged_stop():
        cleanup_started.set()
        try:
            await never_finishes.wait()
        finally:
            cleanup_cancelled.set()

    async def _failed_forward(_entry, _platforms):
        assert entry.runtime_data is not None
        forwarded_runtime.append(entry.runtime_data)
        raise forward_error

    mock_device.async_stop.side_effect = _wedged_stop
    with (
        patch(
            "custom_components.samsungtv_frame.FrameDevice",
            return_value=mock_device,
        ),
        patch(
            "custom_components.samsungtv_frame.get_ssl_context",
            return_value=object(),
        ),
        patch.object(
            FrameCoordinator,
            "async_config_entry_first_refresh",
            AsyncMock(),
        ) as first_refresh,
        patch(
            "custom_components.samsungtv_frame.ART_CONNECT_DEADLINE",
            0.01,
            create=True,
        ),
        patch(
            "custom_components.samsungtv_frame.ART_CLOSE_DEADLINE",
            0.01,
            create=True,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(side_effect=_failed_forward),
        ),
    ):
        with pytest.raises(
            RuntimeError, match="platform forwarding failed"
        ) as raised:
            await asyncio.wait_for(
                async_setup_entry(hass, entry), timeout=0.2
            )

    assert raised.value is forward_error
    mock_device.async_start_art_session.assert_awaited_once()
    first_refresh.assert_awaited_once()
    assert cleanup_started.is_set()
    assert cleanup_cancelled.is_set()
    assert (
        mock_device.set_art_session_state_callback.call_args_list[-1]
        == call(None)
    )
    assert mock_device.set_remote_token_callback.call_args_list[-1] == call(None)
    assert mock_device.set_remote_reauth_callback.call_args_list[-1] == call(None)
    assert entry.runtime_data is forwarded_runtime[0]


async def test_unload_removes_state_callback_before_platforms_and_stop(
    hass, mock_device
):
    entry = _make_entry()
    coordinator = MagicMock(spec=FrameCoordinator)
    coordinator.device = mock_device
    entry.runtime_data = coordinator
    order: list[str] = []

    def _set_callback(callback):
        assert callback is None
        order.append("state-clear")

    def _set_token_callback(callback):
        assert callback is None
        order.append("token-clear")

    def _set_reauth_callback(callback):
        assert callback is None
        order.append("reauth-clear")

    async def _unload_platforms(_entry, _platforms):
        order.append("platform-unload")
        return True

    async def _stop():
        order.append("device-stop")

    mock_device.set_art_session_state_callback.side_effect = _set_callback
    mock_device.set_remote_token_callback.side_effect = _set_token_callback
    mock_device.set_remote_reauth_callback.side_effect = _set_reauth_callback
    mock_device.async_stop.side_effect = _stop
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(side_effect=_unload_platforms),
    ):
        assert await async_unload_entry(hass, entry)

    assert order == [
        "state-clear",
        "token-clear",
        "reauth-clear",
        "platform-unload",
        "device-stop",
    ]
    mock_device.set_art_session_state_callback.assert_called_once_with(None)
    mock_device.set_remote_token_callback.assert_called_once_with(None)
    mock_device.set_remote_reauth_callback.assert_called_once_with(None)


async def test_unload_restores_state_callback_when_platform_unload_fails(
    hass, mock_device
):
    entry = _make_entry()
    coordinator = MagicMock(spec=FrameCoordinator)
    coordinator.device = mock_device
    entry.runtime_data = coordinator
    callback = coordinator.handle_art_session_state

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=False),
    ):
        assert not await async_unload_entry(hass, entry)

    assert mock_device.set_art_session_state_callback.call_args_list == [
        call(None),
        call(callback),
    ]
    assert mock_device.set_remote_token_callback.call_args_list == [
        call(None),
        call(coordinator.handle_remote_token),
    ]
    assert mock_device.set_remote_reauth_callback.call_args_list == [
        call(None),
        call(coordinator.handle_remote_reauth),
    ]
    mock_device.async_stop.assert_not_awaited()


async def test_unload_restores_state_callback_when_platform_unload_raises(
    hass, mock_device
):
    entry = _make_entry()
    coordinator = MagicMock(spec=FrameCoordinator)
    coordinator.device = mock_device
    entry.runtime_data = coordinator
    callback = coordinator.handle_art_session_state
    unload_error = RuntimeError("platform unload failed")

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(side_effect=unload_error),
        ),
        pytest.raises(RuntimeError, match="platform unload failed") as raised,
    ):
        await async_unload_entry(hass, entry)

    assert raised.value is unload_error
    assert mock_device.set_art_session_state_callback.call_args_list == [
        call(None),
        call(callback),
    ]
    assert mock_device.set_remote_token_callback.call_args_list == [
        call(None),
        call(coordinator.handle_remote_token),
    ]
    assert mock_device.set_remote_reauth_callback.call_args_list == [
        call(None),
        call(coordinator.handle_remote_reauth),
    ]
    mock_device.async_stop.assert_not_awaited()
