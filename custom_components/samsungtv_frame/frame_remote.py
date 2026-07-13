"""Native async transport for the Samsung Frame remote websocket."""
from __future__ import annotations

import asyncio
import contextlib
import json

from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.event import (
    IGNORE_EVENTS_AT_STARTUP,
    MS_CHANNEL_CLIENT_CONNECT_EVENT,
    MS_CHANNEL_CLIENT_DISCONNECT_EVENT,
    MS_CHANNEL_CONNECT_EVENT,
    MS_CHANNEL_UNAUTHORIZED,
)
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError
from websockets.asyncio.client import ClientConnection, connect

from .const import CLIENT_NAME, PORT_WS, REMOTE_CLOSE_DEADLINE


class RemotePairingRequired(ConnectionFailure):
    """The remote channel requires the user to authorize this client."""


_HANDSHAKE_BROADCASTS = {
    *IGNORE_EVENTS_AT_STARTUP,
    MS_CHANNEL_CLIENT_CONNECT_EVENT,
    MS_CHANNEL_CLIENT_DISCONNECT_EVENT,
}


class FrameRemote(SamsungTVWSAsyncRemote):
    """Own the bounded Frame remote websocket handshake."""

    def __init__(
        self,
        host,
        *,
        token,
        ssl_context,
        port=PORT_WS,
        name=CLIENT_NAME,
        timeout=8,
    ):
        super().__init__(
            host,
            token=token,
            port=port,
            name=name,
            timeout=timeout,
        )
        self._ssl_context = ssl_context
        self._open_lock = asyncio.Lock()

    async def open(self) -> ClientConnection:
        """Open and complete the bounded remote websocket handshake."""
        async with self._open_lock:
            if self.connection is not None:
                return self.connection

            websocket = None
            try:
                deadline = self.timeout or 8
                async with asyncio.timeout(deadline):
                    websocket = await connect(
                        self._format_websocket_url(self.endpoint),
                        open_timeout=deadline,
                        ssl=self._ssl_context,
                    )
                    event = None
                    while event is None or event in _HANDSHAKE_BROADCASTS:
                        frame = json.loads(await websocket.recv())
                        event = frame.get("event", "*")
                    if event == MS_CHANNEL_UNAUTHORIZED:
                        raise UnauthorizedError(frame)
                    if event == "ms.channel.timeOut":
                        raise RemotePairingRequired(
                            "Remote authorization required"
                        )
                    if event != MS_CHANNEL_CONNECT_EVENT:
                        raise ConnectionFailure(frame)
                    data = frame.get("data")
                    if isinstance(data, dict):
                        token = data.get("token")
                        if isinstance(token, str) and token:
                            self.token = token
                self.connection = websocket
                return websocket
            except BaseException:
                if websocket is not None:
                    with contextlib.suppress(Exception, TimeoutError):
                        async with asyncio.timeout(REMOTE_CLOSE_DEADLINE):
                            await websocket.close()
                self.connection = None
                raise
