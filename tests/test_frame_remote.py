"""Tests for the native async Frame remote transport."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError
from websockets.protocol import State

from custom_components.samsungtv_frame.frame_remote import (
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

    async def recv(self):
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
        "custom_components.samsungtv_frame.frame_remote.connect",
        AsyncMock(return_value=ws),
    ):
        assert await remote.open() is ws
    assert remote.token == "remote-token"
    await remote.close()


async def test_timeout_event_requires_reauth_and_closes_local_socket():
    ws = FakeWebSocket([{"event": "ms.channel.timeOut"}])
    remote = make_remote()
    with (
        patch(
            "custom_components.samsungtv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(RemotePairingRequired),
    ):
        await remote.open()
    assert ws.closed
    assert remote.connection is None


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
            "custom_components.samsungtv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(expected_error),
    ):
        await remote.open()
    assert ws.closed is True
    assert remote.connection is None


async def test_open_timeout_closes_local_socket():
    ws = FakeWebSocket(
        [{"event": "ms.channel.connect"}], recv_delay=0.02
    )
    remote = make_remote(timeout=0.01)
    with (
        patch(
            "custom_components.samsungtv_frame.frame_remote.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(TimeoutError),
    ):
        await remote.open()
    assert ws.closed is True
    assert remote.connection is None


async def test_open_passes_injected_ssl_context_and_timeout_to_connect():
    ws = FakeWebSocket([{"event": "ms.channel.connect"}])
    ssl_context = MagicMock()
    remote = make_remote(
        ssl_context=ssl_context,
        timeout=0.123,
    )
    connect_mock = AsyncMock(return_value=ws)
    with patch(
        "custom_components.samsungtv_frame.frame_remote.connect",
        connect_mock,
    ):
        assert await remote.open() is ws

    connect_mock.assert_awaited_once_with(
        remote._format_websocket_url(remote.endpoint),
        open_timeout=0.123,
        ssl=ssl_context,
    )
    await remote.close()


async def test_concurrent_open_calls_share_one_connect():
    ws = FakeWebSocket(
        [{"event": "ms.channel.connect"}], recv_delay=0.01
    )
    remote = make_remote()
    connect_mock = AsyncMock(return_value=ws)
    with patch(
        "custom_components.samsungtv_frame.frame_remote.connect",
        connect_mock,
    ):
        first, second = await asyncio.gather(remote.open(), remote.open())

    assert first is ws
    assert second is ws
    connect_mock.assert_awaited_once()
    await remote.close()
