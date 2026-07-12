"""Native async transport for the Samsung Frame Art websocket."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import contextlib
from dataclasses import dataclass
import json
from typing import Any
import uuid

from samsungtvws import helper
from samsungtvws.art.art import ART_ENDPOINT, ArtChannelEmitCommand
from samsungtvws.async_connection import SamsungTVWSAsyncConnection
from samsungtvws.event import (
    D2D_SERVICE_MESSAGE_EVENT,
    IGNORE_EVENTS_AT_STARTUP,
    MS_CHANNEL_CLIENT_CONNECT_EVENT,
    MS_CHANNEL_CLIENT_DISCONNECT_EVENT,
    MS_CHANNEL_CONNECT_EVENT,
    MS_CHANNEL_READY_EVENT,
    MS_CHANNEL_UNAUTHORIZED,
)
from samsungtvws.exceptions import (
    ConnectionFailure,
    ResponseError,
    UnauthorizedError,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.protocol import State

from .const import (
    ART_CLOSE_DEADLINE,
    ART_CONNECT_DEADLINE,
    ART_REQUEST_DEADLINE,
    CLIENT_NAME,
    DOMAIN,
    LOGGER,
    PORT_WS,
)

type ArtEventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
type TaskFactory = Callable[
    [Coroutine[Any, Any, None], str], asyncio.Task[None]
]


@dataclass(slots=True)
class _PendingResponse:
    """A response waiter registered with the receiver."""

    future: asyncio.Future[dict[str, Any]]
    expected_sub_event: str | None


_HANDSHAKE_BROADCASTS = {
    *IGNORE_EVENTS_AT_STARTUP,
    MS_CHANNEL_CLIENT_CONNECT_EVENT,
    MS_CHANNEL_CLIENT_DISCONNECT_EVENT,
}


class FrameArt(SamsungTVWSAsyncConnection):
    """Own the Frame Art websocket and its single receiver task."""

    def __init__(
        self,
        host,
        *,
        token,
        ssl_context,
        task_factory,
        event_callback,
        port=PORT_WS,
        name=CLIENT_NAME,
        timeout=ART_CONNECT_DEADLINE,
    ):
        super().__init__(
            host,
            endpoint=ART_ENDPOINT,
            token=token,
            port=port,
            name=name,
            timeout=timeout,
        )
        self._ssl_context = ssl_context
        self._task_factory: TaskFactory | None = task_factory
        self._event_callback = event_callback
        self._operation_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._pending: dict[str, _PendingResponse] = {}
        self._uuidless_pending: _PendingResponse | None = None
        self._closing = False

    async def open(self) -> ClientConnection:
        """Open the websocket and complete its bounded two-stage handshake."""
        async with self._lifecycle_lock:
            if self.connection is not None:
                return self.connection

            websocket = None
            try:
                async with asyncio.timeout(ART_CONNECT_DEADLINE):
                    kwargs = (
                        {"ssl": self._ssl_context}
                        if self._is_ssl_connection()
                        else {}
                    )
                    websocket = await connect(
                        self._format_websocket_url(self.endpoint),
                        open_timeout=ART_CONNECT_DEADLINE,
                        **kwargs,
                    )
                    await self._wait_for_handshake(websocket)
                self.connection = websocket
                return websocket
            except BaseException:
                if websocket is not None:
                    with contextlib.suppress(Exception, TimeoutError):
                        async with asyncio.timeout(ART_CLOSE_DEADLINE):
                            await websocket.close()
                self.connection = None
                raise

    async def _wait_for_handshake(self, websocket: ClientConnection) -> None:
        """Wait for channel connect followed by Art channel readiness."""
        connected = False
        while True:
            frame = helper.process_api_response(await websocket.recv())
            event = frame.get("event", "*")
            self._websocket_event(event, frame)

            if event == MS_CHANNEL_UNAUTHORIZED:
                raise UnauthorizedError(frame)

            if not connected:
                if event in _HANDSHAKE_BROADCASTS:
                    continue
                if event != MS_CHANNEL_CONNECT_EVENT:
                    raise ConnectionFailure(frame)
                self._check_for_token(frame)
                connected = True
                continue

            if event == MS_CHANNEL_READY_EVENT:
                return
            if event not in _HANDSHAKE_BROADCASTS:
                raise ConnectionFailure(frame)

    async def start_listening(self) -> None:
        """Open and start the sole HA-owned websocket receiver."""
        if self._task_factory is None:
            raise RuntimeError("A task factory is required to start the receiver")
        await self.open()
        if self._recv_loop is not None and not self._recv_loop.done():
            return
        self._closing = False
        self._recv_loop = self._task_factory(
            self._receive_loop(), f"{DOMAIN}-art-receiver"
        )

    def set_event_callback(self, callback: ArtEventCallback | None) -> None:
        """Replace the callback receiving unsolicited Art events."""
        self._event_callback = callback

    async def request(
        self,
        request: str,
        *,
        expected_sub_event=None,
        request_id=None,
        **params,
    ) -> dict[str, Any]:
        """Send one serialized Art request and await its correlated response."""
        async with self._operation_lock:
            await self.start_listening()
            return await self._request_unlocked(
                request,
                expected_sub_event=expected_sub_event,
                request_id=request_id,
                **params,
            )

    async def _request_unlocked(
        self,
        request: str,
        *,
        expected_sub_event: str | None,
        request_id: str | None,
        **params: Any,
    ) -> dict[str, Any]:
        """Send a request while the operation lock is held."""
        correlation_id = request_id or str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        pending = _PendingResponse(future, expected_sub_event)
        self._pending[correlation_id] = pending
        if expected_sub_event is not None:
            self._uuidless_pending = pending

        payload = {
            "request": request,
            **params,
            "id": correlation_id,
            "request_id": correlation_id,
        }
        try:
            connection = self.connection
            if connection is None:
                raise ConnectionFailure("Art connection closed")
            command = ArtChannelEmitCommand.art_app_request(payload)
            await self._send_command(connection, command, 0)
            try:
                async with asyncio.timeout(ART_REQUEST_DEADLINE):
                    response = await future
            except TimeoutError:
                await self.close()
                raise

            if response.get("event") == "error":
                request_name = "unknown_request"
                try:
                    request_data = response.get("request_data", "{}")
                    if isinstance(request_data, str):
                        request_data = json.loads(request_data)
                    if isinstance(request_data, dict):
                        request_name = request_data.get("request", request_name)
                except json.JSONDecodeError:
                    pass
                raise ResponseError(
                    f"`{request_name}` request failed with error number "
                    f"{response.get('error_code', 'unknown')}"
                )
            return response
        finally:
            if self._pending.get(correlation_id) is pending:
                self._pending.pop(correlation_id)
            if self._uuidless_pending is pending:
                self._uuidless_pending = None

    async def _receive_loop(self) -> None:
        """Receive and dispatch each websocket frame exactly once."""
        connection = self.connection
        try:
            while connection is not None:
                frame = helper.process_api_response(await connection.recv())
                event = frame.get("event", "*")
                self._websocket_event(event, frame)
                await self._dispatch_frame(event, frame)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.debug("Art receiver exited: %s", err)
        finally:
            await self._receiver_finished(connection)

    async def _dispatch_frame(self, event: str, frame: dict[str, Any]) -> None:
        """Route a D2D response or send an unsolicited payload to the callback."""
        if event != D2D_SERVICE_MESSAGE_EVENT:
            return
        raw_payload = frame.get("data")
        if not isinstance(raw_payload, str):
            return
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict):
            return

        sub_event = payload.get("event")
        message_id = payload.get("request_id", payload.get("id"))
        pending = (
            self._pending.get(message_id) if isinstance(message_id, str) else None
        )
        if pending is not None and (
            sub_event == "error"
            or pending.expected_sub_event is None
            or sub_event == pending.expected_sub_event
        ):
            if not pending.future.done():
                pending.future.set_result(payload)
            return

        uuidless = self._uuidless_pending if message_id is None else None
        if uuidless is not None and (
            sub_event == "error" or sub_event == uuidless.expected_sub_event
        ):
            if not uuidless.future.done():
                uuidless.future.set_result(payload)
            return

        callback = self._event_callback
        if callback is not None:
            awaitable = callback(event, payload)
            if awaitable is not None:
                await awaitable

    async def _receiver_finished(self, connection: ClientConnection | None) -> None:
        """Clean up only the connection captured by this receiver."""
        if connection is not None:
            with contextlib.suppress(Exception, TimeoutError):
                async with asyncio.timeout(ART_CLOSE_DEADLINE):
                    await connection.close()
        if self.connection is connection:
            self.connection = None
        self._fail_pending(ConnectionFailure("Art connection closed"))
        if self._recv_loop is asyncio.current_task():
            self._recv_loop = None

    def _fail_pending(self, error: Exception) -> None:
        """Fail and clear every response waiter."""
        waiters = [pending.future for pending in self._pending.values()]
        if self._uuidless_pending is not None:
            waiters.append(self._uuidless_pending.future)
        self._pending.clear()
        self._uuidless_pending = None
        for future in waiters:
            if not future.done():
                future.set_exception(error)

    async def close(self) -> None:
        """Cancel the receiver and close the websocket within a deadline."""
        async with self._lifecycle_lock:
            self._closing = True
            receiver = self._recv_loop
            self._recv_loop = None
            if receiver is not None and receiver is not asyncio.current_task():
                receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    async with asyncio.timeout(ART_CLOSE_DEADLINE):
                        await receiver
            connection, self.connection = self.connection, None
            if connection is not None:
                with contextlib.suppress(Exception, TimeoutError):
                    async with asyncio.timeout(ART_CLOSE_DEADLINE):
                        await connection.close()
            self._fail_pending(ConnectionFailure("Art connection closed"))

    def is_alive(self) -> bool:
        """Return whether both the websocket and receiver are active."""
        return (
            self.connection is not None
            and self.connection.state is not State.CLOSED
            and self._recv_loop is not None
            and not self._recv_loop.done()
        )
