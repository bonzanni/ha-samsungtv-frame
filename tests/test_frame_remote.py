"""Tests for the native async Frame remote transport."""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from samsungtvws.command import SamsungTVSleepCommand
from samsungtvws.event import IGNORE_EVENTS_AT_STARTUP
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError
from samsungtvws.remote import ChannelEmitCommand
from websockets.protocol import State

from custom_components.samsung_tv_frame import frame_remote as frame_remote_module
from custom_components.samsung_tv_frame.frame_remote import (
    FrameRemote,
    RemotePairingRequired,
)


class FakeWebSocket:
    """Controllable websocket for remote transport tests."""

    def __init__(self, frames, *, recv_delay=0):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(json.dumps(frame))
        self.sent = []
        self.closed = False
        self.state = State.OPEN
        self.recv_delay = recv_delay
        self.recv_started = asyncio.Event()
        self.transport = MagicMock()

    async def recv(self):
        self.recv_started.set()
        if self.recv_delay:
            await asyncio.sleep(self.recv_delay)
        return await self.frames.get()

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True
        self.state = State.CLOSED


def make_remote(*, ssl_context=None, **kwargs):
    """Create a remote transport under test."""
    return FrameRemote(
        "1.2.3.4",
        token="tok",
        ssl_context=ssl_context or MagicMock(),
        **kwargs,
    )


async def test_open_captures_token_after_ignored_broadcasts():
    ws = FakeWebSocket(
        [
            {"event": "ms.channel.clientConnect"},
            {
                "event": "ms.channel.connect",
                "data": {"token": "remote-token"},
            },
        ]
    )
    remote = make_remote()
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        assert await remote.open() is ws
    assert remote.token == "remote-token"
    await remote.close()


async def test_open_does_not_log_handshake_token_or_client_data(caplog):
    token = "distinctive-remote-secret-token"
    client_name = "identifying-living-room-client"
    ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {
                    "token": token,
                    "clients": [{"deviceName": client_name}],
                },
            }
        ]
    )
    remote = make_remote()
    caplog.set_level(logging.DEBUG, logger="samsungtvws")

    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        await remote.open()
    await remote.close()

    leaked = {value for value in (token, client_name) if value in caplog.text}
    assert leaked == set()


async def test_open_uses_permanently_quiet_connection_logger(caplog):
    secret = "distinctive-token-in-websocket-url"
    ws = FakeWebSocket([{"event": "ms.channel.connect"}])
    remote = make_remote()
    remote.token = secret
    caplog.set_level(logging.DEBUG)

    async def _connect(url, **kwargs):
        kwargs["logger"].debug("handshake URL %s", url)
        return ws

    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(side_effect=_connect),
    ) as connect_mock:
        await remote.open()

    assert (
        connect_mock.await_args.kwargs["logger"]
        is frame_remote_module._QUIET_WEBSOCKET_LOGGER
    )
    assert secret not in caplog.text
    await remote.close()


async def test_send_app_payload_is_exact_and_absent_from_debug_logs(caplog):
    secret = "synthetic-private-deep-link-metadata"
    command = ChannelEmitCommand.launch_app(
        "synthetic-app-id", "DEEP_LINK", secret
    )
    expected_payload = command.get_payload()
    ws = FakeWebSocket([])
    events = []

    async def _send(payload):
        ws.sent.append(payload)
        events.append(("send", payload))

    async def _sleep(delay):
        events.append(("sleep", delay))

    ws.send = _send
    remote = make_remote()
    remote.connection = ws
    caplog.set_level(logging.DEBUG, logger="samsungtvws")

    with patch.object(frame_remote_module.asyncio, "sleep", _sleep):
        await remote.send_commands([command], key_press_delay=0.25)

    assert ws.sent == [expected_payload]
    assert events == [
        ("send", expected_payload),
        ("sleep", 0.25),
    ]
    assert secret not in caplog.text


async def test_send_raw_dict_preserves_upstream_json_framing(caplog):
    secret = "synthetic-private-raw-command"
    command = {
        "method": "ms.channel.emit",
        "params": {"event": "synthetic.event", "data": secret},
    }
    ws = FakeWebSocket([])
    remote = make_remote()
    remote.connection = ws
    caplog.set_level(logging.DEBUG, logger="samsungtvws")

    await remote.send_commands([command], key_press_delay=0)

    assert ws.sent == [json.dumps(command)]
    assert secret not in caplog.text


async def test_send_sleep_command_uses_command_delay_without_websocket_send():
    ws = FakeWebSocket([])
    remote = make_remote()
    remote.connection = ws
    sleep_mock = AsyncMock()

    with patch.object(frame_remote_module.asyncio, "sleep", sleep_mock):
        await remote.send_commands(
            [SamsungTVSleepCommand(0.75)], key_press_delay=9.0
        )

    sleep_mock.assert_awaited_once_with(0.75)
    assert ws.sent == []


async def test_timeout_event_requires_reauth_and_closes_local_socket():
    ws = FakeWebSocket([{"event": "ms.channel.timeOut"}])
    remote = make_remote()
    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(RemotePairingRequired),
    ):
        await remote.open()
    assert ws.closed
    assert remote.connection is None


async def test_timeout_event_preserves_existing_token():
    ws = FakeWebSocket([{"event": "ms.channel.timeOut"}])
    remote = make_remote()
    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(RemotePairingRequired),
    ):
        await remote.open()

    assert remote.token == "tok"


@pytest.mark.parametrize(
    ("event", "expected_error"),
    [
        ("ms.channel.unauthorized", UnauthorizedError),
        ("unexpected", ConnectionFailure),
    ],
)
async def test_open_failure_closes_local_socket(event, expected_error):
    ws = FakeWebSocket([{"event": event}])
    remote = make_remote()
    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(expected_error),
    ):
        await remote.open()
    assert ws.closed is True
    assert remote.connection is None


@pytest.mark.parametrize(
    ("event", "expected_error", "message"),
    [
        (
            "ms.channel.unauthorized",
            UnauthorizedError,
            "Remote authorization rejected",
        ),
        ("unexpected", ConnectionFailure, "Remote handshake failed"),
    ],
)
async def test_open_failure_does_not_expose_raw_handshake(
    event, expected_error, message
):
    secret = "private-handshake-payload"
    ws = FakeWebSocket([{"event": event, "data": {"token": secret}}])
    remote = make_remote()
    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(expected_error) as raised,
    ):
        await remote.open()

    assert str(raised.value) == message
    assert secret not in str(raised.value)


async def test_generic_open_failure_is_sanitized():
    secret = "private-token-bearing-connect-error"
    remote = make_remote()

    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.connect",
            AsyncMock(side_effect=OSError(secret)),
        ),
        pytest.raises(ConnectionFailure) as raised,
    ):
        await remote.open()

    assert str(raised.value) == "Remote connection failed"
    assert raised.value.__suppress_context__ is True
    assert secret not in str(raised.value)


async def test_open_cancellation_closes_local_socket():
    ws = FakeWebSocket([])
    remote = make_remote()
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        open_task = asyncio.create_task(remote.open())
        await ws.recv_started.wait()
        open_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await open_task

    assert ws.closed is True
    assert remote.connection is None


async def test_cancellation_during_failed_handshake_cleanup_wins_and_aborts():
    ws = FakeWebSocket([{"event": "unexpected"}])
    close_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def _close():
        close_started.set()
        await never_finishes.wait()

    ws.close = _close
    remote = make_remote()
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        open_task = asyncio.create_task(remote.open())
        await close_started.wait()
        open_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await open_task

    ws.transport.abort.assert_called_once_with()
    assert remote.connection is None


async def test_repeated_cancellation_during_failed_handshake_cleanup_wins():
    ws = FakeWebSocket([{"event": "unexpected"}])
    close_started = asyncio.Event()
    first_cancellation = asyncio.Event()
    never_finishes = asyncio.Event()

    async def _cancellation_resistant_close():
        close_started.set()
        try:
            await never_finishes.wait()
        except asyncio.CancelledError:
            first_cancellation.set()
            await never_finishes.wait()

    ws.close = _cancellation_resistant_close
    remote = make_remote()
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        open_task = asyncio.create_task(remote.open())
        await close_started.wait()
        open_task.cancel()
        await first_cancellation.wait()
        open_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await open_task

    ws.transport.abort.assert_called_once_with()
    assert remote.connection is None


async def test_open_timeout_closes_local_socket():
    ws = FakeWebSocket(
        [{"event": "ms.channel.connect"}], recv_delay=0.02
    )
    remote = make_remote(timeout=0.01)
    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(TimeoutError),
    ):
        await remote.open()
    assert ws.closed is True
    assert remote.connection is None


@pytest.mark.parametrize(
    "broadcast_event",
    [
        pytest.param(
            "ms.channel.clientDisconnect", id="client-disconnect"
        ),
        pytest.param(
            IGNORE_EVENTS_AT_STARTUP[0], id="upstream-startup-event"
        ),
    ],
)
async def test_open_tolerates_startup_broadcasts(broadcast_event):
    ws = FakeWebSocket(
        [
            {"event": broadcast_event},
            {"event": "ms.channel.connect"},
        ]
    )
    remote = make_remote()
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        assert await remote.open() is ws
    await remote.close()


async def test_open_passes_injected_ssl_context_and_timeout_to_connect():
    ws = FakeWebSocket([{"event": "ms.channel.connect"}])
    ssl_context = MagicMock()
    remote = make_remote(
        ssl_context=ssl_context,
        timeout=0.123,
    )
    connect_mock = AsyncMock(return_value=ws)
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        connect_mock,
    ):
        assert await remote.open() is ws

    connect_mock.assert_awaited_once_with(
        remote._format_websocket_url(remote.endpoint),
        open_timeout=0.123,
        ssl=ssl_context,
        logger=frame_remote_module._QUIET_WEBSOCKET_LOGGER,
    )
    await remote.close()


async def test_close_closes_successful_websocket():
    ws = FakeWebSocket([{"event": "ms.channel.connect"}])
    remote = make_remote()
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        await remote.open()

    await remote.close()

    assert ws.closed is True
    assert remote.connection is None


async def test_concurrent_open_calls_share_one_connect():
    ws = FakeWebSocket(
        [{"event": "ms.channel.connect"}], recv_delay=0.01
    )
    remote = make_remote()
    connect_mock = AsyncMock(return_value=ws)
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        connect_mock,
    ):
        first, second = await asyncio.gather(remote.open(), remote.open())

    assert first is ws
    assert second is ws
    connect_mock.assert_awaited_once()
    await remote.close()


async def test_close_failure_retains_connection_ownership():
    ws = FakeWebSocket([])
    error = OSError("private-close-failure")
    ws.close = AsyncMock(side_effect=error)
    remote = make_remote()
    remote.connection = ws

    with pytest.raises(OSError) as raised:
        await remote.close()

    assert raised.value is error
    assert remote.connection is ws


async def test_close_timeout_retains_connection_ownership():
    ws = FakeWebSocket([])
    never_finishes = asyncio.Event()
    ws.close = AsyncMock(side_effect=never_finishes.wait)
    remote = make_remote()
    remote.connection = ws

    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.REMOTE_CLOSE_DEADLINE",
            0.01,
        ),
        pytest.raises(TimeoutError),
    ):
        await remote.close()

    assert remote.connection is ws


async def test_close_cancellation_retains_connection_ownership():
    ws = FakeWebSocket([])
    ws.close = AsyncMock(side_effect=asyncio.CancelledError())
    remote = make_remote()
    remote.connection = ws

    with pytest.raises(asyncio.CancelledError):
        await remote.close()

    assert remote.connection is ws


async def test_stop_during_open_cannot_publish_socket_or_token():
    secret = "new-token-from-racing-handshake"
    ws = FakeWebSocket([])
    release_handshake = asyncio.Event()

    async def _recv():
        ws.recv_started.set()
        await release_handshake.wait()
        return json.dumps(
            {"event": "ms.channel.connect", "data": {"token": secret}}
        )

    ws.recv = _recv
    remote = make_remote()
    with patch(
        "custom_components.samsung_tv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        open_task = asyncio.create_task(remote.open())
        await ws.recv_started.wait()
        stop_task = asyncio.create_task(remote.async_stop())
        await asyncio.sleep(0)
        release_handshake.set()

        with pytest.raises(ConnectionFailure, match="Remote transport is stopped"):
            await open_task
        await stop_task

    assert remote.connection is None
    assert remote.token == "tok"
    assert ws.closed is True


async def test_open_after_stop_refuses_without_network():
    remote = make_remote()
    await remote.async_stop()
    connect_mock = AsyncMock()

    with (
        patch(
            "custom_components.samsung_tv_frame.frame_remote.connect",
            connect_mock,
        ),
        pytest.raises(ConnectionFailure, match="Remote transport is stopped"),
    ):
        await remote.open()

    connect_mock.assert_not_awaited()


async def test_terminal_stop_force_aborts_and_detaches_failed_close():
    ws = FakeWebSocket([])
    ws.close = AsyncMock(side_effect=OSError("private-close-failure"))
    remote = make_remote()
    remote.connection = ws

    await remote.async_stop()

    ws.transport.abort.assert_called_once_with()
    assert remote.connection is None


async def test_terminal_stop_bounds_lifecycle_lock_wait_and_aborts():
    ws = FakeWebSocket([])
    close_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_close = asyncio.Event()

    async def _resistant_close():
        close_started.set()
        try:
            await release_close.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release_close.wait()

    ws.close = _resistant_close
    remote = make_remote()
    remote.connection = ws

    with patch(
        "custom_components.samsung_tv_frame.frame_remote.REMOTE_CLOSE_DEADLINE",
        0.01,
    ):
        ordinary_close = asyncio.create_task(remote.close())
        await close_started.wait()
        try:
            await asyncio.wait_for(remote.async_stop(), timeout=0.1)
            assert cancellation_seen.is_set()
            ws.transport.abort.assert_called_once_with()
            assert remote.connection is None
        finally:
            release_close.set()
            await asyncio.gather(ordinary_close, return_exceptions=True)
