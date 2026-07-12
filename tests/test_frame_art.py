"""Tests for the native async Frame Art transport."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from samsungtvws.exceptions import ConnectionFailure, ResponseError, UnauthorizedError
from websockets.protocol import State

from custom_components.samsungtv_frame.frame_art import FrameArt


class FakeWebSocket:
    """Controllable websocket for transport tests."""

    def __init__(self, frames, *, on_send=None):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(json.dumps(frame))
        self.sent = []
        self.closed = False
        self.state = State.OPEN
        self.on_send = on_send

    async def recv(self):
        frame = await self.frames.get()
        if isinstance(frame, BaseException):
            raise frame
        return frame

    async def send(self, payload):
        self.sent.append(payload)
        if self.on_send is not None:
            self.on_send(payload)

    async def close(self):
        self.closed = True
        self.state = State.CLOSED


class FakeWriter:
    """Record stream writes and expose complete close semantics."""

    def __init__(self, *, on_write=None, drain_error=None):
        self.data = bytearray()
        self.closed = False
        self.waited_closed = False
        self.on_write = on_write
        self.drain_error = drain_error

    def write(self, data):
        self.data.extend(data)
        if self.on_write is not None:
            self.on_write(data)

    async def drain(self):
        if self.drain_error is not None:
            raise self.drain_error

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited_closed = True


class BlockingWriter(FakeWriter):
    """A complete writer whose drain can be cancelled while blocked."""

    def __init__(self):
        super().__init__()
        self.drain_started = asyncio.Event()
        self.drain_release = asyncio.Event()

    async def drain(self):
        self.drain_started.set()
        await self.drain_release.wait()


def d2d_file(name="thumb", body=b"jpeg", num=0, total=1):
    """Build one complete file frame from the TV's D2D stream."""
    header = json.dumps(
        {
            "fileLength": len(body),
            "fileID": name,
            "fileType": "jpg",
            "num": num,
            "total": total,
        }
    ).encode()
    return len(header).to_bytes(4, "big") + header + body


def stream_reader(data=b"", *, eof=True):
    """Return a StreamReader fed with real stream-shaped bytes."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def task_factory(coroutine, name):
    """Create an asyncio task using the HA-compatible factory contract."""
    return asyncio.create_task(coroutine, name=name)


def make_art(*, callback=None):
    """Create a transport under test."""
    return FrameArt(
        "1.2.3.4",
        token="tok",
        ssl_context=MagicMock(),
        task_factory=task_factory,
        event_callback=callback,
    )


async def test_artmode_commands_normalize_values_and_reject_invalid_input():
    art = make_art()
    art.request = AsyncMock(return_value={"value": "on"})

    assert await art.get_artmode() == "on"
    art.request.assert_awaited_once_with("get_artmode_status")

    for value, expected in (
        (True, "on"),
        (False, "off"),
        ("ON", "on"),
        ("off", "off"),
    ):
        art.request.reset_mock()
        await art.set_artmode(value)
        art.request.assert_awaited_once_with(
            "set_artmode_status", value=expected
        )

    with pytest.raises(ValueError, match="Expected bool or 'on'/'off' string"):
        await art.set_artmode(1)


async def test_get_current_returns_artwork_payload():
    art = make_art()
    payload = {"content_id": "MY_F0001"}
    art.request = AsyncMock(return_value=payload)

    assert await art.get_current() is payload
    art.request.assert_awaited_once_with("get_current_artwork")


async def test_get_artmode_settings_extracts_nested_setting():
    art = make_art()
    payload = {
        "data": json.dumps(
            [
                {"item": "brightness", "value": 7},
                {"item": "color_temperature", "value": 3},
            ]
        )
    }
    art.request = AsyncMock(return_value=payload)

    assert await art.get_artmode_settings("brightness") == {
        "item": "brightness",
        "value": 7,
    }
    art.request.assert_awaited_once_with("get_artmode_settings")


async def test_get_artmode_settings_propagates_nested_json_error():
    art = make_art()
    payload = {"data": "not-json"}
    art.request = AsyncMock(return_value=payload)

    with pytest.raises(json.JSONDecodeError):
        await art.get_artmode_settings("brightness")


@pytest.mark.parametrize(
    ("getter", "setting", "value"),
    [
        ("get_brightness", "brightness", 8),
        ("get_color_temperature", "color_temperature", 4),
    ],
)
async def test_numeric_getters_use_artmode_settings(getter, setting, value):
    art = make_art()
    art.request = AsyncMock(
        return_value={"data": json.dumps([{"item": setting, "value": value}])}
    )

    assert await getattr(art, getter)() == value
    art.request.assert_awaited_once_with("get_artmode_settings")


@pytest.mark.parametrize(
    ("getter", "request_name", "value"),
    [
        ("get_brightness", "get_brightness", 5),
        ("get_color_temperature", "get_color_temperature", 2),
    ],
)
async def test_numeric_getters_fall_back_after_response_error(
    getter, request_name, value
):
    art = make_art()
    art.request = AsyncMock(
        side_effect=[ResponseError("unsupported"), {"value": value}]
    )

    assert await getattr(art, getter)() == value
    assert art.request.await_args_list == [
        (("get_artmode_settings",), {}),
        ((request_name,), {}),
    ]


@pytest.mark.parametrize(
    ("setter", "request_name"),
    [
        ("set_brightness", "set_brightness"),
        ("set_color_temperature", "set_color_temperature"),
    ],
)
async def test_numeric_setters_send_value(setter, request_name):
    art = make_art()
    art.request = AsyncMock(return_value={"event": "changed"})

    assert await getattr(art, setter)(6) == {"event": "changed"}
    art.request.assert_awaited_once_with(request_name, value=6)


async def test_select_image_sends_exact_payload():
    art = make_art()
    art.request = AsyncMock(return_value={"event": "selected"})

    await art.select_image("MY_F0001", show=True)
    art.request.assert_awaited_once_with(
        "select_image", category_id=None, content_id="MY_F0001", show=True
    )


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"content_id_list": [{"content_id": "MY_F0001"}]}, True),
        ({"content_id_list": '[{"content_id": "MY_F0001"}]'}, True),
        ({"content_id_list": [{"content_id": "OTHER"}]}, False),
        ({"content_id_list": "not-json"}, False),
        ({}, False),
        ("not-a-payload", False),
    ],
)
async def test_delete_validates_returned_content_id(response, expected):
    art = make_art()
    art.request = AsyncMock(return_value=response)

    assert await art.delete("MY_F0001") is expected
    art.request.assert_awaited_once_with(
        "delete_image_list",
        content_id_list=[{"content_id": "MY_F0001"}],
    )


async def test_matte_filter_and_favourite_send_exact_payloads():
    art = make_art()
    art.request = AsyncMock(return_value={"event": "changed"})

    await art.change_matte("MY_F0001", None, "portrait_shadowbox")
    art.request.assert_awaited_with(
        "change_matte",
        content_id="MY_F0001",
        matte_id="none",
        portrait_matte_id="portrait_shadowbox",
    )

    await art.set_photo_filter("MY_F0001", "ink")
    art.request.assert_awaited_with(
        "set_photo_filter", content_id="MY_F0001", filter_id="ink"
    )

    await art.set_favourite("MY_F0001", False)
    art.request.assert_awaited_with(
        "change_favorite",
        expected_sub_event="favorite_changed",
        content_id="MY_F0001",
        status="off",
    )


async def test_favourite_rejects_invalid_status():
    art = make_art()
    art.request = AsyncMock()

    with pytest.raises(ValueError, match="Expected bool or 'on'/'off' string"):
        await art.set_favourite("MY_F0001", "yes")
    art.request.assert_not_awaited()


async def test_slideshow_uses_auto_rotation_request():
    art = make_art()
    art.request = AsyncMock(return_value={"event": "changed"})

    assert await art.set_slideshow(15, True, "MY-C0004") == {
        "event": "changed"
    }
    art.request.assert_awaited_once_with(
        "set_auto_rotation_status",
        value="15",
        category_id="MY-C0004",
        type="shuffleslideshow",
    )


async def test_slideshow_falls_back_only_after_response_error():
    art = make_art()
    art.request = AsyncMock(
        side_effect=[ResponseError("unsupported"), {"event": "changed"}]
    )

    assert await art.set_slideshow(0, False, "MY-C0002") == {
        "event": "changed"
    }
    params = {
        "value": "off",
        "category_id": "MY-C0002",
        "type": "slideshow",
    }
    assert art.request.await_args_list == [
        (("set_auto_rotation_status",), params),
        (("set_slideshow_status",), params),
    ]


async def test_slideshow_does_not_mask_transport_errors():
    art = make_art()
    art.request = AsyncMock(side_effect=ConnectionFailure("lost"))

    with pytest.raises(ConnectionFailure, match="lost"):
        await art.set_slideshow(15, False, "MY-C0002")
    art.request.assert_awaited_once()


async def test_thumbnail_downloads_d2d_file_with_secured_stream():
    art = make_art()
    reader = stream_reader(d2d_file(name="MY_F0001", body=b"jpeg-bytes"))
    writer = FakeWriter()
    art.request = AsyncMock(
        return_value={
            "conn_info": json.dumps(
                {
                    "ip": "10.0.0.8",
                    "port": "4321",
                    "secured": True,
                }
            )
        }
    )
    open_connection = AsyncMock(return_value=(reader, writer))

    with patch(
        "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
        open_connection,
    ):
        assert await art.get_thumbnail("MY_F0001") == b"jpeg-bytes"

    request = art.request.await_args
    assert request.args == ("get_thumbnail_list",)
    assert request.kwargs["content_id_list"] == [{"content_id": "MY_F0001"}]
    conn_info = request.kwargs["conn_info"]
    assert conn_info["d2d_mode"] == "socket"
    assert conn_info["id"] == request.kwargs["request_id"]
    assert isinstance(conn_info["connection_id"], int)
    open_connection.assert_awaited_once_with(
        "10.0.0.8", 4321, ssl=art._ssl_context
    )
    assert writer.closed
    assert writer.waited_closed


async def test_thumbnail_drm_response_returns_none_without_opening_stream():
    art = make_art()
    art.request = AsyncMock(side_effect=ResponseError("DRM refused"))
    open_connection = AsyncMock()

    with patch(
        "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
        open_connection,
    ):
        assert await art.get_thumbnail("SAM-F0001") is None

    open_connection.assert_not_awaited()


@pytest.mark.parametrize(
    "stream",
    [
        b"\x00\x00\x00\x08short",
        d2d_file(body=b"jpeg")[:-1],
    ],
    ids=["truncated-header", "truncated-body"],
)
async def test_thumbnail_truncated_stream_propagates_and_closes_writer(stream):
    art = make_art()
    writer = FakeWriter()
    art.request = AsyncMock(
        return_value={"conn_info": {"ip": "10.0.0.8", "port": 4321}}
    )

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(stream), writer)),
        ),
        pytest.raises(asyncio.IncompleteReadError),
    ):
        await art.get_thumbnail("MY_F0001")

    assert writer.closed
    assert writer.waited_closed


async def test_thumbnail_read_timeout_propagates_and_closes_writer():
    art = make_art()
    writer = FakeWriter()
    art.request = AsyncMock(
        return_value={"conn_info": {"ip": "10.0.0.8", "port": 4321}}
    )

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(eof=False), writer)),
        ),
        patch("custom_components.samsungtv_frame.frame_art.ART_D2D_DEADLINE", 0.01),
        pytest.raises(TimeoutError),
    ):
        await art.get_thumbnail("MY_F0001")

    assert writer.closed
    assert writer.waited_closed


async def test_thumbnail_cancellation_propagates_and_closes_writer():
    art = make_art()
    writer = FakeWriter()
    art.request = AsyncMock(
        return_value={"conn_info": {"ip": "10.0.0.8", "port": 4321}}
    )

    with patch(
        "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
        AsyncMock(return_value=(stream_reader(eof=False), writer)),
    ):
        thumbnail = asyncio.create_task(art.get_thumbnail("MY_F0001"))
        await asyncio.sleep(0)
        thumbnail.cancel()
        with pytest.raises(asyncio.CancelledError):
            await thumbnail

    assert writer.closed
    assert writer.waited_closed


async def test_upload_d2d_correlates_ready_and_pre_registers_completion():
    image = b"jpeg-image"
    completion_sent = False
    art = make_art()
    ws = FakeWebSocket(handshake_frames())

    def on_write(_data):
        nonlocal completion_sent
        pending = art._uuidless_pending
        assert pending is not None
        assert pending.expected_sub_event == "image_added"
        if not completion_sent:
            completion_sent = True
            ws.frames.put_nowait(
                art_response(event="image_added", content_id="MY_F0099")
            )

    writer = FakeWriter(on_write=on_write)
    open_connection = AsyncMock(
        return_value=(stream_reader(), writer)
    )
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            open_connection,
        ),
    ):
        upload = asyncio.create_task(art.upload(image, "jpeg", "none"))
        await wait_for_sent(ws, 1)
        version_request = sent_art_request(ws, 0)
        await ws.frames.put(
            art_response(
                request_id=version_request["request_id"], version="4.3"
            )
        )

        await wait_for_sent(ws, 2)
        send_image = sent_art_request(ws, 1)
        upload_id = send_image["request_id"]
        assert send_image["id"] == upload_id
        assert send_image["request"] == "send_image"
        assert send_image["file_type"] == "jpg"
        assert send_image["file_size"] == len(image)
        assert send_image["matte_id"] == "none"
        assert send_image["portrait_matte_id"] == "none"
        assert send_image["conn_info"]["id"] == upload_id

        await ws.frames.put(
            art_response(
                event="ready_to_use",
                request_id="different-upload",
                conn_info={"ip": "wrong", "port": 1, "key": "wrong"},
            )
        )
        await asyncio.sleep(0)
        open_connection.assert_not_awaited()

        await ws.frames.put(
            art_response(
                event="ready_to_use",
                request_id=upload_id,
                conn_info=json.dumps(
                    {"ip": "10.0.0.8", "port": "4321", "key": "secret"}
                ),
            )
        )
        assert await upload == "MY_F0099"

    open_connection.assert_awaited_once_with("10.0.0.8", 4321)
    header_len = int.from_bytes(writer.data[:4], "big")
    header = json.loads(bytes(writer.data[4 : 4 + header_len]))
    assert header == {
        "num": 0,
        "total": 1,
        "fileLength": len(image),
        "fileName": "image",
        "fileType": "jpg",
        "secKey": "secret",
        "version": "0.0.1",
    }
    assert bytes(writer.data[4 + header_len :]) == image
    assert writer.closed
    assert writer.waited_closed
    assert art._uuidless_pending is None
    await art.close()


async def test_upload_d2d_does_not_retry_failed_drain_and_closes_writer():
    art = make_art()
    art.connection = FakeWebSocket([])
    writer = FakeWriter(drain_error=OSError("partial upload"))
    request = AsyncMock(
        side_effect=[
            {"version": "4.3"},
            {
                "event": "ready_to_use",
                "conn_info": {
                    "ip": "10.0.0.8",
                    "port": 4321,
                    "key": "secret",
                },
            },
        ]
    )
    open_connection = AsyncMock(
        return_value=(stream_reader(), writer)
    )

    with (
        patch.object(art, "start_listening", AsyncMock()),
        patch.object(art, "_request_unlocked", request),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            open_connection,
        ),
        pytest.raises(OSError, match="partial upload"),
    ):
        await art.upload(b"image", "jpg", "none")

    assert request.await_count == 2
    open_connection.assert_awaited_once()
    assert writer.closed
    assert writer.waited_closed
    assert art._uuidless_pending is None


async def test_upload_cancellation_cleans_completion_and_writer():
    art = make_art()
    art.connection = FakeWebSocket([])
    writer = BlockingWriter()
    request = AsyncMock(
        side_effect=[
            {"version": "4.3"},
            {
                "event": "ready_to_use",
                "conn_info": {
                    "ip": "10.0.0.8",
                    "port": 4321,
                    "key": "secret",
                },
            },
        ]
    )

    with (
        patch.object(art, "start_listening", AsyncMock()),
        patch.object(art, "_request_unlocked", request),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(), writer)),
        ),
    ):
        upload = asyncio.create_task(art.upload(b"image", "jpg", "none"))
        await writer.drain_started.wait()
        assert art._uuidless_pending is not None
        upload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await upload

    assert writer.closed
    assert writer.waited_closed
    assert art._uuidless_pending is None


async def test_upload_completion_timeout_closes_websocket_and_cleans_pending():
    art = make_art()
    ws = FakeWebSocket([])
    art.connection = ws
    writer = FakeWriter()
    request = AsyncMock(
        side_effect=[
            {"version": "4.3"},
            {
                "event": "ready_to_use",
                "conn_info": {
                    "ip": "10.0.0.8",
                    "port": 4321,
                    "key": "secret",
                },
            },
        ]
    )

    with (
        patch.object(art, "start_listening", AsyncMock()),
        patch.object(art, "_request_unlocked", request),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(), writer)),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_REQUEST_DEADLINE",
            0.01,
        ),
        pytest.raises(TimeoutError),
    ):
        await art.upload(b"image", "jpg", "none")

    assert ws.closed
    assert art.connection is None
    assert writer.closed
    assert writer.waited_closed
    assert art._uuidless_pending is None


async def test_upload_api_097_sends_exact_binary_frame_and_correlates_result():
    image = b"jpeg-image"
    art = make_art()
    binary_payload = None
    ws = None

    def on_send(payload):
        nonlocal binary_payload
        if not isinstance(payload, bytes):
            return
        binary_payload = payload
        header_len = int.from_bytes(payload[:2], "big")
        outer = json.loads(payload[2 : 2 + header_len])
        inner = json.loads(outer["params"]["data"])
        upload_id = inner["id"]
        pending = art._pending.get(upload_id)
        assert pending is not None
        assert pending.expected_sub_event == "image_added"
        assert ws is not None
        ws.frames.put_nowait(
            art_response(
                event="image_added",
                request_id=upload_id,
                content_id="MY_F0100",
            )
        )

    ws = FakeWebSocket(handshake_frames(), on_send=on_send)
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        upload = asyncio.create_task(art.upload(image, "jpg", "none"))
        await wait_for_sent(ws, 1)
        version_request = sent_art_request(ws, 0)
        await ws.frames.put(
            art_response(
                request_id=version_request["request_id"], version="0.97"
            )
        )
        assert await upload == "MY_F0100"

    assert binary_payload is not None
    header_len = int.from_bytes(binary_payload[:2], "big")
    header = binary_payload[2 : 2 + header_len]
    outer = json.loads(header)
    inner = json.loads(outer["params"]["data"])
    expected_inner = {
        "request": "send_image",
        "file_type": "JPEG",
        "matte_id": "none",
        "id": inner["id"],
    }
    expected_outer = {
        "method": "ms.channel.emit",
        "params": {
            "data": json.dumps(expected_inner),
            "to": "host",
            "event": "art_app_request",
        },
    }
    assert header == json.dumps(
        expected_outer, separators=(",", ":")
    ).encode()
    assert binary_payload[2 + header_len :] == image
    assert not art._pending
    await art.close()


async def test_open_ignores_broadcasts_captures_token_and_waits_for_ready():
    ws = FakeWebSocket(
        [
            {"event": "ms.channel.clientConnect"},
            {"event": "ms.channel.connect", "data": {"token": "fresh"}},
            {"event": "ms.channel.clientDisconnect"},
            {"event": "ms.channel.ready"},
        ]
    )
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        assert await art.open() is ws
    assert art.token == "fresh"
    assert not ws.closed


@pytest.mark.parametrize("event", ["ms.channel.unauthorized", "unexpected"])
async def test_open_closes_local_socket_on_failed_handshake(event):
    ws = FakeWebSocket([{"event": event}])
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        with pytest.raises((UnauthorizedError, ConnectionFailure)):
            await art.open()
    assert ws.closed
    assert art.connection is None


async def test_open_deadline_bounds_endless_broadcast_stream():
    ws = FakeWebSocket([{"event": "ms.channel.clientConnect"}] * 20)
    art = make_art()
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        patch("custom_components.samsungtv_frame.frame_art.ART_CONNECT_DEADLINE", 0.01),
        pytest.raises(TimeoutError),
    ):
        await art.open()
    assert ws.closed
    assert art.connection is None


async def test_open_is_idempotent():
    ws = FakeWebSocket(
        [{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}]
    )
    art = make_art()
    connect_mock = AsyncMock(return_value=ws)
    with patch("custom_components.samsungtv_frame.frame_art.connect", connect_mock):
        assert await art.open() is ws
        assert await art.open() is ws
    connect_mock.assert_awaited_once()


async def test_start_listening_is_idempotent():
    ws = FakeWebSocket(
        [{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}]
    )
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
        receiver = art._recv_loop
        await art.start_listening()
        assert art._recv_loop is receiver
        await art.close()


async def test_is_alive_becomes_false_when_receiver_exits():
    ws = FakeWebSocket(
        [
            {"event": "ms.channel.connect"},
            {"event": "ms.channel.ready"},
            "not a websocket frame",
        ]
    )
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
        receiver = art._recv_loop
        assert receiver is not None
        await receiver
    assert not art.is_alive()
    assert ws.closed


async def test_receiver_decodes_push_payload_for_callback():
    callback = AsyncMock()
    ws = FakeWebSocket(
        [
            {"event": "ms.channel.connect"},
            {"event": "ms.channel.ready"},
            {
                "event": "d2d_service_message",
                "data": json.dumps({"event": "art_mode_changed", "value": "on"}),
            },
        ]
    )
    art = make_art(callback=callback)
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
        await asyncio.sleep(0)
        callback.assert_awaited_once_with(
            "d2d_service_message", {"event": "art_mode_changed", "value": "on"}
        )
        await art.close()


def handshake_frames():
    """Return a successful Art websocket handshake."""
    return [{"event": "ms.channel.connect"}, {"event": "ms.channel.ready"}]


async def wait_for_sent(ws, count):
    """Wait until the fake websocket records the expected send count."""
    for _ in range(20):
        if len(ws.sent) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} websocket sends, got {len(ws.sent)}")


def sent_art_request(ws, index):
    """Decode one JSON Art request recorded by the websocket."""
    return json.loads(json.loads(ws.sent[index])["params"]["data"])


def art_response(**payload):
    """Build a complete D2D websocket response frame."""
    return json.dumps(
        {"event": "d2d_service_message", "data": json.dumps(payload)}
    )


async def test_request_correlates_response_by_uuid():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        request_task = asyncio.create_task(art.request("get_artmode_status"))
        await asyncio.sleep(0)
        outer = json.loads(ws.sent[-1])
        inner = json.loads(outer["params"]["data"])
        request_id = inner["request_id"]
        assert inner["id"] == request_id
        await ws.frames.put(
            json.dumps(
                {
                    "event": "d2d_service_message",
                    "data": json.dumps(
                        {"request_id": request_id, "value": "on"}
                    ),
                }
            )
        )
        assert await request_task == {"request_id": request_id, "value": "on"}
        await art.close()


async def test_request_correlates_uuidless_expected_sub_event():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        request_task = asyncio.create_task(
            art.request(
                "get_artmode_status", expected_sub_event="artmode_status"
            )
        )
        await asyncio.sleep(0)
        await ws.frames.put(
            json.dumps(
                {
                    "event": "d2d_service_message",
                    "data": json.dumps(
                        {"event": "artmode_status", "value": "on"}
                    ),
                }
            )
        )
        assert await request_task == {"event": "artmode_status", "value": "on"}
        await art.close()


async def test_request_translates_error_payload():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        request_task = asyncio.create_task(art.request("get_artmode_status"))
        await asyncio.sleep(0)
        inner = json.loads(json.loads(ws.sent[-1])["params"]["data"])
        await ws.frames.put(
            json.dumps(
                {
                    "event": "d2d_service_message",
                    "data": json.dumps(
                        {
                            "event": "error",
                            "request_id": inner["request_id"],
                            "request_data": json.dumps(
                                {"request": "get_artmode_status"}
                            ),
                            "error_code": 42,
                        }
                    ),
                }
            )
        )
        with pytest.raises(
            ResponseError,
            match="`get_artmode_status` request failed with error number 42",
        ):
            await request_task
        await art.close()


async def test_push_and_request_response_coexist():
    callback = AsyncMock()
    ws = FakeWebSocket(handshake_frames())
    art = make_art(callback=callback)
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        request_task = asyncio.create_task(art.request("get_artmode_status"))
        await asyncio.sleep(0)
        inner = json.loads(json.loads(ws.sent[-1])["params"]["data"])
        await ws.frames.put(
            json.dumps(
                {
                    "event": "d2d_service_message",
                    "data": json.dumps(
                        {"event": "art_mode_changed", "value": "on"}
                    ),
                }
            )
        )
        await asyncio.sleep(0)
        callback.assert_awaited_once_with(
            "d2d_service_message", {"event": "art_mode_changed", "value": "on"}
        )
        assert not request_task.done()
        await ws.frames.put(
            json.dumps(
                {
                    "event": "d2d_service_message",
                    "data": json.dumps(
                        {"request_id": inner["request_id"], "value": "on"}
                    ),
                }
            )
        )
        assert (await request_task)["value"] == "on"
        await art.close()


async def test_receiver_disconnect_fails_pending_request():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        request_task = asyncio.create_task(art.request("get_artmode_status"))
        await asyncio.sleep(0)
        await ws.frames.put(ConnectionFailure("lost"))
        with pytest.raises(ConnectionFailure, match="Art connection closed"):
            await request_task
    assert art.connection is None
    assert not art._pending


async def test_request_cancellation_removes_pending_registration():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        request_task = asyncio.create_task(
            art.request(
                "get_artmode_status", expected_sub_event="artmode_status"
            )
        )
        await asyncio.sleep(0)
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task
        assert not art._pending
        assert art._uuidless_pending is None
        await art.close()


async def test_request_connection_loss_before_send_removes_registration():
    art = make_art()
    with patch.object(art, "start_listening", AsyncMock()):
        with pytest.raises(ConnectionFailure, match="Art connection closed"):
            await art.request(
                "get_artmode_status", expected_sub_event="artmode_status"
            )
    assert not art._pending
    assert art._uuidless_pending is None


async def test_request_timeout_closes_transport():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        patch("custom_components.samsungtv_frame.frame_art.ART_REQUEST_DEADLINE", 0.01),
        pytest.raises(TimeoutError),
    ):
        await art.request("get_artmode_status")
    assert ws.closed
    assert art.connection is None
    assert not art._pending


async def test_close_is_idempotent():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
        await art.close()
        await art.close()
    assert ws.closed
    assert art.connection is None
    assert art._recv_loop is None


async def test_close_serializes_with_in_progress_open():
    ws = FakeWebSocket([])
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        open_task = asyncio.create_task(art.open())
        await asyncio.sleep(0)
        close_task = asyncio.create_task(art.close())
        await asyncio.sleep(0)

        await ws.frames.put(json.dumps({"event": "ms.channel.connect"}))
        await ws.frames.put(json.dumps({"event": "ms.channel.ready"}))
        opened, _ = await asyncio.gather(open_task, close_task)

    assert opened is ws
    assert ws.closed
    assert art.connection is None
