# tests/test_config_flow.py
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_MODEL, CONF_TOKEN, DOMAIN, OPT_HEARTBEAT,
)
from custom_components.samsungtv_frame.config_flow import (
    CannotConnect,
    validate_and_pair,
)


def _existing_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "1.2.3.4",
            CONF_MAC: "A0:D0:5B:86:CE:B7",
            CONF_TOKEN: "tok",
            CONF_MODEL: "QE65LS03BAUXXH",
        },
        unique_id="a0:d0:5b:86:ce:b7",
    )


def _pairing_patches(hass, art, ssl_context):
    rest = MagicMock()
    rest.rest_device_info = AsyncMock(
        return_value={
            "device": {
                "FrameTVSupport": "true",
                "wifiMac": "A0:D0:5B:86:CE:B7",
                "modelName": "QE65LS03BAUXXH",
            }
        }
    )
    return (
        patch(
            "custom_components.samsungtv_frame.config_flow.SamsungTVAsyncRest",
            return_value=rest,
        ),
        patch(
            "custom_components.samsungtv_frame.config_flow.FrameArt",
            return_value=art,
        ),
        patch(
            "custom_components.samsungtv_frame.config_flow.get_ssl_context",
            return_value=ssl_context,
        ),
        patch.object(
            hass,
            "async_add_executor_job",
            new=AsyncMock(side_effect=lambda func: func()),
        ),
    )


async def test_pair_always_closes(hass):
    ssl_context = object()
    art = MagicMock(token="new-token")
    art.open = AsyncMock()
    art.close = AsyncMock()
    art.start_listening = AsyncMock()
    rest_patch, art_patch, ssl_patch, executor_patch = _pairing_patches(
        hass, art, ssl_context
    )
    with (
        rest_patch,
        art_patch as art_cls,
        ssl_patch as get_context,
        executor_patch as executor,
    ):
        result = await validate_and_pair(hass, "1.2.3.4")

    assert result[CONF_TOKEN] == "new-token"
    executor.assert_awaited_once_with(get_context)
    art_cls.assert_called_once_with(
        "1.2.3.4",
        token=None,
        ssl_context=ssl_context,
        task_factory=None,
        event_callback=None,
        timeout=30,
    )
    art.open.assert_awaited_once()
    art.start_listening.assert_not_awaited()
    art.close.assert_awaited_once()


async def test_pair_open_failure_always_closes(hass):
    art = MagicMock(token=None)
    art.open = AsyncMock(side_effect=OSError("cannot open"))
    art.close = AsyncMock()
    rest_patch, art_patch, ssl_patch, executor_patch = _pairing_patches(
        hass, art, object()
    )
    with rest_patch, art_patch, ssl_patch, executor_patch as executor:
        with pytest.raises(CannotConnect):
            await validate_and_pair(hass, "1.2.3.4")

    assert executor.await_count == 1
    art.close.assert_awaited_once()


async def test_pair_cancellation_always_closes(hass):
    art = MagicMock(token=None)
    art.open = AsyncMock(side_effect=asyncio.CancelledError)
    art.close = AsyncMock()
    rest_patch, art_patch, ssl_patch, executor_patch = _pairing_patches(
        hass, art, object()
    )
    with rest_patch, art_patch, ssl_patch, executor_patch as executor:
        with pytest.raises(asyncio.CancelledError):
            await validate_and_pair(hass, "1.2.3.4")

    assert executor.await_count == 1
    art.close.assert_awaited_once()


async def test_user_flow_success(hass):
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={"mac": "A0:D0:5B:86:CE:B7",
                                    "token": "tok", "model": "QE65LS03BAUXXH"}),
    ), patch(
        # Prevent HA from running async_setup_entry after the flow creates
        # the config entry (avoids real socket connections and lingering tasks).
        "custom_components.samsungtv_frame.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["mac"] == "A0:D0:5B:86:CE:B7"
    assert result["data"]["token"] == "tok"


async def test_user_flow_not_a_frame(hass):
    from custom_components.samsungtv_frame.config_flow import NotAFrameError
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(side_effect=NotAFrameError),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "not_a_frame"}


async def test_user_flow_cannot_connect(hass):
    from custom_components.samsungtv_frame.config_flow import CannotConnect
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(side_effect=CannotConnect),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reconfigure_updates_host_keeps_token(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={
            # Re-pairing an already-granted client returns token None; the
            # stored token must survive.
            CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: None,
            CONF_MODEL: "QE65LS03BAUXXH",
        }),
    ), patch(
        "custom_components.samsungtv_frame.async_setup_entry", return_value=True
    ):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "5.6.7.8"
    assert entry.data[CONF_TOKEN] == "tok"


async def test_reconfigure_rejects_different_tv(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={
            CONF_MAC: "00:11:22:33:44:55", CONF_TOKEN: None, CONF_MODEL: "OTHER",
        }),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_HOST] == "1.2.3.4"


async def test_options_flow_sets_heartbeat(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.async_setup_entry", return_value=True
    ), patch(
        "custom_components.samsungtv_frame.async_unload_entry", return_value=True
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {OPT_HEARTBEAT: 30}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_HEARTBEAT] == 30
