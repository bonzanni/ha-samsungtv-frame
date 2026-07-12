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

    def __init__(self, frames):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(json.dumps(frame))
        self.sent = []
        self.closed = False
        self.state = State.OPEN

    async def recv(self):
        frame = await self.frames.get()
        if isinstance(frame, BaseException):
            raise frame
        return frame

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self):
        self.closed = True
        self.state = State.CLOSED


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
