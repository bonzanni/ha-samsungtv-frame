# tests/test_config_flow.py
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_MODEL,
    CONF_TOKEN,
    DOMAIN,
    OPT_HEARTBEAT,
    PAIRING_DEADLINE,
)
from custom_components.samsungtv_frame.config_flow import (
    CannotConnect,
    validate_and_pair,
)


RECONFIGURE_PAIRING_DESCRIPTION = (
    "Make sure the TV is showing normal TV or app content (not Art Mode). "
    "Enter the TV's new IP address, then approve the 'Allow' prompt on the TV "
    "when it appears."
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


@contextmanager
def pairing_patches(hass, *, remote, art, ssl_context):
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
    with (
        patch(
            "custom_components.samsungtv_frame.config_flow.SamsungTVAsyncRest",
            return_value=rest,
        ),
        patch(
            "custom_components.samsungtv_frame.config_flow.FrameRemote",
            return_value=remote,
        ),
        patch(
            "custom_components.samsungtv_frame.config_flow.FrameArt",
            return_value=art,
        ) as art_constructor,
        patch(
            "custom_components.samsungtv_frame.config_flow.get_ssl_context",
            return_value=ssl_context,
        ),
        patch.object(
            hass,
            "async_add_executor_job",
            new=AsyncMock(side_effect=lambda func: func()),
        ),
    ):
        yield art_constructor


async def test_pair_remote_token_is_used_to_validate_art(hass):
    ssl_context = object()
    remote = MagicMock(
        token="remote-token", open=AsyncMock(), close=AsyncMock()
    )
    art = MagicMock(
        token="ignored-art-token", open=AsyncMock(), close=AsyncMock()
    )
    with pairing_patches(
        hass, remote=remote, art=art, ssl_context=ssl_context
    ) as art_constructor:
        result = await validate_and_pair(hass, "1.2.3.4")

    assert result[CONF_TOKEN] == "remote-token"
    remote.open.assert_awaited_once()
    art_constructor.assert_called_once_with(
        "1.2.3.4",
        token="remote-token",
        ssl_context=ssl_context,
        task_factory=None,
        event_callback=None,
        timeout=PAIRING_DEADLINE,
    )
    art.open.assert_awaited_once()
    remote.close.assert_awaited_once()
    art.close.assert_awaited_once()


async def test_pair_missing_remote_token_fails_before_art(hass):
    remote = MagicMock(token=None, open=AsyncMock(), close=AsyncMock())
    art = MagicMock(open=AsyncMock(), close=AsyncMock())
    with pairing_patches(
        hass, remote=remote, art=art, ssl_context=object()
    ) as art_constructor:
        with pytest.raises(CannotConnect):
            await validate_and_pair(hass, "1.2.3.4")

    art_constructor.assert_not_called()
    remote.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("failed_client", "error", "expected_error"),
    [
        pytest.param(
            "remote", OSError("cannot open remote"), CannotConnect, id="remote-error"
        ),
        pytest.param(
            "remote",
            asyncio.CancelledError(),
            asyncio.CancelledError,
            id="remote-cancelled",
        ),
        pytest.param(
            "art", OSError("cannot open art"), CannotConnect, id="art-error"
        ),
        pytest.param(
            "art",
            asyncio.CancelledError(),
            asyncio.CancelledError,
            id="art-cancelled",
        ),
    ],
)
async def test_pair_open_failure_closes_created_clients(
    hass, failed_client, error, expected_error
):
    remote = MagicMock(
        token="remote-token", open=AsyncMock(), close=AsyncMock()
    )
    art = MagicMock(open=AsyncMock(), close=AsyncMock())
    if failed_client == "remote":
        remote.open.side_effect = error
    else:
        art.open.side_effect = error

    with pairing_patches(
        hass, remote=remote, art=art, ssl_context=object()
    ) as art_constructor:
        with pytest.raises(expected_error):
            await validate_and_pair(hass, "1.2.3.4")

    remote.close.assert_awaited_once()
    if failed_client == "remote":
        art_constructor.assert_not_called()
        art.close.assert_not_awaited()
    else:
        art_constructor.assert_called_once()
        art.close.assert_awaited_once()


async def test_pair_close_errors_do_not_mask_success(hass):
    remote = MagicMock(
        token="remote-token",
        open=AsyncMock(),
        close=AsyncMock(side_effect=OSError("cannot close remote")),
    )
    art = MagicMock(
        open=AsyncMock(),
        close=AsyncMock(side_effect=OSError("cannot close art")),
    )
    with pairing_patches(hass, remote=remote, art=art, ssl_context=object()):
        result = await validate_and_pair(hass, "1.2.3.4")

    assert result[CONF_TOKEN] == "remote-token"
    remote.close.assert_awaited_once()
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


async def test_reconfigure_updates_host_and_canonical_token(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={
            CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "remote-token",
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
    assert entry.data[CONF_TOKEN] == "remote-token"


async def test_reauth_updates_canonical_token_and_schedules_reload(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={
            CONF_MAC: "A0:D0:5B:86:CE:B7",
            CONF_TOKEN: "remote-token",
            CONF_MODEL: "QE65LS03BAUXXH",
        }),
    ), patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == "remote-token"
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_reauth_cannot_connect_keeps_stored_token(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(side_effect=CannotConnect),
    ), patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_TOKEN] == "tok"
    schedule_reload.assert_not_called()


async def test_reauth_rejects_different_tv_without_updating_entry(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={
            CONF_MAC: "00:11:22:33:44:55",
            CONF_TOKEN: "remote-token",
            CONF_MODEL: "OTHER",
        }),
    ), patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_TOKEN] == "tok"
    assert entry.data[CONF_MODEL] == "QE65LS03BAUXXH"
    schedule_reload.assert_not_called()


async def test_reconfigure_rejects_different_tv(hass):
    entry = _existing_entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={
            CONF_MAC: "00:11:22:33:44:55",
            CONF_TOKEN: "remote-token",
            CONF_MODEL: "OTHER",
        }),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "5.6.7.8"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data[CONF_HOST] == "1.2.3.4"


@pytest.mark.parametrize(
    "resource_path",
    [
        "custom_components/samsungtv_frame/strings.json",
        "custom_components/samsungtv_frame/translations/en.json",
    ],
)
def test_reconfigure_pairing_copy_requires_normal_content_and_allow(resource_path):
    resource = json.loads(Path(resource_path).read_text())

    assert (
        resource["config"]["step"]["reconfigure"]["description"]
        == RECONFIGURE_PAIRING_DESCRIPTION
    )


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
