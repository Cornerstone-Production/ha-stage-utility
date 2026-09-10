"""Ask for the server and a cue token, then prove both work."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    CannotConnect,
    InvalidAuth,
    StageUtilityApi,
    StageUtilityError,
    TooOld,
    host_of,
    normalise_host,
)
from .const import CONF_HOST, CONF_TOKEN, DEFAULT_NAME, DOMAIN, LOGGER, OPT_SHOW_IN_SIDEBAR

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_TOKEN): str})
OPTIONS_SCHEMA = vol.Schema({vol.Required(OPT_SHOW_IN_SIDEBAR, default=True): bool})


class StageUtilityConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Stage Utility."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> StageUtilityOptionsFlow:
        """Return the options flow for an existing entry."""
        return StageUtilityOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the host and token, and check them against the server."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_url = normalise_host(user_input[CONF_HOST])
            except StageUtilityError:
                errors[CONF_HOST] = "invalid_host"
            else:
                api = StageUtilityApi(
                    async_get_clientsession(self.hass),
                    base_url,
                    user_input[CONF_TOKEN],
                )
                try:
                    manifest = await api.async_get_manifest()
                    # A manifest proves the server; the probe proves the token.
                    # Both, because a token typo would otherwise only show up
                    # the first time somebody pressed a switch.
                    await api.async_verify_token()
                except InvalidAuth:
                    errors[CONF_TOKEN] = "invalid_auth"
                except TooOld as err:
                    LOGGER.debug("Stage Utility at %s is too old: %s", base_url, err)
                    errors["base"] = "too_old"
                except CannotConnect as err:
                    LOGGER.debug("Stage Utility at %s did not answer: %s", base_url, err)
                    errors["base"] = "cannot_connect"
                else:
                    server = manifest.get("server") or {}
                    lan_url = server.get("lanUrl") or base_url
                    # The server's own LAN host, not what was typed: an
                    # operator adding the same appliance by IP and by name must
                    # not end up with two copies of every switch.
                    await self.async_set_unique_id(host_of(lan_url))
                    self._abort_if_unique_id_configured(updates={CONF_HOST: base_url})
                    return self.async_create_entry(
                        title=str(server.get("name") or DEFAULT_NAME),
                        data={CONF_HOST: base_url, CONF_TOKEN: user_input[CONF_TOKEN]},
                    )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)


class StageUtilityOptionsFlow(OptionsFlow):
    """One question: should this server be in the sidebar?"""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the sidebar option, and save it.

        Saving fires the entry's update listener, which reloads the entry — that
        reload is what actually adds or removes the panel.
        """
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, dict(self.config_entry.options)),
        )
