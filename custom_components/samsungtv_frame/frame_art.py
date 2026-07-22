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
from samsungtvws.command import SamsungTVCommand, SamsungTVSleepCommand
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

from .art_settings import MOTION_SENSITIVITIES, MOTION_TIMERS
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
from .websocket_privacy import (
    QUIET_WEBSOCKET_LOGGER,
    process_api_response_silently,
)

type ArtEventCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]
type TaskFactory = Callable[
    [Coroutine[Any, Any, None], str], asyncio.Task[None]
]


class ArtHostUnavailable(ConnectionFailure):
    """The channel connected but explicitly listed no internal Art host."""


class ArtCleanupPending(ConnectionFailure):
    """A prior Art generation still owns live cleanup work."""


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
        self._receiver_connection: ClientConnection | None = None
        self._closing = False
        self._stopped = False

    @staticmethod
    def _force_abort(websocket: ClientConnection) -> None:
        """Abort a websocket whose graceful close could not finish."""
        transport = getattr(websocket, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            with contextlib.suppress(Exception):
                abort()

    @classmethod
    async def _async_close_websocket(
        cls, websocket: ClientConnection
    ) -> None:
        """Bound, abort, and fully drain one physical websocket close."""
        close_task = asyncio.create_task(
            websocket.close(), name=f"{DOMAIN}-art-socket-close"
        )
        cancellation: asyncio.CancelledError | None = None
        aborted = False

        def abort_once() -> None:
            nonlocal aborted
            if not aborted:
                cls._force_abort(websocket)
                aborted = True

        try:
            done, _pending = await asyncio.wait(
                {close_task}, timeout=ART_CLOSE_DEADLINE
            )
        except asyncio.CancelledError as err:
            cancellation = err
            done = set()

        if close_task in done:
            try:
                close_task.result()
            except asyncio.CancelledError:
                abort_once()
            except BaseException:
                abort_once()
        else:
            abort_once()
            if not close_task.done():
                close_task.cancel()
            while not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as err:
                    if not close_task.done():
                        cancellation = err
                        abort_once()
                        close_task.cancel()
                except BaseException:
                    break
            if close_task.done() and not close_task.cancelled():
                with contextlib.suppress(BaseException):
                    close_task.result()

        if cancellation is not None:
            raise cancellation

    @staticmethod
    async def _async_drain_tasks(
        tasks: set[asyncio.Task[Any]],
        cancellation: asyncio.CancelledError | None,
    ) -> tuple[set[asyncio.Task[Any]], asyncio.CancelledError | None]:
        """Bound child-task drain while remembering only caller cancellation."""
        pending = {task for task in tasks if not task.done()}
        deadline = asyncio.get_running_loop().time() + ART_CLOSE_DEADLINE
        while pending:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                _done, pending = await asyncio.wait(
                    pending, timeout=remaining
                )
            except asyncio.CancelledError as err:
                cancellation = err
        return pending, cancellation

    async def open(self) -> ClientConnection:
        """Open the websocket and complete its bounded two-stage handshake."""
        async with self._lifecycle_lock:
            self._raise_if_stopped()
            if not self.token:
                raise UnauthorizedError("Art authorization required")
            if self.connection is not None:
                return self.connection
            if self.has_live_children():
                raise ArtCleanupPending("Art cleanup pending")

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
                        logger=QUIET_WEBSOCKET_LOGGER,
                        **kwargs,
                    )
                    await self._wait_for_handshake(websocket)
                    self._raise_if_stopped()
                self.connection = websocket
                return websocket
            except BaseException as err:
                if websocket is not None:
                    await self._async_close_websocket(websocket)
                self.connection = None
                if isinstance(
                    err,
                    (
                        asyncio.CancelledError,
                        TimeoutError,
                        ArtHostUnavailable,
                        UnauthorizedError,
                        ConnectionFailure,
                    ),
                ):
                    raise
                raise ConnectionFailure("Art connection failed") from None

    async def _wait_for_handshake(
        self, websocket: ClientConnection
    ) -> None:
        """Wait for channel connect followed by Art channel readiness."""
        connected = False
        while True:
            frame = process_api_response_silently(await websocket.recv())
            event = frame.get("event", "*")

            if event == MS_CHANNEL_UNAUTHORIZED:
                raise UnauthorizedError("Art authorization rejected")

            if not connected:
                if event in _HANDSHAKE_BROADCASTS:
                    continue
                if event != MS_CHANNEL_CONNECT_EVENT:
                    raise ConnectionFailure("Art handshake failed")
                if _explicitly_missing_art_host(frame):
                    raise ArtHostUnavailable(
                        "Art channel connected without an internal host"
                    )
                connected = True
                continue

            if event == MS_CHANNEL_READY_EVENT:
                return
            if event not in _HANDSHAKE_BROADCASTS:
                raise ConnectionFailure("Art handshake failed")

    async def start_listening(self) -> None:
        """Open and start the sole HA-owned websocket receiver."""
        self._raise_if_stopped()
        if self._task_factory is None:
            raise RuntimeError("A task factory is required to start the receiver")
        connection = await self.open()
        receiver = self._recv_loop
        if receiver is not None and not receiver.done():
            if self._receiver_connection is connection:
                return
            raise ArtCleanupPending("Art cleanup pending")
        self._closing = False
        coroutine = self._receive_loop(connection)
        try:
            receiver = self._task_factory(
                coroutine, f"{DOMAIN}-art-receiver"
            )
        except BaseException:
            coroutine.close()
            raise
        self._recv_loop = receiver
        self._receiver_connection = connection

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

    def has_live_children(self) -> bool:
        """Return whether a receiver or transfer still owns Art work."""
        receiver = self._recv_loop
        return (
            receiver is not None and not receiver.done()
        ) or any(not task.done() for task in self._transfer_tasks)

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

    async def get_art_settings_payload(self) -> dict[str, Any]:
        """Return the raw aggregate Art Mode settings payload."""
        return await self.request("get_artmode_settings")

    async def get_auto_rotation_status(self) -> dict[str, Any]:
        """Return the raw modern slideshow status payload."""
        return await self.request("get_auto_rotation_status")

    async def get_legacy_slideshow_status(self) -> dict[str, Any]:
        """Return the raw legacy slideshow status payload."""
        return await self.request("get_slideshow_status")

    async def get_legacy_brightness(self) -> Any:
        """Return the correlated legacy Art Mode brightness value."""
        return (await self.request("get_brightness")).get("value")

    async def get_legacy_color_temperature(self) -> Any:
        """Return the correlated legacy color-temperature value."""
        return (await self.request("get_color_temperature")).get("value")

    async def set_brightness(self, value: Any) -> dict[str, Any]:
        """Set the Art Mode brightness level."""
        return await self.request("set_brightness", value=value)

    async def set_color_temperature(self, value: Any) -> dict[str, Any]:
        """Set the Art Mode color temperature."""
        return await self.request("set_color_temperature", value=value)

    async def set_motion_timer(self, value: str) -> dict[str, Any]:
        """Set the motion timer to one supported wire value."""
        if value not in MOTION_TIMERS:
            raise ValueError("Invalid motion timer")
        return await self.request("set_motion_timer", value=value)

    async def set_motion_sensitivity(self, value: str) -> dict[str, Any]:
        """Set motion sensitivity to one supported wire value."""
        if value not in MOTION_SENSITIVITIES:
            raise ValueError("Invalid motion sensitivity")
        return await self.request("set_motion_sensitivity", value=value)

    async def set_brightness_sensor_setting(
        self, enabled: bool
    ) -> dict[str, Any]:
        """Enable or disable automatic Art Mode brightness."""
        if not isinstance(enabled, bool):
            raise ValueError("Brightness sensor state must be boolean")
        return await self.request(
            "set_brightness_sensor_setting",
            value="on" if enabled else "off",
        )

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

    async def _receive_loop(
        self, connection: ClientConnection | None = None
    ) -> None:
        """Receive and dispatch each websocket frame exactly once."""
        if connection is None:
            connection = self.connection
        try:
            while connection is not None:
                frame = process_api_response_silently(
                    await connection.recv()
                )
                event = frame.get("event", "*")
                await self._dispatch_frame(event, frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.debug("Art receiver exited")
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
            except Exception:
                LOGGER.warning("Art event callback failed")

    async def _receiver_finished(self, connection: ClientConnection | None) -> None:
        """Clean up only the connection captured by this receiver."""
        if self.connection is connection:
            self.connection = None
        try:
            if connection is not None:
                await self._async_close_websocket(connection)
        finally:
            self._fail_pending(ConnectionFailure("Art connection closed"))
            if self._recv_loop is asyncio.current_task():
                self._recv_loop = None
                self._receiver_connection = None

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
            cancellation: asyncio.CancelledError | None = None
            receiver_error: BaseException | None = None
            tasks_alive = False
            try:
                current = asyncio.current_task()
                completed_transfers = {
                    task for task in self._transfer_tasks if task.done()
                }
                for task in completed_transfers:
                    if not task.cancelled():
                        task.exception()
                self._transfer_tasks.difference_update(completed_transfers)
                transfers = [
                    task
                    for task in self._transfer_tasks
                    if task is not current and not task.done()
                ]
                for task in transfers:
                    task.cancel()
                if transfers:
                    pending_transfers, cancellation = await self._async_drain_tasks(
                        set(transfers), cancellation
                    )
                    completed_transfers = {
                        task for task in transfers if task.done()
                    }
                    for task in completed_transfers:
                        if not task.cancelled():
                            task.exception()
                    self._transfer_tasks.difference_update(
                        completed_transfers
                    )
                    tasks_alive = any(
                        not task.done() for task in pending_transfers
                    )

                receiver = self._recv_loop
                if receiver is not None and receiver is not current:
                    if not receiver.done():
                        receiver.cancel()
                        pending_receivers, cancellation = (
                            await self._async_drain_tasks(
                                {receiver}, cancellation
                            )
                        )
                        tasks_alive = tasks_alive or any(
                            not task.done() for task in pending_receivers
                        )
                    if receiver.done() and not receiver.cancelled():
                        receiver_error = receiver.exception()
                    if receiver.done() and self._recv_loop is receiver:
                        self._recv_loop = None
                        self._receiver_connection = None
                elif receiver is current:
                    self._recv_loop = None
                    self._receiver_connection = None
                connection, self.connection = self.connection, None
                try:
                    if connection is not None:
                        try:
                            await self._async_close_websocket(connection)
                        except asyncio.CancelledError as err:
                            cancellation = err
                finally:
                    self._fail_pending(
                        ConnectionFailure("Art connection closed")
                    )
                if cancellation is not None:
                    raise cancellation
                if tasks_alive:
                    raise ConnectionFailure("Art tasks did not stop")
                if receiver_error is not None:
                    raise receiver_error
            finally:
                self._closing = False

    def is_alive(self) -> bool:
        """Return whether both the websocket and receiver are active."""
        return (
            self.connection is not None
            and self.connection.state is not State.CLOSED
            and self._recv_loop is not None
            and not self._recv_loop.done()
            and self._receiver_connection is self.connection
        )
