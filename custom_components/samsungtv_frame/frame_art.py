"""Native async transport for the Samsung Frame Art websocket."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import contextlib
from dataclasses import dataclass
from datetime import datetime
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
    ART_D2D_DEADLINE,
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


class ArtHostUnavailable(ConnectionFailure):
    """The channel connected but explicitly listed no internal Art host."""


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


def _explicitly_missing_art_host(frame: dict[str, Any]) -> bool:
    data = frame.get("data")
    if not isinstance(data, dict):
        return False
    clients = data.get("clients")
    if not isinstance(clients, list) or not clients:
        return False
    explicit_roles = [
        client["isHost"]
        for client in clients
        if isinstance(client, dict)
        and isinstance(client.get("isHost"), bool)
    ]
    return bool(explicit_roles) and not any(explicit_roles)


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
        self._transfer_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._stopped = False

    async def open(self) -> ClientConnection:
        """Open the websocket and complete its bounded two-stage handshake."""
        async with self._lifecycle_lock:
            self._raise_if_stopped()
            if self.connection is not None:
                return self.connection

            websocket = None
            try:
                deadline = self.timeout or ART_CONNECT_DEADLINE
                async with asyncio.timeout(deadline):
                    kwargs = (
                        {"ssl": self._ssl_context}
                        if self._is_ssl_connection()
                        else {}
                    )
                    websocket = await connect(
                        self._format_websocket_url(self.endpoint),
                        open_timeout=deadline,
                        **kwargs,
                    )
                    await self._wait_for_handshake(websocket)
                    self._raise_if_stopped()
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
                if _explicitly_missing_art_host(frame):
                    raise ArtHostUnavailable(
                        "Art channel connected without an internal host"
                    )
                connected = True
                continue

            if event == MS_CHANNEL_READY_EVENT:
                return
            if event not in _HANDSHAKE_BROADCASTS:
                raise ConnectionFailure(frame)

    async def start_listening(self) -> None:
        """Open and start the sole HA-owned websocket receiver."""
        self._raise_if_stopped()
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

    def stop(self) -> None:
        """Permanently prevent this adapter from opening another connection."""
        self._stopped = True

    def _raise_if_stopped(self) -> None:
        """Reject work that crossed the permanent shutdown boundary."""
        if self._stopped:
            raise ConnectionFailure("Art connection is stopped")

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
            self._raise_if_stopped()
            if not self.is_alive():
                raise ConnectionFailure("Art session is not ready")
            return await self._request_unlocked(
                request,
                expected_sub_event=expected_sub_event,
                request_id=request_id,
                **params,
            )

    @staticmethod
    def _on_off(value: bool | str) -> str:
        """Normalize a boolean or on/off string for Art commands."""
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, str) and value.lower() in {"on", "off"}:
            return value.lower()
        raise ValueError("Expected bool or 'on'/'off' string")

    async def _get_value(
        self, request: str, key: str = "value", **params: Any
    ) -> Any:
        """Return a response field when the Art response is a mapping."""
        payload = await self.request(request, **params)
        return payload.get(key) if isinstance(payload, dict) else payload

    async def get_artmode(self) -> Any:
        """Return the current Art Mode state."""
        return await self._get_value("get_artmode_status")

    async def set_artmode(self, value: bool | str) -> dict[str, Any]:
        """Set the Art Mode state."""
        return await self.request(
            "set_artmode_status", value=self._on_off(value)
        )

    async def get_current(self) -> dict[str, Any]:
        """Return the current artwork payload."""
        return await self.request("get_current_artwork")

    async def get_artmode_settings(
        self, setting: str = ""
    ) -> dict[str, Any]:
        """Return all Art Mode settings or one nested setting entry."""
        payload = await self.request("get_artmode_settings")
        nested = payload.get("data")
        if isinstance(nested, str):
            nested_data = json.loads(nested)
            for item in nested_data:
                if item.get("item") == setting:
                    return item
        return payload

    async def get_brightness(self) -> Any:
        """Return the Art Mode brightness level."""
        try:
            payload = await self.get_artmode_settings("brightness")
            return payload.get("value")
        except (ResponseError, json.JSONDecodeError):
            return await self._get_value("get_brightness")

    async def set_brightness(self, value: Any) -> dict[str, Any]:
        """Set the Art Mode brightness level."""
        return await self.request("set_brightness", value=value)

    async def get_color_temperature(self) -> Any:
        """Return the Art Mode color temperature."""
        try:
            payload = await self.get_artmode_settings("color_temperature")
            return payload.get("value")
        except (ResponseError, json.JSONDecodeError):
            return await self._get_value("get_color_temperature")

    async def set_color_temperature(self, value: Any) -> dict[str, Any]:
        """Set the Art Mode color temperature."""
        return await self.request("set_color_temperature", value=value)

    async def select_image(
        self,
        content_id: str,
        category: str | None = None,
        show: bool = True,
    ) -> dict[str, Any]:
        """Select an artwork and optionally show it immediately."""
        return await self.request(
            "select_image",
            category_id=category,
            content_id=content_id,
            show=show,
        )

    async def delete(self, content_id: str) -> bool:
        """Delete one artwork and validate the returned content id."""
        content_id_list = [{"content_id": content_id}]
        payload = await self.request(
            "delete_image_list", content_id_list=content_id_list
        )
        if not isinstance(payload, dict):
            return False
        returned = payload.get("content_id_list")
        if not returned:
            return False
        if isinstance(returned, str):
            try:
                returned = json.loads(returned)
            except json.JSONDecodeError:
                return False
        return isinstance(returned, list) and returned == content_id_list

    async def change_matte(
        self,
        content_id: str,
        matte_id: str | None = None,
        portrait_matte: str | None = None,
    ) -> dict[str, Any]:
        """Change an artwork's landscape and optional portrait matte."""
        params = {
            "content_id": content_id,
            "matte_id": matte_id or "none",
        }
        if portrait_matte:
            params["portrait_matte_id"] = portrait_matte
        return await self.request("change_matte", **params)

    async def set_photo_filter(
        self, content_id: str, filter_id: str
    ) -> dict[str, Any]:
        """Set the photo filter for an artwork."""
        return await self.request(
            "set_photo_filter", content_id=content_id, filter_id=filter_id
        )

    async def set_favourite(
        self, content_id: str, status: bool | str = "on"
    ) -> dict[str, Any]:
        """Set the favourite status for an artwork."""
        return await self.request(
            "change_favorite",
            expected_sub_event="favorite_changed",
            content_id=content_id,
            status=self._on_off(status),
        )

    async def set_slideshow(
        self, duration: int, shuffle: bool, category_id: str
    ) -> dict[str, Any]:
        """Configure slideshow playback, with the legacy command fallback."""
        params = {
            "value": str(duration) if duration > 0 else "off",
            "category_id": category_id,
            "type": "shuffleslideshow" if shuffle else "slideshow",
        }
        try:
            return await self.request("set_auto_rotation_status", **params)
        except ResponseError:
            return await self.request("set_slideshow_status", **params)

    async def _open_d2d(
        self, conn_info: dict[str, Any]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a deadline-bounded D2D stream, optionally secured."""
        kwargs = {}
        if conn_info.get("secured"):
            kwargs["ssl"] = self._ssl_context
        async with asyncio.timeout(ART_D2D_DEADLINE):
            return await asyncio.open_connection(
                conn_info["ip"], int(conn_info["port"]), **kwargs
            )

    async def _read_d2d_file(
        self, reader: asyncio.StreamReader
    ) -> tuple[dict[str, Any], bytes]:
        """Read one complete deadline-bounded D2D file frame."""
        async with asyncio.timeout(ART_D2D_DEADLINE):
            header_size = int.from_bytes(await reader.readexactly(4), "big")
            header = json.loads(await reader.readexactly(header_size))
            body = await reader.readexactly(int(header["fileLength"]))
        return header, body

    async def _close_d2d_writer(self, writer: asyncio.StreamWriter) -> None:
        """Close a D2D writer without allowing cleanup to mask transfer errors."""
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception, TimeoutError):
            async with asyncio.timeout(ART_CLOSE_DEADLINE):
                await writer.wait_closed()

    def _track_transfer(self) -> asyncio.Task[Any] | None:
        """Register the current transfer task before its first await."""
        if self._closing:
            raise ConnectionFailure("Art connection is closing")
        task = asyncio.current_task()
        if task is not None:
            self._transfer_tasks.add(task)
        return task

    def _untrack_transfer(self, task: asyncio.Task[Any] | None) -> None:
        """Remove a completed or cancelled transfer task."""
        if task is not None:
            self._transfer_tasks.discard(task)

    @staticmethod
    def _consume_or_cancel_future(future: asyncio.Future[Any]) -> None:
        """Finish an abandoned waiter without an unretrieved exception."""
        if not future.done():
            future.cancel()
        elif not future.cancelled():
            future.exception()

    async def get_thumbnail(self, content_id: str) -> bytes | None:
        """Download one thumbnail over a short-lived D2D stream."""
        transfer = self._track_transfer()
        try:
            d2d_id = str(uuid.uuid4())
            try:
                payload = await self.request(
                    "get_thumbnail_list",
                    request_id=d2d_id,
                    content_id_list=[{"content_id": content_id}],
                    conn_info={
                        "d2d_mode": "socket",
                        "connection_id": helper.generate_connection_id(),
                        "id": d2d_id,
                    },
                )
            except ResponseError:
                return None

            conn_info = payload.get("conn_info", {})
            if isinstance(conn_info, str):
                conn_info = json.loads(conn_info)
            reader, writer = await self._open_d2d(conn_info)
            try:
                result = None
                total = 1
                current = -1
                while current + 1 < total:
                    header, result = await self._read_d2d_file(reader)
                    current = int(header["num"])
                    total = int(header["total"])
                return result
            finally:
                await self._close_d2d_writer(writer)
        finally:
            self._untrack_transfer(transfer)

    async def upload(self, data: bytes, file_type: str, matte: str) -> str:
        """Upload image bytes as one serialized, non-retried transaction."""
        transfer = self._track_transfer()
        try:
            async with self._operation_lock:
                self._raise_if_stopped()
                if not self.is_alive():
                    raise ConnectionFailure("Art session is not ready")
                try:
                    try:
                        version = await self._request_unlocked(
                            "api_version",
                            expected_sub_event=None,
                            request_id=None,
                        )
                    except ResponseError:
                        version = None
                    if (
                        isinstance(version, dict)
                        and version.get("version") == "0.97"
                    ):
                        return await self._upload_ws_binary_unlocked(
                            data, file_type, matte
                        )
                    return await self._upload_d2d_unlocked(
                        data, file_type, matte
                    )
                except TimeoutError:
                    await self.close()
                    raise
        finally:
            self._untrack_transfer(transfer)

    async def _upload_d2d_unlocked(
        self, data: bytes, file_type: str, matte: str
    ) -> str:
        """Upload through the TV's short-lived D2D stream."""
        upload_id = str(uuid.uuid4())
        normalized_type = file_type.lower()
        if normalized_type == "jpeg":
            normalized_type = "jpg"
        ready = await self._request_unlocked(
            "send_image",
            expected_sub_event="ready_to_use",
            request_id=upload_id,
            file_type=normalized_type,
            file_size=len(data),
            image_date=datetime.now().strftime("%Y:%m:%d %H:%M:%S"),
            matte_id=matte or "none",
            portrait_matte_id=matte or "none",
            conn_info={
                "d2d_mode": "socket",
                "connection_id": helper.generate_connection_id(),
                "id": upload_id,
            },
        )

        completion = _PendingResponse(
            asyncio.get_running_loop().create_future(),
            "image_added",
        )
        self._pending[upload_id] = completion
        self._uuidless_pending = completion
        try:
            conn_info = ready.get("conn_info", {})
            if isinstance(conn_info, str):
                conn_info = json.loads(conn_info)
            header = json.dumps(
                {
                    "num": 0,
                    "total": 1,
                    "fileLength": len(data),
                    "fileName": "image",
                    "fileType": normalized_type,
                    "secKey": conn_info["key"],
                    "version": "0.0.1",
                }
            ).encode("ascii")
            reader, writer = await self._open_d2d(conn_info)
            del reader
            try:
                async with asyncio.timeout(ART_D2D_DEADLINE):
                    writer.write(len(header).to_bytes(4, "big"))
                    writer.write(header)
                    writer.write(data)
                    await writer.drain()
            finally:
                await self._close_d2d_writer(writer)

            async with asyncio.timeout(ART_REQUEST_DEADLINE):
                response = await completion.future
            if response.get("event") == "error":
                raise ResponseError("`send_image` request failed")
            return str(response["content_id"])
        finally:
            if self._pending.get(upload_id) is completion:
                self._pending.pop(upload_id)
            if self._uuidless_pending is completion:
                self._uuidless_pending = None
            self._consume_or_cancel_future(completion.future)

    async def _upload_ws_binary_unlocked(
        self, data: bytes, file_type: str, matte: str
    ) -> str:
        """Upload through the Art API 0.97 websocket binary format."""
        upload_id = str(uuid.uuid4())
        normalized_type = file_type.lower()
        header_type = (
            "JPEG"
            if normalized_type in {"jpg", "jpeg"}
            else normalized_type.upper()
        )
        inner = {
            "request": "send_image",
            "file_type": header_type,
            "matte_id": matte or "none",
            "id": upload_id,
        }
        outer = {
            "method": "ms.channel.emit",
            "params": {
                "data": json.dumps(inner),
                "to": "host",
                "event": "art_app_request",
            },
        }
        header = json.dumps(outer, separators=(",", ":")).encode("utf-8")
        if len(header) > 0xFFFF:
            raise ValueError("Upload header too large")
        binary_payload = len(header).to_bytes(2, "big") + header + data

        completion = _PendingResponse(
            asyncio.get_running_loop().create_future(), "image_added"
        )
        self._pending[upload_id] = completion
        try:
            connection = self.connection
            if connection is None:
                raise ConnectionFailure("Art connection closed")
            async with asyncio.timeout(ART_REQUEST_DEADLINE):
                await connection.send(binary_payload)
                response = await completion.future
            if response.get("event") == "error":
                raise ResponseError("`send_image` request failed")
            return str(response["content_id"])
        finally:
            if self._pending.get(upload_id) is completion:
                self._pending.pop(upload_id)
            self._consume_or_cancel_future(completion.future)

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
            try:
                async with asyncio.timeout(ART_REQUEST_DEADLINE):
                    await self._send_command(connection, command, 0)
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
            self._consume_or_cancel_future(future)

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
        message_id = payload.get("request_id")
        if not isinstance(message_id, str) or not message_id:
            message_id = payload.get("id")
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
            try:
                awaitable = callback(event, payload)
                if awaitable is not None:
                    await awaitable
            except Exception as err:
                LOGGER.warning("Art event callback failed: %s", err)

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
        """Cancel owned tasks and close the websocket within a deadline."""
        async with self._lifecycle_lock:
            self._closing = True
            try:
                current = asyncio.current_task()
                transfers = [
                    task
                    for task in self._transfer_tasks
                    if task is not current and not task.done()
                ]
                for task in transfers:
                    task.cancel()
                if transfers:
                    with contextlib.suppress(
                        asyncio.CancelledError, TimeoutError
                    ):
                        async with asyncio.timeout(ART_CLOSE_DEADLINE):
                            await asyncio.gather(
                                *transfers, return_exceptions=True
                            )
                    self._transfer_tasks.difference_update(transfers)

                receiver = self._recv_loop
                self._recv_loop = None
                if receiver is not None and receiver is not current:
                    receiver.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, TimeoutError
                    ):
                        async with asyncio.timeout(ART_CLOSE_DEADLINE):
                            await receiver
                connection, self.connection = self.connection, None
                if connection is not None:
                    with contextlib.suppress(Exception, TimeoutError):
                        async with asyncio.timeout(ART_CLOSE_DEADLINE):
                            await connection.close()
                self._fail_pending(ConnectionFailure("Art connection closed"))
            finally:
                self._closing = False

    def is_alive(self) -> bool:
        """Return whether both the websocket and receiver are active."""
        return (
            self.connection is not None
            and self.connection.state is not State.CLOSED
            and self._recv_loop is not None
            and not self._recv_loop.done()
        )
