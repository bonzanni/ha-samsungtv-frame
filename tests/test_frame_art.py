"""Tests for the native async Frame Art transport."""
from __future__ import annotations

import asyncio
import gc
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from samsungtvws.art.art import ArtChannelEmitCommand
from samsungtvws.command import SamsungTVSleepCommand
from samsungtvws.exceptions import ConnectionFailure, ResponseError, UnauthorizedError
from websockets.protocol import State

from custom_components.samsungtv_frame.device import FrameDevice
from custom_components.samsungtv_frame.frame_art import (
    ArtHostUnavailable,
    FrameArt,
)
from custom_components.samsungtv_frame.websocket_privacy import (
    QUIET_WEBSOCKET_LOGGER,
)


class FakeWebSocket:
    """Controllable websocket for transport tests."""

    def __init__(self, frames, *, on_send=None, recv_delay=0):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(json.dumps(frame))
        self.sent = []
        self.closed = False
        self.state = State.OPEN
        self.on_send = on_send
        self.recv_delay = recv_delay
        self.transport = MagicMock()

    async def recv(self):
        if self.recv_delay:
            await asyncio.sleep(self.recv_delay)
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

    def __init__(
        self,
        *,
        on_write=None,
        drain_error=None,
        wait_closed_delay=None,
        wait_closed_error=None,
    ):
        self.data = bytearray()
        self.closed = False
        self.waited_closed = False
        self.on_write = on_write
        self.drain_error = drain_error
        self.wait_closed_delay = wait_closed_delay
        self.wait_closed_error = wait_closed_error

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
        if self.wait_closed_delay is not None:
            await asyncio.sleep(self.wait_closed_delay)
        if self.wait_closed_error is not None:
            raise self.wait_closed_error


class BlockingWriter(FakeWriter):
    """A complete writer whose drain can be cancelled while blocked."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
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


def make_art(*, callback=None, token="tok", **kwargs):
    """Create a transport under test."""
    return FrameArt(
        "1.2.3.4",
        token=token,
        ssl_context=MagicMock(),
        task_factory=task_factory,
        event_callback=callback,
        **kwargs,
    )


@pytest.mark.parametrize("token", [None, ""], ids=["none", "empty"])
async def test_open_without_token_refuses_before_network(token):
    art = make_art(token=token)
    connect_mock = AsyncMock(
        side_effect=AssertionError("network should not be called")
    )

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            connect_mock,
        ),
        pytest.raises(
            UnauthorizedError, match="^Art authorization required$"
        ),
    ):
        await art.open()

    connect_mock.assert_not_awaited()
    assert art.connection is None
    assert art._recv_loop is None
    assert art._receiver_connection is None
    assert not art._transfer_tasks


async def test_request_does_not_open_when_receiver_is_not_ready():
    art = make_art()
    start = AsyncMock()

    with patch.object(art, "start_listening", start):
        with pytest.raises(
            ConnectionFailure, match="^Art session is not ready$"
        ):
            await art.request("get_artmode_status")

    start.assert_not_awaited()


async def test_upload_does_not_open_when_receiver_is_not_ready():
    art = make_art()
    start = AsyncMock()
    request = AsyncMock()
    d2d_upload = AsyncMock()
    binary_upload = AsyncMock()

    with (
        patch.object(art, "start_listening", start),
        patch.object(art, "_request_unlocked", request),
        patch.object(art, "_upload_d2d_unlocked", d2d_upload),
        patch.object(art, "_upload_ws_binary_unlocked", binary_upload),
        pytest.raises(
            ConnectionFailure, match="^Art session is not ready$"
        ),
    ):
        await art.upload(b"image", "jpg", "none")

    start.assert_not_awaited()
    request.assert_not_awaited()
    d2d_upload.assert_not_awaited()
    binary_upload.assert_not_awaited()


@pytest.mark.parametrize("operation_name", ["request", "upload"])
async def test_art_operation_rechecks_readiness_inside_operation_lock(
    operation_name,
):
    art = make_art()
    ready = True
    art.is_alive = MagicMock(side_effect=lambda: ready)
    start = AsyncMock()
    request = AsyncMock(return_value={"version": "4.3"})
    d2d_upload = AsyncMock(return_value="MY_F0001")

    await art._operation_lock.acquire()
    with (
        patch.object(art, "start_listening", start),
        patch.object(art, "_request_unlocked", request),
        patch.object(art, "_upload_d2d_unlocked", d2d_upload),
    ):
        if operation_name == "request":
            operation = asyncio.create_task(
                art.request("get_artmode_status")
            )
        else:
            operation = asyncio.create_task(
                art.upload(b"image", "jpg", "none")
            )
        await asyncio.sleep(0)
        assert not operation.done()

        ready = False
        art._operation_lock.release()
        with pytest.raises(
            ConnectionFailure, match="^Art session is not ready$"
        ):
            await operation

    start.assert_not_awaited()
    request.assert_not_awaited()
    d2d_upload.assert_not_awaited()


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


async def test_get_artmode_settings_skips_malformed_members_before_match():
    art = make_art()
    payload = {
        "data": json.dumps(
            [
                None,
                4,
                "invalid",
                ["item", "brightness"],
                {"item": "brightness", "value": 7},
            ]
        )
    }
    art.request = AsyncMock(return_value=payload)

    assert await art.get_artmode_settings("brightness") == {
        "item": "brightness",
        "value": 7,
    }


@pytest.mark.parametrize(
    "nested_data",
    [None, 4, "invalid", {"item": "brightness", "value": 7}],
)
async def test_get_artmode_settings_non_list_returns_payload(nested_data):
    art = make_art()
    payload = {"data": json.dumps(nested_data)}
    art.request = AsyncMock(return_value=payload)

    assert await art.get_artmode_settings("brightness") is payload


async def test_numeric_device_getter_ignores_malformed_nested_settings(hass):
    device = FrameDevice(
        hass,
        host="1.2.3.4",
        mac="A0:D0:5B:86:CE:B7",
        token="tok",
        ssl_context=MagicMock(),
        task_factory=task_factory,
    )
    session = MagicMock(ready=True)
    session.async_connection_failed = AsyncMock()
    device._art_session = session
    device._art.request = AsyncMock(
        return_value={
            "data": json.dumps(
                [None, "invalid", {"item": "brightness", "value": 7}]
            )
        }
    )

    assert await device.async_get_art_brightness() == 7
    session.async_connection_failed.assert_not_awaited()


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


async def test_close_cancels_blocked_thumbnail_and_cleans_writer():
    art = make_art()
    writer = FakeWriter()
    art.request = AsyncMock(
        return_value={"conn_info": {"ip": "10.0.0.8", "port": 4321}}
    )
    open_connection = AsyncMock(
        return_value=(stream_reader(eof=False), writer)
    )
    thumbnail = None

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            open_connection,
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
            0.01,
        ),
    ):
        thumbnail = asyncio.create_task(art.get_thumbnail("MY_F0001"))
        while not open_connection.await_count:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(art.close(), timeout=0.05)
            elapsed = asyncio.get_running_loop().time() - started
            assert thumbnail.done()
            with pytest.raises(asyncio.CancelledError):
                await thumbnail
        finally:
            if not thumbnail.done():
                thumbnail.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await thumbnail

    assert elapsed < 0.05
    assert writer.closed
    assert writer.waited_closed
    assert not art._transfer_tasks


async def test_thumbnail_writer_close_deadline_bounds_blocking_wait():
    art = make_art()
    writer = FakeWriter(wait_closed_delay=0.1)
    art.request = AsyncMock(
        return_value={"conn_info": {"ip": "10.0.0.8", "port": 4321}}
    )
    loop = asyncio.get_running_loop()

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(
                return_value=(stream_reader(d2d_file(body=b"jpeg")), writer)
            ),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
            0.005,
        ),
    ):
        started = loop.time()
        assert await art.get_thumbnail("MY_F0001") == b"jpeg"
        elapsed = loop.time() - started

    assert elapsed < 0.05
    assert writer.closed
    assert writer.waited_closed


async def test_thumbnail_writer_close_failure_preserves_read_error():
    art = make_art()
    writer = FakeWriter(wait_closed_error=OSError("close failed"))
    art.request = AsyncMock(
        return_value={"conn_info": {"ip": "10.0.0.8", "port": 4321}}
    )

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(
                return_value=(stream_reader(d2d_file(body=b"jpeg")[:-1]), writer)
            ),
        ),
        pytest.raises(asyncio.IncompleteReadError),
    ):
        await art.get_thumbnail("MY_F0001")

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
        await art.start_listening()
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


@pytest.mark.parametrize("correlation_key", ["request_id", "id"])
async def test_upload_d2d_completion_accepts_echoed_upload_id(
    correlation_key,
):
    art = make_art()
    ws = FakeWebSocket(handshake_frames())
    completion_sent = False

    def on_write(_data):
        nonlocal completion_sent
        if completion_sent:
            return
        completion_sent = True
        upload_id = sent_art_request(ws, 1)["request_id"]
        ws.frames.put_nowait(
            art_response(
                event="image_added",
                content_id="MY_F0101",
                **{correlation_key: upload_id},
            )
        )

    writer = FakeWriter(on_write=on_write)
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(), writer)),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_REQUEST_DEADLINE",
            0.01,
        ),
    ):
        await art.start_listening()
        upload = asyncio.create_task(art.upload(b"image", "jpg", "none"))
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
        await ws.frames.put(
            art_response(
                event="ready_to_use",
                request_id=upload_id,
                conn_info={
                    "ip": "10.0.0.8",
                    "port": 4321,
                    "key": "secret",
                },
            )
        )

        assert await upload == "MY_F0101"

    assert writer.closed
    assert writer.waited_closed
    assert art._uuidless_pending is None
    await art.close()


@pytest.mark.parametrize(
    ("wrong_event", "wrong_fields"),
    [
        ("image_added", {"content_id": "OTHER_CLIENT_IMAGE"}),
        ("error", {"error_code": 99}),
    ],
)
async def test_upload_d2d_completion_rejects_other_client_id(
    wrong_event, wrong_fields
):
    callback = AsyncMock()
    art = make_art(callback=callback)
    ws = FakeWebSocket(handshake_frames())
    d2d_write_started = asyncio.Event()
    writer = FakeWriter(on_write=lambda _data: d2d_write_started.set())
    upload = None

    try:
        with (
            patch(
                "custom_components.samsungtv_frame.frame_art.connect",
                AsyncMock(return_value=ws),
            ),
            patch(
                "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
                AsyncMock(return_value=(stream_reader(), writer)),
            ),
        ):
            await art.start_listening()
            upload = asyncio.create_task(
                art.upload(b"image", "jpg", "none")
            )
            await wait_for_sent(ws, 1)
            version_request = sent_art_request(ws, 0)
            await ws.frames.put(
                art_response(
                    request_id=version_request["request_id"],
                    version="4.3",
                )
            )

            await wait_for_sent(ws, 2)
            upload_id = sent_art_request(ws, 1)["request_id"]
            await ws.frames.put(
                art_response(
                    event="ready_to_use",
                    request_id=upload_id,
                    conn_info={
                        "ip": "10.0.0.8",
                        "port": 4321,
                        "key": "secret",
                    },
                )
            )
            await d2d_write_started.wait()

            wrong_payload = {
                "event": wrong_event,
                "request_id": "other-client-upload",
                **wrong_fields,
            }
            await ws.frames.put(art_response(**wrong_payload))
            for _ in range(20):
                if callback.await_count or upload.done():
                    break
                await asyncio.sleep(0)

            assert not upload.done()
            callback.assert_awaited_once_with(
                "d2d_service_message", wrong_payload
            )

            await ws.frames.put(
                art_response(
                    event="image_added",
                    request_id=upload_id,
                    content_id="MY_F0102",
                )
            )
            assert await upload == "MY_F0102"
    finally:
        if upload is not None:
            if not upload.done():
                upload.cancel()
            try:
                await upload
            except BaseException:
                pass
        await art.close()

    assert writer.closed
    assert writer.waited_closed
    assert art._uuidless_pending is None


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
        patch.object(art, "is_alive", return_value=True),
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
        patch.object(art, "is_alive", return_value=True),
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


async def test_close_cancels_blocked_upload_and_cleans_writer():
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
        patch.object(art, "is_alive", return_value=True),
        patch.object(art, "_request_unlocked", request),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(), writer)),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
            0.01,
        ),
    ):
        upload = asyncio.create_task(art.upload(b"image", "jpg", "none"))
        await writer.drain_started.wait()
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(art.close(), timeout=0.05)
            elapsed = asyncio.get_running_loop().time() - started
            assert upload.done()
            with pytest.raises(asyncio.CancelledError):
                await upload
        finally:
            writer.drain_release.set()
            if not upload.done():
                try:
                    await upload
                except (Exception, asyncio.CancelledError):
                    pass

    assert elapsed < 0.05
    assert writer.closed
    assert writer.waited_closed
    assert not art._transfer_tasks


async def test_upload_writer_close_failure_preserves_drain_error():
    art = make_art()
    art.connection = FakeWebSocket([])
    writer = FakeWriter(
        drain_error=OSError("drain failed"),
        wait_closed_error=RuntimeError("close failed"),
    )
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
        patch.object(art, "is_alive", return_value=True),
        patch.object(art, "_request_unlocked", request),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(), writer)),
        ),
        pytest.raises(OSError, match="drain failed"),
    ):
        await art.upload(b"image", "jpg", "none")

    assert writer.closed
    assert writer.waited_closed
    assert art._uuidless_pending is None


async def test_upload_writer_close_deadline_preserves_cancellation():
    art = make_art()
    art.connection = FakeWebSocket([])
    writer = BlockingWriter(wait_closed_delay=0.1)
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
    loop = asyncio.get_running_loop()

    with (
        patch.object(art, "start_listening", AsyncMock()),
        patch.object(art, "is_alive", return_value=True),
        patch.object(art, "_request_unlocked", request),
        patch(
            "custom_components.samsungtv_frame.frame_art.asyncio.open_connection",
            AsyncMock(return_value=(stream_reader(), writer)),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
            0.005,
        ),
    ):
        upload = asyncio.create_task(art.upload(b"image", "jpg", "none"))
        await writer.drain_started.wait()
        started = loop.time()
        upload.cancel()
        with pytest.raises(asyncio.CancelledError):
            await upload
        elapsed = loop.time() - started

    assert elapsed < 0.05
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
        patch.object(art, "is_alive", return_value=True),
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
        await art.start_listening()
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


async def test_open_ignores_broadcasts_preserves_token_and_waits_for_ready():
    ws = FakeWebSocket(
        [
            {"event": "ms.channel.clientConnect"},
            {
                "event": "ms.channel.connect",
                "data": {
                    "token": "fresh",
                    "clients": [
                        {"isHost": True, "deviceName": "Smart Device"},
                        {
                            "isHost": False,
                            "deviceName": "Home Assistant",
                        },
                    ],
                },
            },
            {"event": "ms.channel.clientDisconnect"},
            {"event": "ms.channel.ready"},
        ]
    )
    art = make_art(token="remote-token")
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        assert await art.open() is ws
    assert art.token == "remote-token"
    assert not ws.closed


async def test_open_ignores_art_token_across_reconnect_generations():
    remote_token = "remote-issued-token"
    art_token = "art-handshake-token"
    first_ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {
                    "token": art_token,
                    "clients": [{"isHost": True}],
                },
            },
            {"event": "ms.channel.ready"},
        ]
    )
    second_ws = FakeWebSocket(handshake_frames())
    art = make_art(token=remote_token)
    connect_mock = AsyncMock(side_effect=[first_ws, second_ws])

    try:
        with patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            connect_mock,
        ):
            assert await art.open() is first_ws
            token_after_first_generation = art.token
            await art.close()
            assert await art.open() is second_ws

        reconnect_url = connect_mock.await_args_list[1].args[0]
        reconnect_token = parse_qs(urlparse(reconnect_url).query)["token"]
        assert reconnect_token == [remote_token]
        assert token_after_first_generation == remote_token
        assert art.token == remote_token
    finally:
        await art.close()


async def test_open_uses_quiet_logger_and_never_logs_private_handshake(
    caplog,
):
    handshake_token = "private-art-handshake-token"
    remote_token = "private-remote-token"
    client = "private-art-client-name"
    host = "private-art-tv-host"
    frame_secret = "private-art-frame-field"
    ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {
                    "token": handshake_token,
                    "clients": [
                        {
                            "isHost": True,
                            "deviceName": host,
                            "private": frame_secret,
                        },
                        {"isHost": False, "deviceName": client},
                    ],
                },
            },
            {"event": "ms.channel.ready"},
        ]
    )
    art = FrameArt(
        host,
        token=remote_token,
        ssl_context=MagicMock(),
        task_factory=task_factory,
        event_callback=None,
    )
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="samsungtvws")
    caplog.set_level(logging.DEBUG, logger="websockets")

    async def _connect(url, **kwargs):
        kwargs["logger"].debug("private websocket URL %s", url)
        return ws

    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(side_effect=_connect),
    ) as connect_mock:
        assert await art.open() is ws

    assert connect_mock.await_args.kwargs["logger"] is QUIET_WEBSOCKET_LOGGER
    assert art.token == remote_token
    assert not {
        value
        for value in (
            handshake_token,
            remote_token,
            client,
            host,
            frame_secret,
        )
        if value in caplog.text
    }
    await art.close()


@pytest.mark.parametrize(
    ("event", "expected_error", "message"),
    [
        (
            "ms.channel.unauthorized",
            UnauthorizedError,
            "Art authorization rejected",
        ),
        ("unexpected", ConnectionFailure, "Art handshake failed"),
    ],
)
async def test_open_failure_is_sanitized_and_never_logs_raw_frame(
    event, expected_error, message, caplog
):
    secret = "private-art-failed-handshake-frame"
    ws = FakeWebSocket(
        [{"event": event, "data": {"token": secret, "private": secret}}]
    )
    art = make_art()
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="samsungtvws")

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(expected_error) as raised,
    ):
        await art.open()

    assert str(raised.value) == message
    assert secret not in str(raised.value)
    assert secret not in caplog.text


async def test_generic_open_failure_is_sanitized():
    secret = "private-art-connect-exception"
    art = make_art()

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(side_effect=OSError(secret)),
        ),
        pytest.raises(ConnectionFailure) as raised,
    ):
        await art.open()

    assert str(raised.value) == "Art connection failed"
    assert raised.value.__suppress_context__ is True
    assert secret not in str(raised.value)


async def test_open_uses_instance_timeout_for_connect():
    ws = FakeWebSocket(handshake_frames())
    art = make_art(timeout=0.123)
    connect_mock = AsyncMock(return_value=ws)
    try:
        with patch(
            "custom_components.samsungtv_frame.frame_art.connect", connect_mock
        ):
            assert await art.open() is ws

        assert connect_mock.await_args.kwargs["open_timeout"] == 0.123
    finally:
        await art.close()


async def test_open_uses_instance_timeout_for_handshake():
    ws = FakeWebSocket(handshake_frames(), recv_delay=0.02)
    art = make_art(timeout=0.01)
    try:
        with (
            patch(
                "custom_components.samsungtv_frame.frame_art.connect",
                AsyncMock(return_value=ws),
            ),
            pytest.raises(TimeoutError),
        ):
            await art.open()
    finally:
        await art.close()

    assert ws.closed
    assert art.connection is None


async def test_open_fails_fast_when_connect_lists_no_art_host():
    ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {
                    "clients": [
                        {"isHost": False, "deviceName": "Home Assistant"}
                    ]
                },
            },
            {"event": "ms.channel.ready"},
        ]
    )
    art = make_art()
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(ArtHostUnavailable),
    ):
        await art.open()
    assert ws.closed
    assert ws.frames.qsize() == 1
    assert art.connection is None


async def test_open_allows_missing_client_metadata_when_ready_arrives():
    """Retain compatibility with connect frames that omit client metadata."""
    ws = FakeWebSocket(
        [
            {"event": "ms.channel.connect", "data": {}},
            {"event": "ms.channel.ready"},
        ]
    )
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        assert await art.open() is ws
    await art.close()


@pytest.mark.parametrize(
    "clients",
    [
        pytest.param([], id="empty-list"),
        pytest.param({}, id="non-list"),
        pytest.param([{}], id="missing-role"),
        pytest.param([None], id="non-dict-entry"),
        pytest.param([{"isHost": "false"}], id="non-boolean-role"),
    ],
)
async def test_open_allows_unknown_client_role_metadata(clients):
    ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {"clients": clients},
            },
            {"event": "ms.channel.ready"},
        ]
    )
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        assert await art.open() is ws
    await art.close()


async def test_open_rejects_malformed_clients_with_explicit_false_role():
    ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {"clients": [None, {"isHost": False}]},
            },
            {"event": "ms.channel.ready"},
        ]
    )
    art = make_art()
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(ArtHostUnavailable),
    ):
        await art.open()
    assert ws.closed
    assert ws.frames.qsize() == 1
    assert art.connection is None


async def test_open_times_out_when_host_is_present_but_ready_never_arrives():
    ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {
                    "clients": [
                        {"isHost": True, "deviceName": "Smart Device"}
                    ]
                },
            }
        ]
    )
    art = make_art(timeout=0.01)
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(TimeoutError),
    ):
        await art.open()
    assert ws.closed
    assert art.connection is None


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


async def test_failed_handshake_close_error_force_aborts_without_token_adoption():
    ws = FakeWebSocket(
        [
            {
                "event": "ms.channel.connect",
                "data": {"token": "new-token"},
            },
            {"event": "unexpected"},
        ]
    )
    ws.close = AsyncMock(side_effect=OSError("close failed"))
    art = make_art()

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(ConnectionFailure, match="^Art handshake failed$"),
    ):
        await art.open()

    ws.transport.abort.assert_called_once_with()
    assert art.connection is None
    assert art.token == "tok"


async def test_failed_handshake_close_deadline_force_aborts_and_drains():
    ws = FakeWebSocket([{"event": "unexpected"}])
    close_finished = asyncio.Event()
    never_finishes = asyncio.Event()

    async def _close():
        try:
            await never_finishes.wait()
        finally:
            close_finished.set()

    ws.close = _close
    art = make_art()

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
            0.01,
        ),
        pytest.raises(ConnectionFailure, match="^Art handshake failed$"),
    ):
        await art.open()

    ws.transport.abort.assert_called_once_with()
    assert close_finished.is_set()
    assert art.connection is None
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "samsungtv_frame-art-socket-close"
    ]


async def test_cancellation_during_failed_handshake_cleanup_wins_and_aborts():
    ws = FakeWebSocket([{"event": "unexpected"}])
    close_started = asyncio.Event()
    close_finished = asyncio.Event()
    never_finishes = asyncio.Event()

    async def _close():
        close_started.set()
        try:
            await never_finishes.wait()
        finally:
            close_finished.set()

    ws.close = _close
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        open_task = asyncio.create_task(art.open())
        await close_started.wait()
        open_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await open_task

    ws.transport.abort.assert_called_once_with()
    assert close_finished.is_set()
    assert art.connection is None


async def test_repeated_cancellation_during_failed_handshake_cleanup_wins():
    ws = FakeWebSocket([{"event": "unexpected"}])
    close_started = asyncio.Event()
    first_close_cancellation = asyncio.Event()
    close_finished = asyncio.Event()
    never_finishes = asyncio.Event()

    async def _cancellation_resistant_close():
        close_started.set()
        try:
            await never_finishes.wait()
        except asyncio.CancelledError:
            first_close_cancellation.set()
            await never_finishes.wait()
        finally:
            close_finished.set()

    ws.close = _cancellation_resistant_close
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        open_task = asyncio.create_task(art.open())
        await close_started.wait()
        open_task.cancel()
        await first_close_cancellation.wait()
        open_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await open_task

    assert ws.transport.abort.call_count >= 1
    assert close_finished.is_set()
    assert art.connection is None
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "samsungtv_frame-art-socket-close"
    ]


async def test_open_deadline_bounds_endless_broadcast_stream():
    ws = FakeWebSocket([{"event": "ms.channel.clientConnect"}] * 20)
    art = make_art(timeout=0.01)
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        pytest.raises(TimeoutError),
    ):
        await art.open()
    assert ws.closed
    assert art.connection is None


async def test_open_is_idempotent():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    connect_mock = AsyncMock(return_value=ws)
    with patch("custom_components.samsungtv_frame.frame_art.connect", connect_mock):
        assert await art.open() is ws
        assert await art.open() is ws
    connect_mock.assert_awaited_once()


async def test_open_rejects_permanent_stop_without_connecting():
    art = make_art()
    connect_mock = AsyncMock()
    art.stop()

    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            connect_mock,
        ),
        pytest.raises(ConnectionFailure, match="stopped"),
    ):
        await art.open()

    connect_mock.assert_not_awaited()
    assert art.connection is None


async def test_open_queued_on_lifecycle_lock_rechecks_permanent_stop():
    art = make_art()
    ws = FakeWebSocket(handshake_frames())
    connect_mock = AsyncMock(return_value=ws)

    await art._lifecycle_lock.acquire()
    open_task = asyncio.create_task(art.open())
    await asyncio.sleep(0)
    art.stop()
    art._lifecycle_lock.release()

    try:
        with (
            patch(
                "custom_components.samsungtv_frame.frame_art.connect",
                connect_mock,
            ),
            pytest.raises(ConnectionFailure, match="stopped"),
        ):
            await open_task
    finally:
        await art.close()

    connect_mock.assert_not_awaited()
    assert art.connection is None


async def test_open_stop_during_handshake_closes_local_socket_before_publish():
    art = make_art()
    ws = FakeWebSocket([])
    connect_mock = AsyncMock(return_value=ws)
    connect_frame = handshake_frames()[0]
    connect_frame["data"]["token"] = "new-token-after-stop"

    with patch(
        "custom_components.samsungtv_frame.frame_art.connect", connect_mock
    ):
        open_task = asyncio.create_task(art.open())
        while not connect_mock.await_count:
            await asyncio.sleep(0)
        art.stop()
        await ws.frames.put(json.dumps(connect_frame))
        await ws.frames.put(json.dumps({"event": "ms.channel.ready"}))

        try:
            with pytest.raises(ConnectionFailure, match="stopped"):
                await open_task
            assert ws.closed
            assert art.connection is None
            assert art.token == "tok"
        finally:
            await art.close()


async def test_start_listening_is_idempotent():
    ws = FakeWebSocket(handshake_frames())
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


async def test_start_listening_factory_failure_clears_binding_and_coroutine():
    created = []

    def failing_factory(coroutine, _name):
        created.append(coroutine)
        raise RuntimeError("task factory failed")

    ws = FakeWebSocket(handshake_frames())
    art = FrameArt(
        "1.2.3.4",
        token="tok",
        ssl_context=MagicMock(),
        task_factory=failing_factory,
        event_callback=None,
    )
    try:
        with (
            patch(
                "custom_components.samsungtv_frame.frame_art.connect",
                AsyncMock(return_value=ws),
            ),
            pytest.raises(RuntimeError, match="task factory failed"),
        ):
            await art.start_listening()

        assert art._recv_loop is None
        assert art._receiver_connection is None
        assert len(created) == 1
        assert created[0].cr_frame is None
    finally:
        for coroutine in created:
            coroutine.close()
        await art.close()


async def test_is_alive_becomes_false_when_receiver_exits():
    ws = FakeWebSocket(
        [
            *handshake_frames(),
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


async def test_is_alive_rejects_receiver_bound_to_different_connection():
    art = make_art()
    published = FakeWebSocket([])
    stale = FakeWebSocket([])
    receiver = asyncio.create_task(
        asyncio.Event().wait(), name="test-mismatched-art-receiver"
    )
    art.connection = published
    art._recv_loop = receiver
    art._receiver_connection = stale

    try:
        assert not art.is_alive()
    finally:
        await art.close()

    assert receiver.done()
    assert art._recv_loop is None
    assert art._receiver_connection is None


async def test_receiver_decodes_push_payload_for_callback():
    callback = AsyncMock()
    ws = FakeWebSocket(
        [
            *handshake_frames(),
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


@pytest.mark.parametrize(
    ("event", "data"),
    [
        ("ms.channel.clientConnect", "{}"),
        ("d2d_service_message", None),
        ("d2d_service_message", "not json{"),
        ("d2d_service_message", '["not", "a", "dict"]'),
    ],
)
async def test_dispatch_ignores_non_push_and_undecodable_payloads(event, data):
    callback = AsyncMock()
    art = make_art(callback=callback)
    await art._dispatch_frame(event, {"event": event, "data": data})
    callback.assert_not_awaited()


def handshake_frames():
    """Return a successful Art websocket handshake with the TV host present."""
    return [
        {
            "event": "ms.channel.connect",
            "data": {
                "clients": [
                    {"isHost": True, "deviceName": "Smart Device"},
                    {"isHost": False, "deviceName": "Home Assistant"},
                ]
            },
        },
        {"event": "ms.channel.ready"},
    ]


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
        await art.start_listening()
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


async def test_request_wire_payload_is_unchanged_and_not_logged(caplog):
    request_name = "private-art-request-name"
    content_id = "private-art-content-id"
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="samsungtvws")

    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
        request_task = asyncio.create_task(
            art.request(request_name, content_id=content_id)
        )
        await wait_for_sent(ws, 1)
        inner = sent_art_request(ws, 0)
        expected = ArtChannelEmitCommand.art_app_request(inner).get_payload()
        assert ws.sent[0] == expected
        await ws.frames.put(
            art_response(request_id=inner["request_id"], value="ok")
        )
        assert (await request_task)["value"] == "ok"
        await art.close()

    assert request_name not in caplog.text
    assert content_id not in caplog.text


async def test_send_command_preserves_dict_and_sleep_semantics(caplog):
    secret = "private-art-dict-payload"
    ws = FakeWebSocket([])
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="samsungtvws")

    payload = {"request": secret, "value": 1}
    await FrameArt._send_command(ws, payload, 0)
    assert ws.sent == [json.dumps(payload)]

    with patch(
        "custom_components.samsungtv_frame.frame_art.asyncio.sleep",
        AsyncMock(),
    ) as sleep:
        await FrameArt._send_command(ws, SamsungTVSleepCommand(0.25), 99)
    sleep.assert_awaited_once_with(0.25)
    assert ws.sent == [json.dumps(payload)]
    assert secret not in caplog.text


async def test_request_falls_back_to_id_when_request_id_is_null():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with (
        patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            AsyncMock(return_value=ws),
        ),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_REQUEST_DEADLINE",
            0.01,
        ),
    ):
        await art.start_listening()
        request_task = asyncio.create_task(art.request("get_artmode_status"))
        await wait_for_sent(ws, 1)
        request_id = sent_art_request(ws, 0)["id"]
        await ws.frames.put(
            art_response(request_id=None, id=request_id, value="on")
        )
        assert await request_task == {
            "request_id": None,
            "id": request_id,
            "value": "on",
        }
        await art.close()


async def test_request_correlates_uuidless_expected_sub_event():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
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
        await art.start_listening()
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
        await art.start_listening()
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


@pytest.mark.parametrize("callback_kind", ["sync", "async"])
async def test_callback_exception_does_not_interrupt_pending_request(
    callback_kind, caplog
):
    if callback_kind == "sync":

        def callback(_event, _payload):
            raise RuntimeError("callback failed")

    else:

        async def callback(_event, _payload):
            raise RuntimeError("callback failed")

    ws = FakeWebSocket(handshake_frames())
    art = make_art(callback=callback)
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
        request_task = asyncio.create_task(art.request("get_artmode_status"))
        try:
            await wait_for_sent(ws, 1)
            inner = sent_art_request(ws, 0)
            await ws.frames.put(
                art_response(event="art_mode_changed", value="on")
            )
            await ws.frames.put(
                art_response(
                    request_id=inner["request_id"],
                    value="on",
                )
            )

            assert (await request_task)["value"] == "on"
            assert art.is_alive()
            assert "Art event callback failed" in caplog.text
        finally:
            await art.close()


async def test_receiver_and_callback_failures_do_not_log_private_data(caplog):
    content_secret = "private-art-receiver-content"
    exception_secret = "private-art-callback-exception"

    async def callback(_event, _payload):
        raise RuntimeError(exception_secret)

    ws = FakeWebSocket(
        [
            *handshake_frames(),
            {
                "event": "d2d_service_message",
                "data": json.dumps(
                    {"event": "art_mode_changed", "value": content_secret}
                ),
            },
        ]
    )
    art = make_art(callback=callback)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="samsungtvws")

    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
        for _ in range(20):
            if "Art event callback failed" in caplog.text:
                break
            await asyncio.sleep(0)
        await ws.frames.put(OSError("private-art-receiver-exception"))
        receiver = art._recv_loop
        assert receiver is not None
        await receiver

    assert "Art event callback failed" in caplog.text
    assert "Art receiver exited" in caplog.text
    assert not {
        value
        for value in (
            content_secret,
            exception_secret,
            "private-art-receiver-exception",
        )
        if value in caplog.text
    }


async def test_dispatch_propagates_callback_cancellation():
    async def callback(_event, _payload):
        raise asyncio.CancelledError

    art = make_art(callback=callback)
    frame = art_response(event="art_mode_changed", value="on")
    with pytest.raises(asyncio.CancelledError):
        await art._dispatch_frame("d2d_service_message", json.loads(frame))


async def test_receiver_disconnect_fails_pending_request():
    ws = FakeWebSocket(handshake_frames())
    art = make_art()
    with patch(
        "custom_components.samsungtv_frame.frame_art.connect",
        AsyncMock(return_value=ws),
    ):
        await art.start_listening()
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
        await art.start_listening()
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
    with (
        patch.object(art, "start_listening", AsyncMock()),
        patch.object(art, "is_alive", return_value=True),
    ):
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
        await art.start_listening()
        await art.request("get_artmode_status")
    assert ws.closed
    assert art.connection is None
    assert not art._pending


async def test_request_deadline_includes_blocked_websocket_send():
    art = make_art()
    ws = FakeWebSocket([])
    art.connection = ws
    send_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_send(*_args):
        send_started.set()
        await never_release.wait()

    with (
        patch.object(art, "start_listening", AsyncMock()),
        patch.object(art, "is_alive", return_value=True),
        patch.object(art, "_send_command", side_effect=blocked_send),
        patch(
            "custom_components.samsungtv_frame.frame_art.ART_REQUEST_DEADLINE",
            0.01,
        ),
        pytest.raises(TimeoutError),
    ):
        await asyncio.wait_for(
            art.request("get_artmode_status"), timeout=0.05
        )

    assert send_started.is_set()
    assert ws.closed
    assert art.connection is None
    assert not art._pending


async def test_close_during_send_retrieves_failed_pending_future():
    art = make_art()
    art.connection = FakeWebSocket([])
    send_started = asyncio.Event()
    release_send = asyncio.Event()
    loop = asyncio.get_running_loop()
    exception_contexts = []
    previous_handler = loop.get_exception_handler()

    async def blocked_send(*_args):
        send_started.set()
        await release_send.wait()
        raise OSError("send failed after close")

    request_task = None
    loop.set_exception_handler(
        lambda _loop, context: exception_contexts.append(context)
    )
    try:
        with (
            patch.object(art, "start_listening", AsyncMock()),
            patch.object(art, "is_alive", return_value=True),
            patch.object(art, "_send_command", side_effect=blocked_send),
        ):
            request_task = asyncio.create_task(
                art.request("get_artmode_status")
            )
            await send_started.wait()
            await art.close()
            release_send.set()
            with pytest.raises(OSError, match="send failed after close"):
                await request_task
            request_task = None
            gc.collect()
            await asyncio.sleep(0)
    finally:
        release_send.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
        loop.set_exception_handler(previous_handler)

    assert not art._pending
    assert not [
        context
        for context in exception_contexts
        if context.get("message") == "Future exception was never retrieved"
    ]


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


async def test_close_failure_force_aborts_and_detaches_connection():
    art = make_art()
    ws = FakeWebSocket([])
    ws.close = AsyncMock(side_effect=OSError("close failed"))
    art.connection = ws

    await art.close()

    ws.transport.abort.assert_called_once_with()
    assert art.connection is None


async def test_close_deadline_force_aborts_and_drains_close_task():
    art = make_art()
    ws = FakeWebSocket([])
    close_finished = asyncio.Event()
    never_finishes = asyncio.Event()

    async def _close():
        try:
            await never_finishes.wait()
        finally:
            close_finished.set()

    ws.close = _close
    art.connection = ws

    with patch(
        "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
        0.01,
    ):
        await art.close()

    ws.transport.abort.assert_called_once_with()
    assert close_finished.is_set()
    assert art.connection is None
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == "samsungtv_frame-art-socket-close"
    ]


async def test_close_cancellation_force_aborts_detaches_and_propagates():
    art = make_art()
    ws = FakeWebSocket([])
    close_started = asyncio.Event()
    close_finished = asyncio.Event()
    never_finishes = asyncio.Event()

    async def _close():
        close_started.set()
        try:
            await never_finishes.wait()
        finally:
            close_finished.set()

    ws.close = _close
    art.connection = ws
    close_task = asyncio.create_task(art.close())
    await close_started.wait()
    close_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    ws.transport.abort.assert_called_once_with()
    assert close_finished.is_set()
    assert art.connection is None


def register_pending_response(art):
    """Register one pending response and return its observable future."""
    future = asyncio.get_running_loop().create_future()
    pending = MagicMock()
    pending.future = future
    art._pending["pending-close"] = pending
    return future


def assert_no_art_close_tasks():
    """Assert that no Art receiver, transfer, or socket-close task remains."""
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name()
        in {
            "test-blocking-art-transfer",
            "test-blocking-art-receiver",
            "samsungtv_frame-art-socket-close",
        }
    ]


@pytest.mark.parametrize("cancel_count", [1, 2], ids=["single", "repeated"])
async def test_close_remembers_caller_cancellation_during_transfer_drain(
    cancel_count,
):
    art = make_art()
    ws = FakeWebSocket([])
    art.connection = ws
    pending = register_pending_response(art)
    transfer_started = asyncio.Event()
    transfer_cancelled = asyncio.Event()
    release_transfer = asyncio.Event()
    transfer_finished = asyncio.Event()
    child_cancellations = 0

    async def _blocking_transfer():
        nonlocal child_cancellations
        transfer_started.set()
        try:
            while not release_transfer.is_set():
                try:
                    await release_transfer.wait()
                except asyncio.CancelledError:
                    child_cancellations += 1
                    transfer_cancelled.set()
        finally:
            transfer_finished.set()

    transfer = asyncio.create_task(
        _blocking_transfer(), name="test-blocking-art-transfer"
    )
    art._transfer_tasks.add(transfer)
    await transfer_started.wait()
    close_task = asyncio.create_task(art.close())
    await transfer_cancelled.wait()

    for _ in range(cancel_count):
        close_task.cancel()
        await asyncio.sleep(0)
        assert not close_task.done()
    release_transfer.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert transfer.done()
    assert transfer_finished.is_set()
    assert child_cancellations >= 1
    assert not art._transfer_tasks
    assert ws.closed
    assert ws.state is State.CLOSED
    assert art.connection is None
    assert isinstance(pending.exception(), ConnectionFailure)
    assert not art._pending
    assert not art._closing
    assert_no_art_close_tasks()


@pytest.mark.parametrize("cancel_count", [1, 2], ids=["single", "repeated"])
async def test_close_remembers_caller_cancellation_during_receiver_drain(
    cancel_count,
):
    art = make_art()
    ws = FakeWebSocket([])
    recv_started = asyncio.Event()
    recv_forever = asyncio.Event()
    socket_close_started = asyncio.Event()
    release_socket_close = asyncio.Event()
    socket_close_finished = asyncio.Event()

    async def _recv():
        recv_started.set()
        await recv_forever.wait()

    async def _cancellation_resistant_close():
        socket_close_started.set()
        try:
            while not release_socket_close.is_set():
                try:
                    await release_socket_close.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            ws.closed = True
            ws.state = State.CLOSED
            socket_close_finished.set()

    ws.recv = _recv
    ws.close = _cancellation_resistant_close
    art.connection = ws
    pending = register_pending_response(art)
    receiver = asyncio.create_task(
        art._receive_loop(), name="test-blocking-art-receiver"
    )
    art._recv_loop = receiver
    await recv_started.wait()
    close_task = asyncio.create_task(art.close())
    await socket_close_started.wait()

    for _ in range(cancel_count):
        close_task.cancel()
        await asyncio.sleep(0)
        assert not close_task.done()
    release_socket_close.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert receiver.done()
    assert socket_close_finished.is_set()
    assert ws.closed
    assert ws.state is State.CLOSED
    assert art.connection is None
    assert art._recv_loop is None
    assert isinstance(pending.exception(), ConnectionFailure)
    assert not art._pending
    assert not art._closing
    assert_no_art_close_tasks()


@pytest.mark.parametrize(
    "caller_cancel", [False, True], ids=["deadline", "caller-cancel"]
)
async def test_close_retains_transfer_that_outlives_drain_deadline(
    caller_cancel,
):
    art = make_art()
    ws = FakeWebSocket([])
    art.connection = ws
    pending_response = register_pending_response(art)
    transfer_started = asyncio.Event()
    transfer_cancelled = asyncio.Event()
    release_transfer = asyncio.Event()

    async def _resistant_transfer():
        transfer_started.set()
        while not release_transfer.is_set():
            try:
                await release_transfer.wait()
            except asyncio.CancelledError:
                transfer_cancelled.set()

    transfer = asyncio.create_task(
        _resistant_transfer(), name="test-resistant-art-transfer"
    )
    art._transfer_tasks.add(transfer)
    await transfer_started.wait()
    with patch(
        "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
        0.01,
    ):
        first_close = asyncio.create_task(art.close())
        await transfer_cancelled.wait()
        if caller_cancel:
            first_close.cancel()
        result = (await asyncio.gather(first_close, return_exceptions=True))[0]

    try:
        if caller_cancel:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert isinstance(result, ConnectionFailure)
            assert str(result) == "Art tasks did not stop"
        assert not transfer.done()
        assert transfer in art._transfer_tasks
        assert ws.closed
        assert ws.state is State.CLOSED
        assert art.connection is None
        assert isinstance(pending_response.exception(), ConnectionFailure)
        assert not art._pending
        assert not art._closing
    finally:
        release_transfer.set()
        await transfer

    await art.close()
    await art.close()
    assert not art._transfer_tasks
    assert_no_art_close_tasks()


@pytest.mark.parametrize(
    "caller_cancel", [False, True], ids=["deadline", "caller-cancel"]
)
async def test_close_retains_receiver_that_outlives_drain_deadline(
    caller_cancel,
):
    art = make_art()
    ws = FakeWebSocket([])
    recv_started = asyncio.Event()
    recv_cancelled = asyncio.Event()
    release_recv = asyncio.Event()

    async def _resistant_recv():
        recv_started.set()
        while not release_recv.is_set():
            try:
                await release_recv.wait()
            except asyncio.CancelledError:
                recv_cancelled.set()
        raise ConnectionFailure("released receiver")

    ws.recv = _resistant_recv
    art.connection = ws
    pending_response = register_pending_response(art)
    receiver = asyncio.create_task(
        art._receive_loop(), name="test-resistant-art-receiver"
    )
    art._recv_loop = receiver
    await recv_started.wait()
    with patch(
        "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
        0.01,
    ):
        first_close = asyncio.create_task(art.close())
        await recv_cancelled.wait()
        if caller_cancel:
            first_close.cancel()
        result = (await asyncio.gather(first_close, return_exceptions=True))[0]

    try:
        if caller_cancel:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert isinstance(result, ConnectionFailure)
            assert str(result) == "Art tasks did not stop"
        assert not receiver.done()
        assert art._recv_loop is receiver
        assert ws.closed
        assert ws.state is State.CLOSED
        assert art.connection is None
        assert isinstance(pending_response.exception(), ConnectionFailure)
        assert not art._pending
        assert not art._closing
    finally:
        release_recv.set()
        await receiver

    await art.close()
    await art.close()
    assert art._recv_loop is None
    assert_no_art_close_tasks()


async def test_open_fences_retained_receiver_until_its_finally_completes():
    art = make_art()
    old_websocket = FakeWebSocket([])
    new_websocket = FakeWebSocket(handshake_frames())
    recv_started = asyncio.Event()
    recv_cancelled = asyncio.Event()
    release_recv = asyncio.Event()

    async def _resistant_recv():
        recv_started.set()
        while not release_recv.is_set():
            try:
                await release_recv.wait()
            except asyncio.CancelledError:
                recv_cancelled.set()
        raise ConnectionFailure("released receiver")

    old_websocket.recv = _resistant_recv
    art.connection = old_websocket
    receiver = asyncio.create_task(
        art._receive_loop(), name="test-fenced-art-receiver"
    )
    art._recv_loop = receiver
    art._receiver_connection = old_websocket
    await recv_started.wait()
    with patch(
        "custom_components.samsungtv_frame.frame_art.ART_CLOSE_DEADLINE",
        0.01,
    ):
        result = (
            await asyncio.gather(art.close(), return_exceptions=True)
        )[0]
    assert isinstance(result, ConnectionFailure)
    assert recv_cancelled.is_set()
    assert not receiver.done()
    assert art.connection is None
    assert art._receiver_connection is old_websocket

    connect_mock = AsyncMock(return_value=new_websocket)
    pending = None
    try:
        live_children = getattr(art, "has_live_children", None)
        assert callable(live_children)
        assert live_children()
        with (
            patch(
                "custom_components.samsungtv_frame.frame_art.connect",
                connect_mock,
            ),
            pytest.raises(
                ConnectionFailure, match="^Art cleanup pending$"
            ) as caught,
        ):
            await art.open()
        assert type(caught.value).__name__ == "ArtCleanupPending"
        connect_mock.assert_not_awaited()
        assert art.connection is None
        assert art._recv_loop is receiver

        release_recv.set()
        await receiver
        assert not live_children()
        assert art._recv_loop is None
        assert art._receiver_connection is None

        with patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            connect_mock,
        ):
            assert await art.open() is new_websocket
            await art.start_listening()
        connect_mock.assert_awaited_once()
        assert art._receiver_connection is new_websocket
        pending = register_pending_response(art)
        await asyncio.sleep(0)
        assert not pending.done()
    finally:
        release_recv.set()
        await asyncio.gather(receiver, return_exceptions=True)
        await art.close()

    assert pending is not None
    assert isinstance(pending.exception(), ConnectionFailure)


async def test_open_fences_current_transfer_until_outer_finally_untracks():
    art = make_art()
    old_websocket = FakeWebSocket([])
    new_websocket = FakeWebSocket(handshake_frames())
    art.connection = old_websocket
    close_returned = asyncio.Event()
    release_transfer = asyncio.Event()

    async def _current_transfer_owner():
        current = asyncio.current_task()
        assert current is not None
        art._transfer_tasks.add(current)
        try:
            await art.close()
            close_returned.set()
            await release_transfer.wait()
        finally:
            art._transfer_tasks.discard(current)

    transfer = asyncio.create_task(
        _current_transfer_owner(), name="test-current-art-transfer"
    )
    await close_returned.wait()
    connect_mock = AsyncMock(return_value=new_websocket)
    try:
        assert not transfer.done()
        assert art.has_live_children()
        with (
            patch(
                "custom_components.samsungtv_frame.frame_art.connect",
                connect_mock,
            ),
            pytest.raises(
                ConnectionFailure, match="^Art cleanup pending$"
            ) as caught,
        ):
            await art.open()
        assert type(caught.value).__name__ == "ArtCleanupPending"
        connect_mock.assert_not_awaited()
        assert art.connection is None

        release_transfer.set()
        await transfer
        assert not art.has_live_children()
        with patch(
            "custom_components.samsungtv_frame.frame_art.connect",
            connect_mock,
        ):
            assert await art.open() is new_websocket
        connect_mock.assert_awaited_once()
    finally:
        release_transfer.set()
        await asyncio.gather(transfer, return_exceptions=True)
        await art.close()


async def test_receiver_finished_close_failure_force_aborts_and_detaches():
    art = make_art()
    ws = FakeWebSocket([])
    ws.close = AsyncMock(side_effect=OSError("close failed"))
    art.connection = ws

    await art._receiver_finished(ws)

    ws.transport.abort.assert_called_once_with()
    assert art.connection is None


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

        await ws.frames.put(json.dumps(handshake_frames()[0]))
        await ws.frames.put(json.dumps({"event": "ms.channel.ready"}))
        opened, _ = await asyncio.gather(open_task, close_task)

    assert opened is ws
    assert ws.closed
    assert art.connection is None
