"""The config flow: a server, a token, and what happens when either is wrong."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stage_utility.const import CONF_HOST, CONF_TOKEN, DOMAIN

from .conftest import HOST, MANIFEST, TOKEN


async def test_user_flow_creates_entry(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A reachable server and an accepted token make an entry."""
    aioclient_mock.get(f"{HOST}/api/cues/manifest", json=MANIFEST)
    aioclient_mock.post(
        f"{HOST}/api/cues/__probe__",
        status=404,
        json={"error": "There is no cue called __probe__", "reason": "unknown"},
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # Typed as a bare IP: the flow supplies the scheme and the default port.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.168.16.61", CONF_TOKEN: TOKEN}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Stage Utility"
    assert result["data"] == {CONF_HOST: HOST, CONF_TOKEN: TOKEN}
    assert result["result"].unique_id == "192.168.16.61"


async def test_user_flow_bad_token(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A 401 on the probe is a bad token, and says so on the token field."""
    aioclient_mock.get(f"{HOST}/api/cues/manifest", json=MANIFEST)
    aioclient_mock.post(
        f"{HOST}/api/cues/__probe__",
        status=401,
        json={"error": "A bearer token is required"},
    )

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_TOKEN: "su_wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TOKEN: "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A server that does not answer the manifest is a connection error."""
    aioclient_mock.get(f"{HOST}/api/cues/manifest", status=500)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST, CONF_TOKEN: TOKEN})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_rejects_unusable_host(hass: HomeAssistant) -> None:
    """Something that is not an address never reaches the network."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "ftp://nope", CONF_TOKEN: TOKEN}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HOST: "invalid_host"}


async def test_same_server_twice_aborts(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry) -> None:
    """Adding the same appliance by another address must not duplicate it.

    The unique id is the server's OWN `lanUrl` host, so adding it by hostname
    after adding it by IP is recognised as the same box.
    """
    aioclient_mock.get(f"{HOST}/api/cues/manifest", json=MANIFEST)
    aioclient_mock.post(f"{HOST}/api/cues/__probe__", status=404, json={})

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.168.16.61:8788", CONF_TOKEN: TOKEN}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
