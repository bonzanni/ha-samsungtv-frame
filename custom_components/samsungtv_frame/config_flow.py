"""Config flow for Samsung Frame TV."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.art import SamsungTVArt

from .const import CLIENT_NAME, CONF_HOST, CONF_MAC, CONF_MODEL, CONF_TOKEN, DOMAIN, PORT_REST, PORT_WS


class NotAFrameError(Exception):
    """The target device is not a Frame TV."""


class CannotConnect(Exception):
    """Could not reach the TV."""


async def validate_and_pair(hass, host: str) -> dict[str, Any]:
    """Confirm it is a Frame, then pair (one-time Allow) and capture the token."""
    session = async_get_clientsession(hass)
    rest = SamsungTVAsyncRest(host, session=session, port=PORT_REST, timeout=8)
    try:
        info = (await rest.rest_device_info()) or {}
    except Exception as err:  # noqa: BLE001
        raise CannotConnect from err
    device = info.get("device", {})
    if device.get("FrameTVSupport") != "true":
        raise NotAFrameError

    def _pair() -> str | None:
        art = SamsungTVArt(host, port=PORT_WS, name=CLIENT_NAME, timeout=30)
        art.open()  # triggers on-TV Allow prompt; returns after acceptance
        token = art.token
        art.close()
        return token

    try:
        token = await hass.async_add_executor_job(_pair)
    except Exception as err:  # noqa: BLE001
        raise CannotConnect from err
    return {
        CONF_MAC: device.get("wifiMac"),
        CONF_TOKEN: token,
        CONF_MODEL: device.get("modelName"),
    }


class SamsungFrameConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Samsung Frame TV config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                paired = await validate_and_pair(self.hass, user_input[CONF_HOST])
            except NotAFrameError:
                errors["base"] = "not_a_frame"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(format_mac(paired[CONF_MAC]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=paired[CONF_MODEL] or "Samsung Frame TV",
                    data={CONF_HOST: user_input[CONF_HOST], **paired},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )
