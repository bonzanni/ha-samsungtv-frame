"""Config flow for Samsung Frame TV."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.art import SamsungTVArt

from .const import (
    CLIENT_NAME,
    CONF_HOST,
    CONF_MAC,
    CONF_MODEL,
    CONF_TOKEN,
    DEFAULT_HEARTBEAT_SECONDS,
    DOMAIN,
    OPT_HEARTBEAT,
    PORT_REST,
    PORT_WS,
)


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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FrameOptionsFlow:
        return FrameOptionsFlow()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point the entry at a new IP (validates it is the same TV)."""
        entry = self._get_reconfigure_entry()
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
                self._abort_if_unique_id_mismatch()
                # Keep the stored token: re-pairing an already-granted client
                # returns None, which must not clobber a captured token.
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_MODEL: paired[CONF_MODEL],
                    },
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str}
            ),
            errors=errors,
        )

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


class FrameOptionsFlow(OptionsFlowWithReload):
    """Options: polling heartbeat (the entry reloads on save)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_HEARTBEAT,
                        default=self.config_entry.options.get(
                            OPT_HEARTBEAT, DEFAULT_HEARTBEAT_SECONDS
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=60)),
                }
            ),
        )
