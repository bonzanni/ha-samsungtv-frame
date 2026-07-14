"""Native async transport for the Samsung Frame remote websocket."""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.command import SamsungTVCommand, SamsungTVSleepCommand
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
from .websocket_privacy import (
    QUIET_WEBSOCKET_LOGGER as _QUIET_WEBSOCKET_LOGGER,
    process_api_response_silently,
)


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
        self._lifecycle_lock = asyncio.Lock()
        self._stopped = False

    @staticmethod
    async def _send_command(
        connection: ClientConnection,
        command: SamsungTVCommand | dict[str, Any],
        delay: float,
    ) -> None:
        """Send one command without exposing its payload through logging."""
        if isinstance(command, SamsungTVSleepCommand):
            await asyncio.sleep(command.delay)
            return
        if isinstance(command, SamsungTVCommand):
            payload = command.get_payload()
        else:
            payload = json.dumps(command)
        await connection.send(payload)
        await asyncio.sleep(delay)

    @staticmethod
    def _force_abort(websocket: ClientConnection) -> None:
        """Abort a socket whose graceful close could not establish ownership."""
        transport = getattr(websocket, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            with contextlib.suppress(Exception):
                abort()

    async def _async_close_unpublished(
        self, websocket: ClientConnection
    ) -> None:
        """Close or abort a handshake socket that was never published."""
        try:
            async with asyncio.timeout(REMOTE_CLOSE_DEADLINE):
                await websocket.close()
        except asyncio.CancelledError:
            self._force_abort(websocket)
            raise
        except BaseException:
            self._force_abort(websocket)

    async def open(self) -> ClientConnection:
        """Open and complete the bounded remote websocket handshake."""
        async with self._lifecycle_lock:
            if self._stopped:
                raise ConnectionFailure("Remote transport is stopped")
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
                        logger=_QUIET_WEBSOCKET_LOGGER,
                    )
                    event = None
                    while event is None or event in _HANDSHAKE_BROADCASTS:
                        frame = process_api_response_silently(
                            await websocket.recv()
                        )
                        event = frame.get("event", "*")
                    if event == MS_CHANNEL_UNAUTHORIZED:
                        raise UnauthorizedError(
                            "Remote authorization rejected"
                        )
                    if event == "ms.channel.timeOut":
                        raise RemotePairingRequired(
                            "Remote authorization required"
                        )
                    if event != MS_CHANNEL_CONNECT_EVENT:
                        raise ConnectionFailure("Remote handshake failed")
                    data = frame.get("data")
                    token = None
                    if isinstance(data, dict):
                        token = data.get("token")
                    if self._stopped:
                        raise ConnectionFailure(
                            "Remote transport is stopped"
                        )
                    if isinstance(token, str) and token:
                        self.token = token
                self.connection = websocket
                return websocket
            except BaseException as err:
                if websocket is not None:
                    await self._async_close_unpublished(websocket)
                self.connection = None
                if isinstance(
                    err,
                    (
                        asyncio.CancelledError,
                        TimeoutError,
                        RemotePairingRequired,
                        UnauthorizedError,
                        ConnectionFailure,
                    ),
                ):
                    raise
                raise ConnectionFailure("Remote connection failed") from None

    async def close(self) -> None:
        """Gracefully close while retaining ownership on any close failure."""
        async with self._lifecycle_lock:
            websocket = self.connection
            if websocket is None:
                return
            async with asyncio.timeout(REMOTE_CLOSE_DEADLINE):
                await websocket.close()
            self.connection = None

    async def async_stop(self) -> None:
        """Permanently stop, force-aborting any socket that cannot close."""
        self._stopped = True
        try:
            async with asyncio.timeout(REMOTE_CLOSE_DEADLINE):
                async with self._lifecycle_lock:
                    websocket = self.connection
                    if websocket is None:
                        return
                    await websocket.close()
                    self.connection = None
        except asyncio.CancelledError:
            websocket = self.connection
            if websocket is not None:
                self._force_abort(websocket)
                self.connection = None
            raise
        except Exception:
            websocket = self.connection
            if websocket is not None:
                self._force_abort(websocket)
                self.connection = None
