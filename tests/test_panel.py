"""The sidebar entry: when it appears, what it points at, and when it does not."""

from __future__ import annotations

import logging

import pytest
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.components.http import ApiConfig
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stage_utility.const import (
    CONF_HOST,
    CONF_TOKEN,
    DOMAIN,
    OPT_SHOW_IN_SIDEBAR,
    PANEL_URL_PATH,
)

from .conftest import HOST, TOKEN, StreamMockResponse

SECOND_HOST = "http://stage-utility-chapel:8788"


def panel_paths(hass: HomeAssistant) -> list[str]:
    """Every panel path Stage Utility owns, read from the frontend's registry."""
    return sorted(path for path in hass.data.get(DATA_PANELS, {}) if path.startswith(PANEL_URL_PATH))


async def test_panel_registered_with_the_server_name_and_url(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """Setting the entry up puts the server in the sidebar, framed."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]
    assert panel.component_name == "iframe"
    # The name off the manifest, not the entry title the operator can rename.
    assert panel.sidebar_title == "Stage Utility"
    assert panel.sidebar_icon == "mdi:microphone-variant"
    assert panel.config == {"url": HOST}
    assert panel.require_admin is False


async def test_panel_removed_on_unload(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """Unloading the entry takes its entry out of the sidebar."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert panel_paths(hass) == [PANEL_URL_PATH]

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert panel_paths(hass) == []


async def test_no_panel_when_the_option_is_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """An operator who turned the sidebar entry off does not get one."""
    hass.config_entries.async_update_entry(config_entry, options={OPT_SHOW_IN_SIDEBAR: False})

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert panel_paths(hass) == []


async def test_option_change_removes_the_panel(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """Turning the option off reloads the entry and the entry goes away."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert panel_paths(hass) == [PANEL_URL_PATH]

    hass.config_entries.async_update_entry(config_entry, options={OPT_SHOW_IN_SIDEBAR: False})
    await hass.async_block_till_done()

    assert panel_paths(hass) == []

    # And back on again, so the reload is proved in both directions.
    hass.config_entries.async_update_entry(config_entry, options={OPT_SHOW_IN_SIDEBAR: True})
    await hass.async_block_till_done()

    assert panel_paths(hass) == [PANEL_URL_PATH]


async def test_two_servers_get_two_panels(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A second server sits beside the first rather than overwriting it."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    second_stream = StreamMockResponse(f"{SECOND_HOST}/api/events")
    aioclient_mock._mocks.insert(0, second_stream)  # noqa: SLF001
    aioclient_mock.get(
        f"{SECOND_HOST}/api/cues/manifest",
        json={"version": 1, "server": {"name": "Chapel", "lanUrl": SECOND_HOST}, "switches": [], "buttons": []},
    )
    aioclient_mock.post(f"{SECOND_HOST}/api/events/subscribe", json={"ok": True})
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Chapel",
        data={CONF_HOST: SECOND_HOST, CONF_TOKEN: TOKEN},
        unique_id="stage-utility-chapel",
    )
    second.add_to_hass(hass)
    await hass.config_entries.async_setup(second.entry_id)
    await hass.async_block_till_done()

    suffixed_path = f"{PANEL_URL_PATH}-{second.entry_id[:8]}"
    assert panel_paths(hass) == sorted([PANEL_URL_PATH, suffixed_path])
    panels = hass.data[DATA_PANELS]
    assert panels[PANEL_URL_PATH].config == {"url": HOST}
    assert panels[suffixed_path].config == {"url": SECOND_HOST}
    assert panels[suffixed_path].sidebar_title == "Chapel"

    # Unloading one leaves the other where it was.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert panel_paths(hass) == [suffixed_path]


@pytest.mark.parametrize(
    "https_config",
    [
        {"api_use_ssl": True},
        {"external_url": "https://stage.example.com"},
        {"internal_url": "https://stage.internal.example.com"},
    ],
    ids=["own-ssl", "external-url", "internal-url"],
)
async def test_no_panel_when_home_assistant_is_https(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    https_config: dict[str, object],
) -> None:
    """A browser will not frame plain HTTP inside an HTTPS page, so nothing is added."""
    if https_config.get("api_use_ssl"):
        # Home Assistant terminating TLS itself: no configured URL says https,
        # only the http component's own config does. Set up ahead of the entry,
        # because the entry pulls `frontend` in — and `http` behind it, which
        # rebuilds `hass.config.api` from its own configuration.
        assert await async_setup_component(hass, "http", {"http": {}})
        hass.config.api = ApiConfig("127.0.0.1", "127.0.0.1", 8123, True)
    else:
        await hass.config.async_update(**https_config)

    with caplog.at_level(logging.INFO, logger="custom_components.stage_utility"):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert panel_paths(hass) == []
    # An operator who cannot find the sidebar entry has one line saying why and
    # where to go instead.
    assert "Not adding Stage Utility to the sidebar" in caplog.text
    assert "served over HTTPS" in caplog.text
    assert HOST in caplog.text


async def test_panel_registered_when_both_are_https(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    """HTTPS is only a problem against a plain-HTTP server."""
    secure_host = "https://stage-utility.example.com"
    stream = StreamMockResponse(f"{secure_host}/api/events")
    aioclient_mock._mocks.insert(0, stream)  # noqa: SLF001
    aioclient_mock.get(
        f"{secure_host}/api/cues/manifest",
        json={"version": 1, "server": {"name": "Stage Utility", "lanUrl": secure_host}, "switches": [], "buttons": []},
    )
    aioclient_mock.post(f"{secure_host}/api/events/subscribe", json={"ok": True})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Stage Utility",
        data={CONF_HOST: secure_host, CONF_TOKEN: TOKEN},
        unique_id="stage-utility.example.com",
    )
    entry.add_to_hass(hass)
    await hass.config.async_update(external_url="https://ha.example.com")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert panel_paths(hass) == [PANEL_URL_PATH]
    assert hass.data[DATA_PANELS][PANEL_URL_PATH].config == {"url": secure_host}
