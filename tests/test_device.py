# tests/test_device.py
import asyncio
import json
import logging
from inspect import signature
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from samsungtvws.exceptions import (
    ConnectionFailure,
    ResponseError,
    UnauthorizedError,
)
from websockets.protocol import State

from custom_components.samsungtv_frame.art_session import (
    ArtSessionState,
    ArtSessionTrigger,
)
from custom_components.samsungtv_frame.device import FrameDevice
from custom_components.samsungtv_frame.frame_remote import (
    FrameRemote,
    RemotePairingRequired,
)
from custom_components.samsungtv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    SlideshowMode,
    SlideshowState,
)


VALID_ART_SETTINGS_PAYLOAD = {
    "data": json.dumps(
        [
            {"item": "brightness", "value": "7"},
            {"item": "color_temperature", "value": "-2"},
            {"item": "motion_timer", "value": "15"},
            {"item": "motion_sensitivity", "value": "2"},
            {"item": "brightness_sensor_setting", "value": "on"},
        ]
    )
}
EXPECTED_ART_SETTINGS = ArtSettingsSnapshot(
    supported=frozenset(ArtSettingKey),
    brightness=7,
    color_temperature=-2,
    motion_timer="15",
    motion_sensitivity="2",
    brightness_sensor_enabled=True,
)
MODERN_SLIDESHOW_PAYLOAD = {
    "value": "15",
    "type": "slideshow",
    "category_id": "MY-C0002",
}
LEGACY_SLIDESHOW_PAYLOAD = {
    "value": "30",
    "type": "shuffleslideshow",
    "category_id": "MY-C0004",
}
EXPECTED_MODERN_SLIDESHOW = SlideshowState(
    SlideshowMode.SEQUENTIAL, 15, "MY-C0002"
)
EXPECTED_LEGACY_SLIDESHOW = SlideshowState(
    SlideshowMode.SHUFFLE, 30, "MY-C0004"
)


@pytest.fixture
def device(hass):
    task_calls = []

    def task_factory(coro, name):
        task_calls.append(name)
        return asyncio.create_task(coro, name=name)

    device = FrameDevice(
        hass,
        host="1.2.3.4",
        mac="A0:D0:5B:86:CE:B7",
        token="tok",
        ssl_context=MagicMock(),
        task_factory=task_factory,
    )
    session = MagicMock()
    session.ready = True
    session.generation = 3
    session.state = ArtSessionState.READY
    session.async_start = AsyncMock()
    session.async_ensure_ready = AsyncMock(return_value=True)
    session.async_connection_failed = AsyncMock()
    session.async_stop = AsyncMock()
    device._art_session = session
    device._test_task_factory_calls = task_calls
    return device


def test_device_constructs_art_session(hass):
    task_factory = MagicMock()
    device = FrameDevice(
        hass,
        host="1.2.3.4",
        mac="A0:D0:5B:86:CE:B7",
        token="tok",
        ssl_context=MagicMock(),
        task_factory=task_factory,
    )

    assert device._art_session._art is device._art
    assert device._art_session._task_factory is task_factory


def test_device_constructs_remote_with_entry_ssl_context(hass):
    ssl_context = MagicMock()

    device = FrameDevice(
        hass,
        host="1.2.3.4",
        mac="A0:D0:5B:86:CE:B7",
        token="tok",
        ssl_context=ssl_context,
        task_factory=MagicMock(),
    )

    assert isinstance(device._remote, FrameRemote)
    assert device._remote._ssl_context is ssl_context


@pytest.mark.parametrize("token", [None, ""])
def test_device_defers_remote_construction_without_stored_token(hass, token):
    with patch("custom_components.samsungtv_frame.device.FrameRemote") as remote_cls:
        device = FrameDevice(
            hass,
            host="1.2.3.4",
            mac="A0:D0:5B:86:CE:B7",
            token=token,
            ssl_context=MagicMock(),
            task_factory=MagicMock(),
        )

    remote_cls.assert_not_called()
    assert device._remote is None
    assert device.newest_token is None


@pytest.mark.parametrize("token", [None, ""])
async def test_legacy_remote_command_requests_reauth_without_network(
    hass, token
):
    reauth = MagicMock()
    persist = MagicMock()
    with patch("custom_components.samsungtv_frame.device.FrameRemote") as remote_cls:
        remote_cls.return_value.send_commands = AsyncMock()
        device = FrameDevice(
            hass,
            host="1.2.3.4",
            mac="A0:D0:5B:86:CE:B7",
            token=token,
            ssl_context=MagicMock(),
            task_factory=MagicMock(),
        )
        device.set_remote_reauth_callback(reauth)
        device.set_remote_token_callback(persist)

        with pytest.raises(RemotePairingRequired):
            await device.async_send_key("KEY_HOME")

    remote_cls.assert_not_called()
    reauth.assert_called_once_with()
    persist.assert_not_called()


@pytest.mark.parametrize("token", [None, ""])
async def test_legacy_app_list_never_constructs_remote_or_reauths(hass, token):
    reauth = MagicMock()
    with patch("custom_components.samsungtv_frame.device.FrameRemote") as remote_cls:
        remote_cls.return_value.app_list = AsyncMock(return_value=[])
        device = FrameDevice(
            hass,
            host="1.2.3.4",
            mac="A0:D0:5B:86:CE:B7",
            token=token,
            ssl_context=MagicMock(),
            task_factory=MagicMock(),
        )
        device.set_remote_reauth_callback(reauth)

        assert await device.async_app_list() is None

    remote_cls.assert_not_called()
    reauth.assert_not_called()


def test_update_token_constructs_deferred_remote_with_nonempty_token(hass):
    ssl_context = MagicMock()
    remote = MagicMock(token="new-token")
    with patch(
        "custom_components.samsungtv_frame.device.FrameRemote",
        return_value=remote,
    ) as remote_cls:
        device = FrameDevice(
            hass,
            host="1.2.3.4",
            mac="A0:D0:5B:86:CE:B7",
            token=None,
            ssl_context=ssl_context,
            task_factory=MagicMock(),
        )
        remote_cls.assert_not_called()

        device.update_token("new-token")

    remote_cls.assert_called_once_with(
        "1.2.3.4",
        token="new-token",
        ssl_context=ssl_context,
        timeout=8,
    )
    assert device._remote is remote
    assert device._art.token == "new-token"
    assert device.newest_token is None


def test_update_empty_token_keeps_remote_deferred(hass):
    with patch("custom_components.samsungtv_frame.device.FrameRemote") as remote_cls:
        device = FrameDevice(
            hass,
            host="1.2.3.4",
            mac="A0:D0:5B:86:CE:B7",
            token=None,
            ssl_context=MagicMock(),
            task_factory=MagicMock(),
        )

        device.update_token("")

    remote_cls.assert_not_called()
    assert device._remote is None
    assert device._art.token is None


async def test_legacy_stop_is_safe_without_constructing_remote(hass):
    with patch("custom_components.samsungtv_frame.device.FrameRemote") as remote_cls:
        device = FrameDevice(
            hass,
            host="1.2.3.4",
            mac="A0:D0:5B:86:CE:B7",
            token=None,
            ssl_context=MagicMock(),
            task_factory=lambda coro, name: asyncio.create_task(coro, name=name),
        )
        device._art_session = MagicMock(async_stop=AsyncMock())

        await device.async_stop()

    remote_cls.assert_not_called()
    device._art_session.async_stop.assert_awaited_once()


async def test_device_info_returns_device_dict(hass, device):
    rest = MagicMock()
    rest.rest_device_info = AsyncMock(return_value={"device": {"PowerState": "on"}})
    with patch.object(device, "_rest", rest):
        info = await device.async_device_info()
    assert info == {"PowerState": "on"}


async def test_device_info_none_when_unreachable(hass, device, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.samsungtv_frame")
    private_value = "private-device-info"
    rest = MagicMock()
    rest.rest_device_info = AsyncMock(side_effect=OSError(private_value))
    with patch.object(device, "_rest", rest):
        assert await device.async_device_info() is None
    assert private_value not in caplog.text
    assert "1.2.3.4" not in caplog.text
    assert "REST device info request failed" in caplog.text


async def test_app_status_failure_does_not_log_identifier_or_error(
    hass, device, caplog
):
    caplog.set_level(logging.DEBUG, logger="custom_components.samsungtv_frame")
    private_value = "private-app-status"
    private_app_id = "private-app-id"
    rest = MagicMock()
    rest.rest_app_status = AsyncMock(side_effect=OSError(private_value))

    with patch.object(device, "_rest", rest):
        assert await device.async_app_status(private_app_id) is None

    assert private_value not in caplog.text
    assert private_app_id not in caplog.text
    assert "REST app status request failed" in caplog.text


async def test_device_info_response_body_is_not_logged(
    hass, device, aioclient_mock, caplog
):
    private_sentinel = "private-device-response-sentinel"
    response = {
        "device": {
            "PowerState": "on",
            "name": private_sentinel,
            "wifiMac": "A0:D0:5B:86:CE:B7",
        }
    }
    aioclient_mock.get(
        "http://1.2.3.4:8001/api/v2/",
        json=response,
    )
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    assert await device.async_device_info() == response["device"]

    assert private_sentinel not in caplog.text
    assert "A0:D0:5B:86:CE:B7" not in caplog.text
    assert "1.2.3.4" not in caplog.text


async def test_app_status_response_and_identifier_are_not_logged(
    hass, device, aioclient_mock, caplog
):
    private_app_id = "private-app-id-sentinel"
    private_response = "private-app-response-sentinel"
    response = {"visible": True, "metadata": private_response}
    aioclient_mock.get(
        f"http://1.2.3.4:8001/api/v2/applications/{private_app_id}",
        json=response,
    )
    caplog.set_level(logging.DEBUG)
    caplog.clear()

    assert await device.async_app_status(private_app_id) == response

    assert private_response not in caplog.text
    assert private_app_id not in caplog.text
    assert "1.2.3.4" not in caplog.text


async def test_get_artmode_true(hass, device):
    device._art.get_artmode = AsyncMock(return_value="on")
    assert await device.async_get_artmode() is True


async def test_turn_on_sends_magic_packet(hass, device):
    with patch("custom_components.samsungtv_frame.device.send_magic_packet") as smp:
        await device.async_turn_on()
    smp.assert_called_once()
    assert smp.call_args.args[0] == "A0:D0:5B:86:CE:B7"


async def test_background_art_getter_returns_none_without_session_open(device):
    device._art_session.ready = False
    device._art.get_artmode = AsyncMock()

    assert await device.async_get_artmode() is None

    device._art_session.async_ensure_ready.assert_not_awaited()
    device._art.get_artmode.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "args", "delegate"),
    [
        ("async_get_artmode", (), "get_artmode"),
        ("async_get_current_art", (), "get_current"),
        ("async_get_art_thumbnail", ("MY_F0001",), "get_thumbnail"),
        ("async_get_art_settings", (), "get_art_settings_payload"),
        ("async_get_slideshow_state", (), "get_auto_rotation_status"),
    ],
)
async def test_background_art_getters_never_ensure_or_open(
    device, method, args, delegate
):
    device._art_session.ready = False
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    assert await getattr(device, method)(*args) is None
    device._art_session.async_ensure_ready.assert_not_awaited()
    operation.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "delegate"),
    [
        ("async_get_artmode", "get_artmode"),
        ("async_get_current_art", "get_current"),
        ("async_get_art_settings", "get_art_settings_payload"),
        ("async_get_slideshow_state", "get_auto_rotation_status"),
    ],
)
async def test_ready_art_getter_failure_is_reported_once(
    device, method, delegate
):
    error = OSError("lost")
    operation = AsyncMock(side_effect=error)
    setattr(device._art, delegate, operation)

    assert await getattr(device, method)() is None
    operation.assert_awaited_once()
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


async def test_ready_operation_failure_is_reported_to_session(device):
    error = OSError("lost")
    device._art.get_artmode = AsyncMock(side_effect=error)

    assert await device.async_get_artmode() is None
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


async def test_ready_art_read_response_error_keeps_session_ready(device):
    error = ResponseError("command rejected")
    device._art.get_artmode = AsyncMock(side_effect=error)

    assert await device.async_get_artmode() is None

    device._art.get_artmode.assert_awaited_once_with()
    device._art_session.async_ensure_ready.assert_not_awaited()
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_art_settings_aggregate_dialect_reuses_one_command_for_generation(
    device,
):
    device._art.get_art_settings_payload = AsyncMock(
        return_value=VALID_ART_SETTINGS_PAYLOAD
    )
    device._art.get_legacy_brightness = AsyncMock()
    device._art.get_legacy_color_temperature = AsyncMock()

    first = await device.async_get_art_settings()
    second = await device.async_get_art_settings()

    assert first == second == EXPECTED_ART_SETTINGS
    assert device._art.get_art_settings_payload.await_count == 2
    device._art.get_legacy_brightness.assert_not_awaited()
    device._art.get_legacy_color_temperature.assert_not_awaited()


async def test_correlated_aggregate_response_error_uses_legacy_for_generation(
    device,
):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=ResponseError("unsupported")
    )
    device._art.get_legacy_brightness = AsyncMock(return_value="7")
    device._art.get_legacy_color_temperature = AsyncMock(return_value="-2")
    expected = ArtSettingsSnapshot(
        supported=frozenset(
            {
                ArtSettingKey.BRIGHTNESS,
                ArtSettingKey.COLOR_TEMPERATURE,
            }
        ),
        brightness=7,
        color_temperature=-2,
    )

    first = await device.async_get_art_settings()
    second = await device.async_get_art_settings()

    assert first == second == expected
    device._art.get_art_settings_payload.assert_awaited_once_with()
    assert device._art.get_legacy_brightness.await_count == 2
    assert device._art.get_legacy_color_temperature.await_count == 2
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_malformed_aggregate_keeps_dialect_unknown_and_retries(device):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=[{"data": "not-json"}, VALID_ART_SETTINGS_PAYLOAD]
    )
    device._art.get_legacy_brightness = AsyncMock()
    device._art.get_legacy_color_temperature = AsyncMock()

    assert await device.async_get_art_settings() is None
    assert device._art_settings_dialect.value == "unknown"
    assert await device.async_get_art_settings() == EXPECTED_ART_SETTINGS

    assert device._art.get_art_settings_payload.await_count == 2
    device._art.get_legacy_brightness.assert_not_awaited()
    device._art.get_legacy_color_temperature.assert_not_awaited()


async def test_aggregate_transport_failure_has_no_legacy_fallback(device):
    error = OSError("lost")
    device._art.get_art_settings_payload = AsyncMock(side_effect=error)
    device._art.get_legacy_brightness = AsyncMock()
    device._art.get_legacy_color_temperature = AsyncMock()

    assert await device.async_get_art_settings() is None

    device._art.get_art_settings_payload.assert_awaited_once_with()
    device._art.get_legacy_brightness.assert_not_awaited()
    device._art.get_legacy_color_temperature.assert_not_awaited()
    device._art_session.async_connection_failed.assert_awaited_once_with(error)
    assert device._art_settings_dialect.value == "unknown"


async def test_generation_change_resets_aggregate_and_slideshow_dialects(device):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=[
            ResponseError("aggregate unsupported"),
            VALID_ART_SETTINGS_PAYLOAD,
        ]
    )
    device._art.get_legacy_brightness = AsyncMock(return_value="7")
    device._art.get_legacy_color_temperature = AsyncMock(return_value="-2")
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=[
            ResponseError("modern unsupported"),
            MODERN_SLIDESHOW_PAYLOAD,
        ]
    )
    device._art.get_legacy_slideshow_status = AsyncMock(
        return_value=LEGACY_SLIDESHOW_PAYLOAD
    )

    assert await device.async_get_art_settings() is not None
    assert await device.async_get_slideshow_state() == EXPECTED_LEGACY_SLIDESHOW

    device._art_session.generation = 4

    assert await device.async_get_art_settings() == EXPECTED_ART_SETTINGS
    assert await device.async_get_slideshow_state() == EXPECTED_MODERN_SLIDESHOW
    assert device._art.get_art_settings_payload.await_count == 2
    device._art.get_legacy_brightness.assert_awaited_once_with()
    device._art.get_legacy_color_temperature.assert_awaited_once_with()
    assert device._art.get_auto_rotation_status.await_count == 2
    device._art.get_legacy_slideshow_status.assert_awaited_once_with()


async def test_modern_slideshow_response_error_probes_legacy_once(device):
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=ResponseError("unsupported")
    )
    device._art.get_legacy_slideshow_status = AsyncMock(
        return_value=LEGACY_SLIDESHOW_PAYLOAD
    )

    first = await device.async_get_slideshow_state()
    second = await device.async_get_slideshow_state()

    assert first == second == EXPECTED_LEGACY_SLIDESHOW
    device._art.get_auto_rotation_status.assert_awaited_once_with()
    assert device._art.get_legacy_slideshow_status.await_count == 2
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_two_correlated_slideshow_errors_cache_unsupported(device):
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=ResponseError("modern unsupported")
    )
    device._art.get_legacy_slideshow_status = AsyncMock(
        side_effect=ResponseError("legacy unsupported")
    )

    assert await device.async_get_slideshow_state() is None
    assert await device.async_get_slideshow_state() is None

    device._art.get_auto_rotation_status.assert_awaited_once_with()
    device._art.get_legacy_slideshow_status.assert_awaited_once_with()
    assert device._slideshow_dialect.value == "unsupported"
    device._art_session.async_connection_failed.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "delegate"),
    [
        ("async_get_art_settings", "get_art_settings_payload"),
        ("async_get_slideshow_state", "get_auto_rotation_status"),
    ],
)
async def test_optional_background_reads_never_ensure_ready(
    device, method, delegate
):
    device._art_session.ready = False
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    assert await getattr(device, method)() is None

    device._art_session.async_ensure_ready.assert_not_awaited()
    operation.assert_not_awaited()


async def test_correlated_invalid_legacy_value_advertises_support(device):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=ResponseError("unsupported")
    )
    device._art.get_legacy_brightness = AsyncMock(return_value="invalid")
    device._art.get_legacy_color_temperature = AsyncMock(
        side_effect=ResponseError("unsupported")
    )

    assert await device.async_get_art_settings() == ArtSettingsSnapshot(
        supported=frozenset({ArtSettingKey.BRIGHTNESS}),
        brightness=None,
    )
    device._art_session.async_connection_failed.assert_not_awaited()


@pytest.mark.parametrize(
    ("failed_getter", "failure_kind"),
    [
        ("get_legacy_brightness", "transport"),
        ("get_legacy_color_temperature", "transport"),
        ("get_legacy_brightness", "generation"),
        ("get_legacy_color_temperature", "generation"),
    ],
)
async def test_legacy_read_failure_returns_none_not_unsupported_snapshot(
    device, failed_getter, failure_kind
):
    device._art.get_art_settings_payload = AsyncMock(
        side_effect=ResponseError("aggregate unsupported")
    )
    error = OSError("lost")

    async def failure():
        if failure_kind == "transport":
            raise error
        device._art_session.generation += 1
        return "7"

    device._art.get_legacy_brightness = AsyncMock(return_value="7")
    device._art.get_legacy_color_temperature = AsyncMock(return_value="-2")
    setattr(device._art, failed_getter, AsyncMock(side_effect=failure))

    assert await device.async_get_art_settings() is None

    if failure_kind == "transport":
        device._art_session.async_connection_failed.assert_awaited_once_with(
            error
        )
    else:
        device._art_session.async_connection_failed.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "delegate", "payload", "expected", "dialect_attr"),
    [
        (
            "async_get_art_settings",
            "get_art_settings_payload",
            VALID_ART_SETTINGS_PAYLOAD,
            EXPECTED_ART_SETTINGS,
            "_art_settings_dialect",
        ),
        (
            "async_get_slideshow_state",
            "get_auto_rotation_status",
            MODERN_SLIDESHOW_PAYLOAD,
            EXPECTED_MODERN_SLIDESHOW,
            "_slideshow_dialect",
        ),
    ],
)
async def test_generation_loss_discards_optional_result_without_cache_write(
    device, method, delegate, payload, expected, dialect_attr
):
    calls = 0

    async def lose_generation_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            device._art_session.generation += 1
        return payload

    operation = AsyncMock(side_effect=lose_generation_once)
    setattr(device._art, delegate, operation)
    device._art.get_legacy_brightness = AsyncMock()
    device._art.get_legacy_color_temperature = AsyncMock()
    device._art.get_legacy_slideshow_status = AsyncMock()

    assert await getattr(device, method)() is None
    assert getattr(device, dialect_attr).value == "unknown"
    assert await getattr(device, method)() == expected
    assert operation.await_count == 2


@pytest.mark.parametrize(
    ("method", "delegate", "payload", "expected", "legacy_delegate"),
    [
        (
            "async_get_art_settings",
            "get_art_settings_payload",
            VALID_ART_SETTINGS_PAYLOAD,
            EXPECTED_ART_SETTINGS,
            "get_legacy_brightness",
        ),
        (
            "async_get_slideshow_state",
            "get_auto_rotation_status",
            MODERN_SLIDESHOW_PAYLOAD,
            EXPECTED_MODERN_SLIDESHOW,
            "get_legacy_slideshow_status",
        ),
    ],
)
async def test_correlated_error_after_generation_loss_does_not_select_fallback(
    device, method, delegate, payload, expected, legacy_delegate
):
    calls = 0

    async def lose_generation_with_response_error():
        nonlocal calls
        calls += 1
        if calls == 1:
            device._art_session.generation += 1
            raise ResponseError("unsupported on stale generation")
        return payload

    operation = AsyncMock(side_effect=lose_generation_with_response_error)
    legacy = AsyncMock()
    setattr(device._art, delegate, operation)
    setattr(device._art, legacy_delegate, legacy)
    device._art.get_legacy_brightness = (
        legacy
        if legacy_delegate == "get_legacy_brightness"
        else AsyncMock()
    )
    device._art.get_legacy_color_temperature = AsyncMock()
    device._art.get_legacy_slideshow_status = (
        legacy
        if legacy_delegate == "get_legacy_slideshow_status"
        else AsyncMock()
    )

    assert await getattr(device, method)() is None
    assert await getattr(device, method)() == expected
    assert operation.await_count == 2
    legacy.assert_not_awaited()


async def test_malformed_modern_slideshow_retains_proven_dialect(device):
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=[{"value": "invalid"}, MODERN_SLIDESHOW_PAYLOAD]
    )
    device._art.get_legacy_slideshow_status = AsyncMock()

    assert await device.async_get_slideshow_state() is None
    assert device._slideshow_dialect.value == "auto_rotation"
    assert await device.async_get_slideshow_state() == EXPECTED_MODERN_SLIDESHOW
    assert device._art.get_auto_rotation_status.await_count == 2
    device._art.get_legacy_slideshow_status.assert_not_awaited()


async def test_malformed_legacy_slideshow_retains_proven_dialect(device):
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=ResponseError("unsupported")
    )
    device._art.get_legacy_slideshow_status = AsyncMock(
        side_effect=[{"value": "invalid"}, LEGACY_SLIDESHOW_PAYLOAD]
    )

    assert await device.async_get_slideshow_state() is None
    assert device._slideshow_dialect.value == "legacy"
    assert await device.async_get_slideshow_state() == EXPECTED_LEGACY_SLIDESHOW
    device._art.get_auto_rotation_status.assert_awaited_once_with()
    assert device._art.get_legacy_slideshow_status.await_count == 2


async def test_slideshow_transport_failure_keeps_capability_unknown(device):
    error = TimeoutError()
    device._art.get_auto_rotation_status = AsyncMock(
        side_effect=[error, MODERN_SLIDESHOW_PAYLOAD]
    )
    device._art.get_legacy_slideshow_status = AsyncMock()

    assert await device.async_get_slideshow_state() is None
    assert device._slideshow_dialect.value == "unknown"
    assert await device.async_get_slideshow_state() == EXPECTED_MODERN_SLIDESHOW
    assert device._art.get_auto_rotation_status.await_count == 2
    device._art.get_legacy_slideshow_status.assert_not_awaited()
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


@pytest.mark.parametrize(
    ("method", "argument", "delegate", "delegate_argument"),
    [
        (
            "async_set_motion_timer",
            "15",
            "set_motion_timer",
            "15",
        ),
        (
            "async_set_motion_sensitivity",
            "2",
            "set_motion_sensitivity",
            "2",
        ),
        (
            "async_set_brightness_sensor",
            True,
            "set_brightness_sensor_setting",
            True,
        ),
    ],
)
async def test_optional_setting_mutations_ensure_user_once(
    device, method, argument, delegate, delegate_argument
):
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    await getattr(device, method)(argument)

    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    operation.assert_awaited_once_with(delegate_argument)


@pytest.mark.parametrize(
    ("method", "argument", "message"),
    [
        (
            "async_set_motion_timer",
            "10",
            "Invalid motion timer",
        ),
        (
            "async_set_motion_sensitivity",
            "0",
            "Invalid motion sensitivity",
        ),
        (
            "async_set_brightness_sensor",
            "on",
            "Brightness sensor state must be boolean",
        ),
    ],
)
async def test_invalid_optional_setting_does_not_fail_healthy_session(
    device, method, argument, message
):
    device._art.request = AsyncMock()

    with pytest.raises(ValueError, match=f"^{message}$") as raised:
        await getattr(device, method)(argument)

    assert type(raised.value).__name__ == "InvalidArtSettingError"
    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    device._art.request.assert_not_awaited()
    device._art_session.async_connection_failed.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ValueError("invalid protocol value"), id="value-error"),
        pytest.param(
            json.JSONDecodeError("invalid response", "{", 0),
            id="json-decode-error",
        ),
    ],
)
async def test_non_setting_value_error_reports_mutation_connection_failure(
    device, error
):
    device._art.upload = AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as raised:
        await device.async_upload_art(b"image", "jpg", "none")

    assert raised.value is error
    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    device._art.upload.assert_awaited_once_with(b"image", "jpg", "none")
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


@pytest.mark.parametrize(
    ("method", "argument", "delegate"),
    [
        ("async_set_motion_timer", "15", "set_motion_timer"),
        (
            "async_set_motion_sensitivity",
            "2",
            "set_motion_sensitivity",
        ),
        (
            "async_set_brightness_sensor",
            True,
            "set_brightness_sensor_setting",
        ),
    ],
)
async def test_optional_setting_response_error_keeps_session_healthy(
    device, method, argument, delegate
):
    error = ResponseError("command rejected")
    operation = AsyncMock(side_effect=error)
    setattr(device._art, delegate, operation)

    with pytest.raises(ResponseError) as raised:
        await getattr(device, method)(argument)

    assert raised.value is error
    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    operation.assert_awaited_once_with(argument)
    device._art_session.async_connection_failed.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "argument", "delegate"),
    [
        ("async_set_motion_timer", "15", "set_motion_timer"),
        (
            "async_set_motion_sensitivity",
            "2",
            "set_motion_sensitivity",
        ),
        (
            "async_set_brightness_sensor",
            True,
            "set_brightness_sensor_setting",
        ),
    ],
)
@pytest.mark.parametrize("error", [TimeoutError(), OSError("lost")])
async def test_optional_setting_transport_failure_reports_without_retry(
    device, method, argument, delegate, error
):
    operation = AsyncMock(side_effect=error)
    setattr(device._art, delegate, operation)

    with pytest.raises(type(error)) as raised:
        await getattr(device, method)(argument)

    assert raised.value is error
    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    operation.assert_awaited_once_with(argument)
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


def test_numeric_compatibility_getters_are_removed():
    assert not hasattr(FrameDevice, "async_get_art_brightness")
    assert not hasattr(FrameDevice, "async_get_color_temperature")


@pytest.mark.parametrize(
    ("method", "args", "delegate"),
    [
        ("async_get_artmode", (), "get_artmode"),
        ("async_get_current_art", (), "get_current"),
        ("async_get_art_thumbnail", ("MY_F0001",), "get_thumbnail"),
        ("async_get_art_settings", (), "get_art_settings_payload"),
        ("async_get_slideshow_state", (), "get_auto_rotation_status"),
    ],
)
async def test_art_getters_return_none_without_opening_after_stop(
    device, method, args, delegate
):
    device._stopped = True
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    assert await getattr(device, method)(*args) is None
    operation.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "args", "delegate"),
    [
        ("async_set_artmode", (True,), "set_artmode"),
        ("async_set_art_brightness", (6,), "set_brightness"),
        ("async_select_art", ("MY_F0001", True), "select_image"),
        ("async_upload_art", (b"image", "jpg", "none"), "upload"),
        ("async_delete_art", ("MY_F0001",), "delete"),
        ("async_change_matte", ("MY_F0001", "none"), "change_matte"),
        ("async_set_photo_filter", ("MY_F0001", "ink"), "set_photo_filter"),
        ("async_set_favourite", ("MY_F0001", True), "set_favourite"),
        ("async_set_color_temperature", (4,), "set_color_temperature"),
        ("async_set_slideshow", (60, False, "MY-C0002"), "set_slideshow"),
        ("async_set_motion_timer", ("15",), "set_motion_timer"),
        (
            "async_set_motion_sensitivity",
            ("2",),
            "set_motion_sensitivity",
        ),
        (
            "async_set_brightness_sensor",
            (True,),
            "set_brightness_sensor_setting",
        ),
    ],
)
async def test_art_mutations_fail_without_opening_after_stop(
    device, method, args, delegate
):
    device._stopped = True
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    with pytest.raises(ConnectionFailure, match="stopped"):
        await getattr(device, method)(*args)
    operation.assert_not_awaited()


async def test_get_artmode_does_not_retry_timeout(device):
    error = TimeoutError()
    device._art.get_artmode = AsyncMock(side_effect=error)
    assert await device.async_get_artmode() is None
    device._art.get_artmode.assert_awaited_once()
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


async def test_user_mutation_requests_one_user_probe_and_executes_once(device):
    device._art.set_artmode = AsyncMock()

    await device.async_set_artmode(True)

    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    device._art.set_artmode.assert_awaited_once_with(True)


async def test_user_mutation_failure_is_not_retried(device):
    error = OSError("dead socket")
    device._art.set_artmode = AsyncMock(side_effect=error)

    with pytest.raises(OSError) as raised:
        await device.async_set_artmode(True)

    assert raised.value is error
    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    device._art.set_artmode.assert_awaited_once_with(True)
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


async def test_user_mutation_response_error_keeps_session_ready(device):
    error = ResponseError("command rejected")
    device._art.set_artmode = AsyncMock(side_effect=error)

    with pytest.raises(ResponseError) as raised:
        await device.async_set_artmode(True)

    assert raised.value is error
    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    device._art.set_artmode.assert_awaited_once_with(True)
    device._art_session.async_connection_failed.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "args", "delegate", "delegate_args"),
    [
        ("async_set_artmode", (True,), "set_artmode", (True,)),
        ("async_set_art_brightness", (6,), "set_brightness", (6,)),
        (
            "async_select_art",
            ("MY_F0001", True),
            "select_image",
            ("MY_F0001", None, True),
        ),
        (
            "async_upload_art",
            (b"image", "jpg", "none"),
            "upload",
            (b"image", "jpg", "none"),
        ),
        ("async_delete_art", ("MY_F0001",), "delete", ("MY_F0001",)),
        (
            "async_change_matte",
            ("MY_F0001", "none"),
            "change_matte",
            ("MY_F0001", "none"),
        ),
        (
            "async_set_photo_filter",
            ("MY_F0001", "ink"),
            "set_photo_filter",
            ("MY_F0001", "ink"),
        ),
        (
            "async_set_favourite",
            ("MY_F0001", True),
            "set_favourite",
            ("MY_F0001", True),
        ),
        (
            "async_set_color_temperature",
            (4,),
            "set_color_temperature",
            (4,),
        ),
        (
            "async_set_slideshow",
            (60, False, "MY-C0002"),
            "set_slideshow",
            (60, False, "MY-C0002"),
        ),
        (
            "async_set_motion_timer",
            ("15",),
            "set_motion_timer",
            ("15",),
        ),
        (
            "async_set_motion_sensitivity",
            ("2",),
            "set_motion_sensitivity",
            ("2",),
        ),
        (
            "async_set_brightness_sensor",
            (True,),
            "set_brightness_sensor_setting",
            (True,),
        ),
    ],
)
async def test_all_art_mutations_ensure_user_and_execute_once(
    device, method, args, delegate, delegate_args
):
    operation = AsyncMock(return_value="MY_F0100")
    setattr(device._art, delegate, operation)

    await getattr(device, method)(*args)

    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    operation.assert_awaited_once_with(*delegate_args)


@pytest.mark.parametrize(
    ("method", "args", "delegate"),
    [
        ("async_set_artmode", (True,), "set_artmode"),
        ("async_set_art_brightness", (6,), "set_brightness"),
        ("async_select_art", ("MY_F0001", True), "select_image"),
        ("async_upload_art", (b"image", "jpg", "none"), "upload"),
        ("async_delete_art", ("MY_F0001",), "delete"),
        ("async_change_matte", ("MY_F0001", "none"), "change_matte"),
        (
            "async_set_photo_filter",
            ("MY_F0001", "ink"),
            "set_photo_filter",
        ),
        ("async_set_favourite", ("MY_F0001", True), "set_favourite"),
        ("async_set_color_temperature", (4,), "set_color_temperature"),
        (
            "async_set_slideshow",
            (60, False, "MY-C0002"),
            "set_slideshow",
        ),
        ("async_set_motion_timer", ("15",), "set_motion_timer"),
        (
            "async_set_motion_sensitivity",
            ("2",),
            "set_motion_sensitivity",
        ),
        (
            "async_set_brightness_sensor",
            (True,),
            "set_brightness_sensor_setting",
        ),
    ],
)
async def test_unavailable_art_mutations_do_not_execute(
    device, method, args, delegate
):
    device._art_session.async_ensure_ready.return_value = False
    operation = AsyncMock()
    setattr(device._art, delegate, operation)

    with pytest.raises(ConnectionFailure, match="Art session is unavailable"):
        await getattr(device, method)(*args)

    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    operation.assert_not_awaited()
    device._art_session.async_connection_failed.assert_not_awaited()


async def test_newest_token_none_when_unchanged(hass, device):
    # All clients were constructed with the stored token ("tok").
    assert device.newest_token is None


async def test_newest_token_ignores_art_issued_token(hass, device):
    device._art.token = "fresh-token"
    assert device.newest_token is None


async def test_newest_token_reads_remote(hass, device):
    device._art.token = "tok"
    device._remote.token = "fresh-token"
    assert device.newest_token == "fresh-token"


async def test_update_token_is_used_by_both_persistent_clients(hass, device):
    device.update_token("fresh-token")
    assert device._art.token == "fresh-token"
    assert device._remote.token == "fresh-token"


async def test_art_callback_and_session_start_are_loop_native(hass, device):
    callback = AsyncMock()
    device._art.set_event_callback = MagicMock()
    device._art.start_listening = AsyncMock()
    with patch.object(hass, "async_add_executor_job") as executor:
        device.set_art_event_callback(callback)
        await device.async_start_art_session()
    device._art.set_event_callback.assert_called_once_with(callback)
    device._art_session.async_start.assert_awaited_once()
    device._art_session.async_ensure_ready.assert_not_awaited()
    device._art.start_listening.assert_not_awaited()
    executor.assert_not_called()


async def test_art_session_facade_properties_and_delegates(device):
    callback = MagicMock()

    assert device.art_ready is True
    assert device.art_generation == 3
    assert device.art_session_state is ArtSessionState.READY

    device.set_art_session_state_callback(callback)
    device._art_session.set_state_callback.assert_called_once_with(callback)
    device.set_art_session_state_callback(None)
    device._art_session.set_state_callback.assert_called_with(None)

    await device.async_start_art_session()
    device._art_session.async_start.assert_awaited_once()


def test_observe_art_power_delegates_without_awaiting_network(device):
    device.observe_art_power(True, "on", True)
    device._art_session.observe_power.assert_called_once_with(
        True, "on", True
    )


def test_temporary_art_compatibility_shims_are_removed(device):
    assert list(signature(FrameDevice.async_get_artmode).parameters) == [
        "self"
    ]
    assert not hasattr(type(device), "listener_alive")
    assert not hasattr(type(device), "async_start_art_listener")
    assert not hasattr(type(device), "async_restart_art_listener")


async def test_device_stop_stops_session_and_remote_once(device):
    release_shutdown = asyncio.Event()
    session_started = asyncio.Event()
    remote_started = asyncio.Event()

    async def stop_session():
        session_started.set()
        await release_shutdown.wait()

    async def close_remote():
        remote_started.set()
        await release_shutdown.wait()

    device._art_session.async_stop = AsyncMock(side_effect=stop_session)
    device._remote.async_stop = AsyncMock(side_effect=close_remote)

    cancelled_waiter = asyncio.create_task(device.async_stop())
    surviving_waiter = None
    try:
        await asyncio.wait_for(session_started.wait(), timeout=0.05)
        await asyncio.wait_for(remote_started.wait(), timeout=0.05)
        surviving_waiter = asyncio.create_task(device.async_stop())
        await asyncio.sleep(0)

        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        assert not surviving_waiter.done()
    finally:
        release_shutdown.set()
        await asyncio.gather(
            cancelled_waiter,
            *([surviving_waiter] if surviving_waiter is not None else []),
            return_exceptions=True,
        )

    await device.async_stop()

    device._art_session.async_stop.assert_awaited_once()
    device._remote.async_stop.assert_awaited_once()
    assert device._stopped is True
    assert len(device._test_task_factory_calls) == 1


async def test_stop_admission_closes_before_owner_first_runs(device):
    release_owner = asyncio.Event()
    owner_created = asyncio.Event()

    def delayed_task_factory(coroutine, name):
        async def run_later():
            await release_owner.wait()
            await coroutine

        task = asyncio.create_task(run_later(), name=name)
        owner_created.set()
        return task

    device._task_factory = delayed_task_factory
    device._art.set_artmode = AsyncMock()
    stop_waiter = asyncio.create_task(device.async_stop())
    await owner_created.wait()

    try:
        with pytest.raises(ConnectionFailure, match="stopped"):
            await device.async_set_artmode(True)
        device._art.set_artmode.assert_not_awaited()
        assert device._stopped is True
    finally:
        release_owner.set()
        await asyncio.gather(stop_waiter, return_exceptions=True)


async def test_cancelled_stop_owner_finishes_shared_cleanup(device):
    release_shutdown = asyncio.Event()
    session_started = asyncio.Event()
    remote_started = asyncio.Event()

    async def stop_session():
        session_started.set()
        await release_shutdown.wait()

    async def close_remote():
        remote_started.set()
        await release_shutdown.wait()

    device._art_session.async_stop = AsyncMock(side_effect=stop_session)
    device._remote.async_stop = AsyncMock(side_effect=close_remote)
    first_waiter = asyncio.create_task(device.async_stop())
    await session_started.wait()
    await remote_started.wait()
    owner = device._stop_task
    assert owner is not None

    owner.cancel()
    await asyncio.sleep(0)
    release_shutdown.set()
    first_result = await asyncio.gather(
        first_waiter, return_exceptions=True
    )
    second_result = await asyncio.wait_for(
        asyncio.gather(device.async_stop(), return_exceptions=True),
        timeout=0.1,
    )

    assert first_result == [None]
    assert second_result == [None]
    device._art_session.async_stop.assert_awaited_once()
    device._remote.async_stop.assert_awaited_once()


async def test_stop_recovers_if_owner_is_cancelled_before_start(device):
    task_factory_calls = 0

    def cancel_first_owner(coroutine, name):
        nonlocal task_factory_calls
        task_factory_calls += 1
        task = asyncio.create_task(coroutine, name=name)
        if task_factory_calls == 1:
            task.cancel()
        return task

    device._task_factory = cancel_first_owner
    device._remote.async_stop = AsyncMock()

    await asyncio.wait_for(device.async_stop(), timeout=0.1)

    assert task_factory_calls == 2
    device._art_session.async_stop.assert_awaited_once()
    device._remote.async_stop.assert_awaited_once()


async def test_stop_task_factory_failure_does_not_close_admission(device):
    def broken_task_factory(_coroutine, _name):
        raise RuntimeError("factory failed")

    device._task_factory = broken_task_factory
    device._remote.async_stop = AsyncMock()
    with pytest.raises(RuntimeError, match="factory failed"):
        await device.async_stop()

    assert device._stopped is False
    assert device._stop_task is None
    device._art_session.async_stop.assert_not_awaited()
    device._remote.async_stop.assert_not_awaited()


async def test_stop_bounds_wedged_remote_terminal_stop_once(device):
    never_finishes = asyncio.Event()
    device._remote.async_stop = AsyncMock(side_effect=never_finishes.wait)

    with patch(
        "custom_components.samsungtv_frame.device.REMOTE_CLOSE_DEADLINE",
        0.01,
    ):
        await asyncio.wait_for(device.async_stop(), timeout=0.1)
        await asyncio.wait_for(device.async_stop(), timeout=0.1)

    device._art_session.async_stop.assert_awaited_once()
    device._remote.async_stop.assert_awaited_once()


async def test_send_key_clicks_remote(hass, device):
    remote = MagicMock()
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")
    cmds = remote.send_commands.call_args.args[0]
    assert cmds[0].params["DataOfCmd"] == "KEY_HOME"
    assert cmds[0].params["Cmd"] == "Click"
    remote.open.assert_awaited_once_with()


async def test_remote_timeout_requests_reauth_without_retry(hass, device):
    remote = MagicMock()
    error = RemotePairingRequired("remote authorization required")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(side_effect=error)
    remote.close = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(RemotePairingRequired) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert raised.value is error
    remote.send_commands.assert_awaited_once()
    remote.close.assert_not_awaited()
    reauth.assert_called_once_with()
    assert device.remote_confirmed is False


async def test_remote_open_timeout_requests_reauth_without_send(hass, device):
    remote = MagicMock(token="tok")
    error = RemotePairingRequired("remote authorization required")
    remote.open = AsyncMock(side_effect=error)
    remote.send_commands = AsyncMock()
    remote.close = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(RemotePairingRequired) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert raised.value is error
    remote.open.assert_awaited_once_with()
    remote.send_commands.assert_not_awaited()
    remote.close.assert_not_awaited()
    reauth.assert_called_once_with()
    assert device.remote_confirmed is False


async def test_remote_open_unauthorized_requests_reauth_without_retry(
    hass, device, caplog
):
    private_error = "private-open-unauthorized-sentinel"
    remote = MagicMock(token="tok")
    remote.open = AsyncMock(side_effect=UnauthorizedError(private_error))
    remote.send_commands = AsyncMock()
    remote.close = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)
    device.remote_confirmed = True

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(UnauthorizedError) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert str(raised.value) == "Remote authorization rejected"
    assert raised.value.__suppress_context__ is True
    assert raised.value.__cause__ is None
    assert private_error not in caplog.text
    remote.open.assert_awaited_once_with()
    remote.send_commands.assert_not_awaited()
    remote.close.assert_not_awaited()
    reauth.assert_called_once_with()
    assert device.remote_confirmed is False


async def test_remote_send_unauthorized_requests_reauth_without_retry(
    hass, device, caplog
):
    private_error = "private-send-unauthorized-sentinel"
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(
        side_effect=UnauthorizedError(private_error)
    )
    remote.close = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)
    device.remote_confirmed = True

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(UnauthorizedError) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert str(raised.value) == "Remote authorization rejected"
    assert raised.value.__suppress_context__ is True
    assert raised.value.__cause__ is None
    assert private_error not in caplog.text
    remote.open.assert_awaited_once_with()
    remote.send_commands.assert_awaited_once()
    remote.close.assert_not_awaited()
    reauth.assert_called_once_with()
    assert device.remote_confirmed is False


async def test_remote_timeout_on_stale_retry_requests_reauth(hass, device):
    remote = MagicMock()
    error = RemotePairingRequired("remote authorization required")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(side_effect=[OSError("stale"), error])
    remote.close = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(RemotePairingRequired) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert raised.value is error
    assert remote.send_commands.await_count == 2
    remote.close.assert_awaited_once()
    reauth.assert_called_once_with()
    assert device.remote_confirmed is False


async def test_remote_timeout_on_retry_open_requests_reauth_without_send(
    hass, device
):
    remote = MagicMock(token="tok")
    error = RemotePairingRequired("remote authorization required")
    remote.open = AsyncMock(side_effect=[OSError("stale"), error])
    remote.send_commands = AsyncMock()
    remote.close = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(RemotePairingRequired) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert raised.value is error
    assert remote.open.await_count == 2
    remote.send_commands.assert_not_awaited()
    remote.close.assert_awaited_once()
    reauth.assert_called_once_with()
    assert device.remote_confirmed is False


async def test_generic_remote_open_failure_retries_once(hass, device):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock(side_effect=[OSError("stale"), None])
    remote.send_commands = AsyncMock()
    remote.close = AsyncMock()
    device.remote_confirmed = True

    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")

    assert remote.open.await_count == 2
    remote.send_commands.assert_awaited_once()
    remote.close.assert_awaited_once()
    assert device.remote_confirmed is True


async def test_initial_remote_open_cancellation_propagates_without_callbacks(
    hass, device
):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock(side_effect=asyncio.CancelledError())
    remote.send_commands = AsyncMock()
    remote.close = AsyncMock()
    reauth = MagicMock()
    persist = MagicMock()
    device.set_remote_reauth_callback(reauth)
    device.set_remote_token_callback(persist)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(asyncio.CancelledError),
    ):
        await device.async_send_key("KEY_HOME")

    remote.open.assert_awaited_once_with()
    remote.send_commands.assert_not_awaited()
    remote.close.assert_not_awaited()
    reauth.assert_not_called()
    persist.assert_not_called()


async def test_initial_remote_send_cancellation_propagates_without_callbacks(
    hass, device
):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(side_effect=asyncio.CancelledError())
    remote.close = AsyncMock()
    reauth = MagicMock()
    persist = MagicMock()
    device.set_remote_reauth_callback(reauth)
    device.set_remote_token_callback(persist)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(asyncio.CancelledError),
    ):
        await device.async_send_key("KEY_HOME")

    remote.send_commands.assert_awaited_once()
    remote.close.assert_not_awaited()
    reauth.assert_not_called()
    persist.assert_not_called()


async def test_stale_remote_close_cancellation_propagates_without_callbacks(
    hass, device
):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(side_effect=[OSError("stale"), None])
    remote.close = AsyncMock(side_effect=asyncio.CancelledError())
    reauth = MagicMock()
    persist = MagicMock()
    device.set_remote_reauth_callback(reauth)
    device.set_remote_token_callback(persist)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(asyncio.CancelledError),
    ):
        await device.async_send_key("KEY_HOME")

    remote.send_commands.assert_awaited_once()
    remote.close.assert_awaited_once()
    reauth.assert_not_called()
    persist.assert_not_called()
    assert device.remote_confirmed is False


async def test_stale_remote_close_failure_is_sanitized_without_retry(
    hass, device
):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(side_effect=OSError("private-stale-send"))
    remote.close = AsyncMock(side_effect=OSError("private-reset-failure"))
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(ConnectionFailure) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert str(raised.value) == "Remote control reset failed"
    assert raised.value.__suppress_context__ is True
    remote.open.assert_awaited_once_with()
    remote.send_commands.assert_awaited_once()
    remote.close.assert_awaited_once()
    reauth.assert_not_called()
    assert device.remote_confirmed is False


async def test_second_remote_send_cancellation_propagates_without_callbacks(
    hass, device
):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(
        side_effect=[OSError("stale"), asyncio.CancelledError()]
    )
    remote.close = AsyncMock()
    reauth = MagicMock()
    persist = MagicMock()
    device.set_remote_reauth_callback(reauth)
    device.set_remote_token_callback(persist)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(asyncio.CancelledError),
    ):
        await device.async_send_key("KEY_HOME")

    assert remote.send_commands.await_count == 2
    remote.close.assert_awaited_once()
    reauth.assert_not_called()
    persist.assert_not_called()
    assert device.remote_confirmed is False


async def test_stale_remote_retry_closes_captured_client_once(hass, device):
    remote = MagicMock()
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(side_effect=[OSError("stale"), None])
    remote.close = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_VOLUP")
    assert remote.send_commands.await_count == 2
    assert remote.open.await_count == 2
    remote.close.assert_awaited_once()


async def test_successful_remote_command_persists_new_token_before_return(
    hass, device
):
    order = []
    remote = MagicMock(token="new-token")
    remote.open = AsyncMock(side_effect=lambda: order.append("opened"))
    remote.send_commands = AsyncMock(
        side_effect=lambda commands: order.append("sent")
    )
    persist = MagicMock(
        side_effect=lambda token: order.append("persisted")
    )
    device.set_remote_token_callback(persist)

    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")
        order.append("returned")

    assert order == ["opened", "persisted", "sent", "returned"]
    persist.assert_called_once_with("new-token")
    assert device.remote_confirmed is True


@pytest.mark.parametrize("token", [None, "tok"])
async def test_successful_remote_command_skips_absent_or_unchanged_token(
    hass, device, token
):
    remote = MagicMock(token=token)
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock()
    persist = MagicMock()
    device.set_remote_token_callback(persist)

    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")

    persist.assert_not_called()


async def test_remote_token_callback_failure_fails_foreground_command(
    hass, device
):
    remote = MagicMock(token="new-token")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock()
    remote.close = AsyncMock()
    persist_error = RuntimeError("persistence failed")
    persist = MagicMock(side_effect=persist_error)
    reauth = MagicMock()
    device.set_remote_token_callback(persist)
    device.set_remote_reauth_callback(reauth)

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(RuntimeError, match="persistence failed") as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert raised.value is persist_error
    remote.open.assert_awaited_once_with()
    persist.assert_called_once_with("new-token")
    remote.send_commands.assert_not_awaited()
    remote.close.assert_not_awaited()
    reauth.assert_not_called()


async def test_stale_retry_persists_new_token_before_second_send(hass, device):
    order = []
    remote = MagicMock(token="tok")
    open_count = 0

    async def _open():
        nonlocal open_count
        open_count += 1
        order.append(f"open-{open_count}")
        if open_count == 2:
            remote.token = "new-token"

    send_count = 0

    async def _send(_commands):
        nonlocal send_count
        send_count += 1
        order.append(f"send-{send_count}")
        if send_count == 1:
            raise OSError("stale")

    remote.open = AsyncMock(side_effect=_open)
    remote.send_commands = AsyncMock(side_effect=_send)
    remote.close = AsyncMock(side_effect=lambda: order.append("closed"))

    def _persist(token):
        order.append("persisted")
        device.update_token(token)

    device.set_remote_token_callback(MagicMock(side_effect=_persist))

    with patch.object(device, "_remote", remote):
        await device.async_send_key("KEY_HOME")

    assert order == [
        "open-1",
        "send-1",
        "closed",
        "open-2",
        "persisted",
        "send-2",
    ]
    assert device.remote_confirmed is True


async def test_second_generic_remote_failure_stays_unconfirmed(hass, device):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(
        side_effect=[OSError("stale"), OSError("still stale")]
    )
    remote.close = AsyncMock()

    device.remote_confirmed = True
    with (
        patch.object(device, "_remote", remote),
        pytest.raises(ConnectionFailure) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert str(raised.value) == "Remote control connection failed"
    assert remote.open.await_count == 2
    assert remote.send_commands.await_count == 2
    remote.close.assert_awaited_once()
    assert device.remote_confirmed is False


async def test_final_generic_remote_failure_is_sanitized(hass, device, caplog):
    secret = "private-final-network-failure"
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(
        side_effect=[OSError("stale"), OSError(secret)]
    )
    remote.close = AsyncMock()

    with (
        patch.object(device, "_remote", remote),
        pytest.raises(ConnectionFailure) as raised,
    ):
        await device.async_send_key("KEY_HOME")

    assert str(raised.value) == "Remote control connection failed"
    assert raised.value.__suppress_context__ is True
    assert secret not in caplog.text
    assert device.remote_confirmed is False


async def test_app_list_is_inert_and_never_delays_foreground_remote_work(
    hass, device
):
    remote = device._remote
    assert remote is not None
    websocket = MagicMock()
    websocket.send = AsyncMock()
    websocket.recv = AsyncMock(
        side_effect=AssertionError("app discovery must not receive frames")
    )
    remote.connection = websocket
    remote.key_press_delay = 0
    existing_future = asyncio.get_running_loop().create_future()
    remote._app_list_futures.add(existing_future)
    futures_before = set(remote._app_list_futures)
    persist = MagicMock()
    reauth = MagicMock()
    device.set_remote_token_callback(persist)
    device.set_remote_reauth_callback(reauth)
    device.remote_confirmed = True

    app_list_task = asyncio.create_task(device.async_app_list())
    command_task = asyncio.create_task(device.async_send_key("KEY_HOME"))
    try:
        app_list_result, command_result = await asyncio.wait_for(
            asyncio.gather(app_list_task, command_task), timeout=0.05
        )

        assert app_list_result is None
        assert command_result is None
        websocket.send.assert_awaited_once()
        payload = websocket.send.await_args.args[0]
        assert "ms.remote.control" in payload
        assert "ed.installedApp.get" not in payload
        websocket.recv.assert_not_awaited()
        assert remote._app_list_futures == futures_before
        assert not existing_future.done()
        assert remote.connection is websocket
        assert not device._remote_operation_lock.locked()
        assert device._active_remote_operation is None
        assert device.remote_confirmed is True
        assert device._token == "tok"
        assert remote.token == "tok"
        persist.assert_not_called()
        reauth.assert_not_called()
    finally:
        for task in (app_list_task, command_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            app_list_task, command_task, return_exceptions=True
        )
        for future in remote._app_list_futures:
            future.cancel()
        remote._app_list_futures.clear()


async def test_remote_quiesce_waits_for_operation_and_blocks_post_open_effects(
    hass, device
):
    open_started = asyncio.Event()
    release_open = asyncio.Event()
    remote = MagicMock(token="new-token")

    async def _open():
        open_started.set()
        await release_open.wait()

    remote.open = AsyncMock(side_effect=_open)
    remote.send_commands = AsyncMock()
    persist = MagicMock(side_effect=device.update_token)
    reauth = MagicMock()
    device.set_remote_token_callback(persist)
    device.set_remote_reauth_callback(reauth)

    with patch.object(device, "_remote", remote):
        command = asyncio.create_task(device.async_send_key("KEY_HOME"))
        await open_started.wait()
        quiesce = asyncio.create_task(device.async_quiesce_remote())
        await asyncio.sleep(0)
        assert not quiesce.done()
        release_open.set()

        with pytest.raises(ConnectionFailure, match="Remote control is unavailable"):
            await command
        await quiesce

    remote.send_commands.assert_not_awaited()
    persist.assert_called_once_with("new-token")
    reauth.assert_not_called()
    assert device._token == "new-token"
    assert device._art.token == "new-token"
    assert remote.token == "new-token"


async def test_remote_quiesce_cancels_long_running_operation(hass, device):
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()
    never_finishes = asyncio.Event()
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()

    async def _send(_commands):
        send_started.set()
        try:
            await never_finishes.wait()
        finally:
            send_cancelled.set()

    remote.send_commands = AsyncMock(side_effect=_send)
    command = None
    with (
        patch.object(device, "_remote", remote),
        patch(
            "custom_components.samsungtv_frame.device.REMOTE_DRAIN_DEADLINE",
            0.01,
            create=True,
        ),
        patch(
            "custom_components.samsungtv_frame.device.REMOTE_CANCEL_DEADLINE",
            0.05,
            create=True,
        ),
    ):
        command = asyncio.create_task(device.async_send_key("KEY_HOME"))
        await send_started.wait()
        try:
            await asyncio.wait_for(
                device.async_quiesce_remote(), timeout=0.2
            )
        finally:
            if not command.done():
                command.cancel()
            await asyncio.gather(command, return_exceptions=True)

    assert send_cancelled.is_set()
    assert command.cancelled()


async def test_remote_resume_reopens_admission_after_reversible_quiesce(
    hass, device
):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock()

    with patch.object(device, "_remote", remote):
        await device.async_quiesce_remote()
        with pytest.raises(ConnectionFailure, match="Remote control is unavailable"):
            await device.async_send_key("KEY_HOME")
        device.resume_remote()
        await device.async_send_key("KEY_HOME")

    remote.open.assert_awaited_once_with()
    remote.send_commands.assert_awaited_once()


async def test_device_stop_joins_inflight_remote_before_terminal_stop(
    hass, device
):
    open_started = asyncio.Event()
    release_open = asyncio.Event()
    remote = MagicMock(token="new-token")

    async def _open():
        open_started.set()
        await release_open.wait()

    remote.open = AsyncMock(side_effect=_open)
    remote.send_commands = AsyncMock()
    remote.async_stop = AsyncMock()
    persist = MagicMock(side_effect=device.update_token)
    device.set_remote_token_callback(persist)

    with patch.object(device, "_remote", remote):
        command = asyncio.create_task(device.async_send_key("KEY_HOME"))
        await open_started.wait()
        stop = asyncio.create_task(device.async_stop())
        await asyncio.sleep(0)
        remote.async_stop.assert_not_awaited()
        release_open.set()

        with pytest.raises(ConnectionFailure, match="Remote control is unavailable"):
            await command
        await stop

    remote.send_commands.assert_not_awaited()
    persist.assert_called_once_with("new-token")
    assert device._token == "new-token"
    assert device._art.token == "new-token"
    assert remote.token == "new-token"
    remote.async_stop.assert_awaited_once_with()


async def test_device_stop_bounds_resistant_operation_and_aborts_remote_socket(
    hass, device
):
    send_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_send = asyncio.Event()

    class _Socket:
        state = State.OPEN

        def __init__(self):
            self.transport = MagicMock()

        async def send(self, _payload):
            send_started.set()
            try:
                await release_send.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_send.wait()

        async def close(self):
            raise OSError("close failed")

    socket = _Socket()
    remote = FrameRemote(
        "1.2.3.4",
        token="tok",
        ssl_context=MagicMock(),
        timeout=8,
    )
    remote.key_press_delay = 0
    remote.connection = socket

    with (
        patch.object(device, "_remote", remote),
        patch(
            "custom_components.samsungtv_frame.device.REMOTE_DRAIN_DEADLINE",
            0.01,
            create=True,
        ),
        patch(
            "custom_components.samsungtv_frame.device.REMOTE_CANCEL_DEADLINE",
            0.01,
            create=True,
        ),
        patch(
            "custom_components.samsungtv_frame.frame_remote.REMOTE_CLOSE_DEADLINE",
            0.05,
        ),
    ):
        command = asyncio.create_task(device.async_send_key("KEY_HOME"))
        await send_started.wait()
        try:
            await asyncio.wait_for(device.async_stop(), timeout=0.2)
            assert cancellation_seen.is_set()
            assert remote.connection is None
            socket.transport.abort.assert_called_once_with()
        finally:
            release_send.set()
            await asyncio.gather(command, return_exceptions=True)


async def test_remote_command_after_stop_has_zero_network_or_reauth(hass, device):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock()
    remote.async_stop = AsyncMock()
    reauth = MagicMock()
    device.set_remote_reauth_callback(reauth)

    with patch.object(device, "_remote", remote):
        await device.async_stop()
        with pytest.raises(ConnectionFailure, match="Remote control is unavailable"):
            await device.async_send_key("KEY_HOME")

    remote.open.assert_not_awaited()
    remote.send_commands.assert_not_awaited()
    reauth.assert_not_called()


async def test_launch_app_emits_channel_command(hass, device):
    remote = MagicMock()
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_launch_app("11101200001", "DEEP_LINK")
    cmds = remote.send_commands.call_args.args[0]
    assert cmds[0].params["event"] == "ed.apps.launch"
    assert cmds[0].params["data"]["appId"] == "11101200001"


async def test_set_slideshow_delegates_to_art(hass, device):
    device._art.set_slideshow = AsyncMock()
    await device.async_set_slideshow(60, True, "MY-C0002")
    device._art.set_slideshow.assert_awaited_once_with(60, True, "MY-C0002")


async def test_upload_art_returns_content_id(hass, device):
    device._art.upload = AsyncMock(return_value="MY_F0100")
    result = await device.async_upload_art(b"bytes", "jpg", "none")
    assert result == "MY_F0100"
    device._art.upload.assert_awaited_once_with(b"bytes", "jpg", "none")


async def test_upload_failure_is_not_retried(device):
    error = OSError("partial")
    device._art.upload = AsyncMock(side_effect=error)

    with pytest.raises(OSError) as raised:
        await device.async_upload_art(b"image", "jpg", "none")

    assert raised.value is error
    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    device._art.upload.assert_awaited_once()
    device._art_session.async_connection_failed.assert_awaited_once_with(error)


async def test_unavailable_upload_is_not_attempted(device):
    device._art_session.async_ensure_ready.return_value = False
    device._art.upload = AsyncMock()

    with pytest.raises(ConnectionFailure, match="Art session is unavailable"):
        await device.async_upload_art(b"image", "jpg", "none")

    device._art_session.async_ensure_ready.assert_awaited_once_with(
        ArtSessionTrigger.USER
    )
    device._art.upload.assert_not_awaited()


async def test_get_current_art_returns_content_id(hass, device):
    device._art.get_current = AsyncMock(
        return_value={"content_id": "MY_F0034", "matte_id": "none"}
    )
    assert await device.async_get_current_art() == "MY_F0034"


async def test_thumbnail_d2d_failure_does_not_reset_or_retry(device, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.samsungtv_frame")
    private_value = "private-thumbnail-error"
    private_content_id = "private-content-id"
    device._art.request = AsyncMock(
        return_value={
            "conn_info": {
                "ip": "10.0.0.8",
                "port": 4321,
                "secured": False,
            }
        }
    )
    device._art._open_d2d = AsyncMock(side_effect=OSError(private_value))
    device._art.close = AsyncMock()

    assert await device.async_get_art_thumbnail(private_content_id) is None
    device._art.request.assert_awaited_once()
    device._art._open_d2d.assert_awaited_once()
    device._art.close.assert_not_awaited()
    device._art_session.async_ensure_ready.assert_not_awaited()
    device._art_session.async_connection_failed.assert_not_awaited()
    assert private_value not in caplog.text
    assert private_content_id not in caplog.text
    assert "Thumbnail fetch failed" in caplog.text


async def test_all_art_operations_stay_off_executor(hass, device):
    device._art.get_artmode = AsyncMock(return_value="off")
    device._art.set_artmode = AsyncMock()
    device._art.get_current = AsyncMock(return_value={"content_id": "MY_F0001"})
    device._art.get_art_settings_payload = AsyncMock(
        return_value=VALID_ART_SETTINGS_PAYLOAD
    )
    device._art.get_legacy_brightness = AsyncMock()
    device._art.get_legacy_color_temperature = AsyncMock()
    device._art.get_auto_rotation_status = AsyncMock(
        return_value=MODERN_SLIDESHOW_PAYLOAD
    )
    device._art.get_legacy_slideshow_status = AsyncMock()
    device._art.set_brightness = AsyncMock()
    device._art.select_image = AsyncMock()
    device._art.upload = AsyncMock(return_value="MY_F0002")
    device._art.delete = AsyncMock()
    device._art.get_thumbnail = AsyncMock(return_value=b"jpeg")
    device._art.change_matte = AsyncMock()
    device._art.set_photo_filter = AsyncMock()
    device._art.set_favourite = AsyncMock()
    device._art.set_color_temperature = AsyncMock()
    device._art.set_slideshow = AsyncMock()
    device._art.set_motion_timer = AsyncMock()
    device._art.set_motion_sensitivity = AsyncMock()
    device._art.set_brightness_sensor_setting = AsyncMock()

    with patch.object(hass, "async_add_executor_job") as executor:
        await device.async_get_artmode()
        await device.async_set_artmode(True)
        await device.async_get_current_art()
        await device.async_get_art_settings()
        await device.async_get_slideshow_state()
        await device.async_set_art_brightness(6)
        await device.async_select_art("MY_F0001", True)
        await device.async_upload_art(b"image", "jpg", "none")
        await device.async_delete_art("MY_F0001")
        await device.async_get_art_thumbnail("MY_F0001")
        await device.async_change_matte("MY_F0001", "shadowbox_polar")
        await device.async_set_photo_filter("MY_F0001", "ink")
        await device.async_set_favourite("MY_F0001", True)
        await device.async_set_color_temperature(4)
        await device.async_set_slideshow(60, False, "MY-C0002")
        await device.async_set_motion_timer("15")
        await device.async_set_motion_sensitivity("2")
        await device.async_set_brightness_sensor(True)

    executor.assert_not_called()


def _mock_rendering_control(actions: dict) -> MagicMock:
    rc = MagicMock()
    rc.action.side_effect = lambda name: actions[name]
    return rc


def _mock_action(result) -> MagicMock:
    action = MagicMock()
    action.async_call = AsyncMock(return_value=result)
    return action


async def test_get_volume_via_upnp(hass, device):
    rc = _mock_rendering_control({
        "GetVolume": _mock_action({"CurrentVolume": 23}),
        "GetMute": _mock_action({"CurrentMute": False}),
    })
    with patch.object(device, "_async_rendering_control", AsyncMock(return_value=rc)):
        vol, mute = await device.async_get_volume()
    assert vol == 0.23
    assert mute is False


async def test_get_volume_failure_resets_upnp_device(hass, device, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.samsungtv_frame")
    private_value = "private-upnp-error"
    device._upnp_device = MagicMock()
    with patch.object(
        device,
        "_async_rendering_control",
        AsyncMock(side_effect=OSError(private_value)),
    ):
        assert await device.async_get_volume() == (None, None)
    assert device._upnp_device is None
    assert private_value not in caplog.text
    assert "UPnP volume query failed" in caplog.text


async def test_set_volume_scales_to_percent(hass, device):
    set_action = _mock_action({})
    rc = _mock_rendering_control({"SetVolume": set_action})
    with patch.object(device, "_async_rendering_control", AsyncMock(return_value=rc)):
        await device.async_set_volume(0.4)
    set_action.async_call.assert_awaited_once_with(
        InstanceID=0, Channel="Master", DesiredVolume=40
    )


async def test_turn_off_holds_power_key(hass, device):
    order = []
    remote = MagicMock(token="new-token")
    remote.open = AsyncMock(side_effect=lambda: order.append("opened"))

    async def _send(commands):
        order.append("sent")
        assert commands[0].params["DataOfCmd"] == "KEY_POWER"
        assert commands[0].params["Cmd"] == "Press"

    remote.send_commands = AsyncMock(side_effect=_send)
    persist = MagicMock(side_effect=lambda token: order.append("persisted"))
    device.set_remote_token_callback(persist)

    with patch.object(device, "_remote", remote):
        await device.async_turn_off()

    assert order == ["opened", "persisted", "sent"]
    persist.assert_called_once_with("new-token")
    remote.send_commands.assert_awaited_once()
    cmd = remote.send_commands.call_args.args[0]
    # Verify the command is a 3-second hold of KEY_POWER
    assert isinstance(cmd, list) and len(cmd) == 3
    assert cmd[0].params["DataOfCmd"] == "KEY_POWER"
    assert cmd[0].params["Cmd"] == "Press"
    assert cmd[1].delay == 3
    assert cmd[2].params["DataOfCmd"] == "KEY_POWER"
    assert cmd[2].params["Cmd"] == "Release"


@pytest.mark.parametrize("send_fails", [False, True])
async def test_turn_off_always_invalidates_remote_confirmation(
    hass, device, send_fails
):
    remote = MagicMock(token="tok")
    remote.open = AsyncMock()
    remote.send_commands = AsyncMock(
        side_effect=OSError("power send failed") if send_fails else None
    )
    remote.close = AsyncMock()
    device.remote_confirmed = True

    with patch.object(device, "_remote", remote):
        if send_fails:
            with pytest.raises(ConnectionFailure):
                await device.async_turn_off()
        else:
            await device.async_turn_off()

    assert device.remote_confirmed is False
