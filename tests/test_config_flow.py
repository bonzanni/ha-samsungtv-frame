# tests/test_config_flow.py
from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.samsungtv_frame.const import DOMAIN


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
