"""Reloading a HomeKit Bridge when the cue set changes.

The Apple Home app keeps showing a deleted cue as "No Response" because the
`homekit` component works out what to publish only when it starts; see
`custom_components/stage_utility/homekit.py` for the reference into its code.
These tests are about the scheduling: what gets reloaded, when, and how often.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    flush_store,
)

from custom_components.stage_utility.const import (
    HOMEKIT_DOMAIN,
    HOMEKIT_RELOAD_DELAY_SECONDS,
    OPT_RELOAD_HOMEKIT,
)

from .conftest import HOST, MANIFEST, StreamMockResponse

BRIDGE_TITLE = "HASS Bridge"


@pytest.fixture(autouse=True)
async def tidy_up(hass: HomeAssistant) -> AsyncIterator[None]:
    """Leave nothing for the test harness to trip over.

    The fake bridges are never really loaded, so Home Assistant must not try to
    unload them at teardown — importing the real `homekit` component pulls in
    dependencies this repo does not install. Advancing the clock also leaves the
    entity registry's delayed write pending, which reads as a lingering timer.
    """
    yield
    for bridge in hass.config_entries.async_entries(HOMEKIT_DOMAIN):
        bridge.mock_state(hass, ConfigEntryState.NOT_LOADED)
    await flush_store(er.async_get(hass)._store)  # noqa: SLF001


@pytest.fixture
def reloads(hass: HomeAssistant) -> Iterator[AsyncMock]:
    """Record every `async_reload`, without running one.

    The `homekit` component itself is never loaded here: what is under test is
    which entries this integration asks to reload, and when.
    """
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as recorder:
        yield recorder


def add_bridge(
    hass: HomeAssistant,
    *,
    options: dict[str, Any] | None = None,
    title: str = BRIDGE_TITLE,
) -> MockConfigEntry:
    """A loaded `homekit` entry, in bridge mode admitting switches by default."""
    entry = MockConfigEntry(
        domain=HOMEKIT_DOMAIN,
        title=title,
        data={"name": "HASS Bridge", "port": 21063},
        options={"mode": "bridge", "filter": {"include_domains": ["switch"]}, **(options or {})},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


def manifest_with(*, switches: list[dict[str, Any]], version: int) -> dict[str, Any]:
    """The fixture manifest at a new version with a given switch list."""
    updated = copy.deepcopy(MANIFEST)
    updated["version"] = version
    updated["switches"] = switches
    return updated


def a_switch(cue_id: str) -> dict[str, Any]:
    """One switch row the coordinator will accept."""
    return {
        "id": cue_id,
        "name": cue_id.replace("_", " ").title(),
        "room": None,
        "on": f"{cue_id}_on",
        "off": f"{cue_id}_off",
        "toggle": False,
        "state": "off",
        "reason": None,
        "stateSource": None,
        "available": True,
    }


async def serve(
    hass: HomeAssistant,
    mock_server: Any,
    event_stream: StreamMockResponse,
    manifest: dict[str, Any],
) -> None:
    """Answer the manifest with `manifest`, then push a manifest event."""
    mock_server.clear_requests()
    mock_server._mocks.insert(0, event_stream)  # noqa: SLF001
    mock_server.get(f"{HOST}/api/cues/manifest", json=manifest)
    mock_server.post(f"{HOST}/api/events/subscribe", json={"ok": True})
    event_stream.send("cues", json.dumps({"type": "manifest", "version": manifest["version"]}))
    await hass.async_block_till_done()


async def settle(hass: HomeAssistant, seconds: int = HOMEKIT_RELOAD_DELAY_SECONDS + 1) -> None:
    """Let the debounce window lapse."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def start(hass: HomeAssistant, config_entry: MockConfigEntry, reloads: AsyncMock) -> None:
    """Set the entry up and clear the reload its own new entities earn.

    A server added while Home Assistant is running is itself a cue set the
    bridge has never seen, so setup schedules a reload of its own.
    """
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await settle(hass)
    reloads.reset_mock()


async def test_a_new_cue_reloads_the_bridge_once_after_the_delay(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One added cue is one reload, and not before the window is up."""
    bridge = add_bridge(hass)
    await start(hass, config_entry, reloads)

    await serve(
        hass,
        mock_server,
        event_stream,
        manifest_with(switches=[*MANIFEST["switches"], a_switch("stage_wash")], version=43),
    )
    assert hass.states.get("switch.stage_utility_stage_wash") is not None

    # Nothing yet: the cue set may still be changing.
    await settle(hass, HOMEKIT_RELOAD_DELAY_SECONDS - 1)
    assert reloads.await_count == 0

    with caplog.at_level(logging.INFO, logger="custom_components.stage_utility"):
        await settle(hass)
    assert reloads.await_count == 1
    assert reloads.await_args_list[0].args == (bridge.entry_id,)
    assert 'Reloading HomeKit Bridge "HASS Bridge" so Home picks up 1 added and 0 removed switches' in caplog.text


async def test_a_deleted_cue_reloads_the_bridge(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The case seen on the real install: a cue deleted in Stage Utility."""
    bridge = add_bridge(hass)
    await start(hass, config_entry, reloads)

    with caplog.at_level(logging.INFO, logger="custom_components.stage_utility"):
        await serve(hass, mock_server, event_stream, manifest_with(switches=[MANIFEST["switches"][0]], version=43))
        await settle(hass)

    assert reloads.await_count == 1
    assert reloads.await_args_list[0].args == (bridge.entry_id,)
    assert "so Home picks up 0 added and 1 removed switches" in caplog.text


async def test_five_changes_inside_the_window_collapse_to_one_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An import of many pairs is one reload, counted from the last change.

    Five manifest events five seconds apart span more than the fifteen-second
    window, so a debounce measured from the first change would reload twice or
    more; measured from the last it reloads once, after the last one.
    """
    bridge = add_bridge(hass)
    await start(hass, config_entry, reloads)

    switches = list(MANIFEST["switches"])
    with caplog.at_level(logging.INFO, logger="custom_components.stage_utility"):
        for index in range(5):
            switches = [*switches, a_switch(f"imported_{index}")]
            await serve(hass, mock_server, event_stream, manifest_with(switches=switches, version=44 + index))
            await settle(hass, 5)
            assert reloads.await_count == 0, f"reloaded while the cue set was still changing (change {index})"

        await settle(hass)

    assert reloads.await_count == 1
    assert reloads.await_args_list[0].args == (bridge.entry_id,)
    assert "so Home picks up 5 added and 0 removed switches" in caplog.text


async def test_a_bridge_in_accessory_mode_is_left_alone(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
) -> None:
    """Accessory mode is one entity paired in its own right, and not ours."""
    add_bridge(hass, options={"mode": "accessory"}, title="Front Door")
    await start(hass, config_entry, reloads)

    await serve(
        hass,
        mock_server,
        event_stream,
        manifest_with(switches=[*MANIFEST["switches"], a_switch("stage_wash")], version=43),
    )
    await settle(hass)

    assert reloads.await_count == 0


@pytest.mark.parametrize(
    "entity_filter",
    [
        {"include_domains": ["light"]},
        {"include_entities": ["light.chancel"]},
        {"exclude_domains": ["switch", "button"]},
    ],
    ids=["lights-only", "one-other-entity", "our-domains-excluded"],
)
async def test_a_bridge_whose_filter_excludes_us_is_left_alone(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
    entity_filter: dict[str, list[str]],
) -> None:
    """A bridge that cannot be publishing a cue has nothing to pick up."""
    add_bridge(hass, options={"filter": entity_filter})
    await start(hass, config_entry, reloads)

    await serve(
        hass,
        mock_server,
        event_stream,
        manifest_with(switches=[*MANIFEST["switches"], a_switch("stage_wash")], version=43),
    )
    await settle(hass)

    assert reloads.await_count == 0


async def test_a_bridge_listing_one_of_our_switches_is_reloaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
) -> None:
    """A filter naming our entities admits us even with no domain listed."""
    bridge = add_bridge(hass, options={"filter": {"include_entities": ["switch.stage_utility_projectors"]}})
    await start(hass, config_entry, reloads)

    await serve(
        hass,
        mock_server,
        event_stream,
        manifest_with(switches=[*MANIFEST["switches"], a_switch("stage_wash")], version=43),
    )
    await settle(hass)

    assert reloads.await_count == 1
    assert reloads.await_args_list[0].args == (bridge.entry_id,)


async def test_the_option_off_reloads_nothing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Turned off, the operator reloads the bridge themselves and we say nothing."""
    add_bridge(hass)
    hass.config_entries.async_update_entry(config_entry, options={OPT_RELOAD_HOMEKIT: False})
    await start(hass, config_entry, reloads)

    with caplog.at_level(logging.DEBUG, logger="custom_components.stage_utility"):
        await serve(
            hass,
            mock_server,
            event_stream,
            manifest_with(switches=[*MANIFEST["switches"], a_switch("stage_wash")], version=43),
        )
        await settle(hass)

    assert reloads.await_count == 0
    assert "HomeKit" not in caplog.text


async def test_a_state_change_alone_reloads_nothing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
) -> None:
    """A sync that added and removed nothing is not a reason to reload.

    A state event and a cue renamed in Stage Utility both run the entity sync;
    neither changes what a bridge publishes.
    """
    add_bridge(hass)
    await start(hass, config_entry, reloads)

    event_stream.send("cues", json.dumps({"type": "state", "id": "projectors", "state": "off"}))
    await hass.async_block_till_done()

    renamed = copy.deepcopy(MANIFEST)
    renamed["version"] = 43
    renamed["switches"][0]["name"] = "Beamers"
    await serve(hass, mock_server, event_stream, renamed)
    await settle(hass)

    assert reloads.await_count == 0


async def test_reloading_our_own_entry_reloads_no_bridge(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: Any,
    event_stream: StreamMockResponse,
    reloads: AsyncMock,
) -> None:
    """Re-adding the entities Home Assistant already has is not a change.

    Our own entry reloads whenever an option is saved, and every entity is
    added again on the way back up. The bridge is already publishing them.
    """
    add_bridge(hass)
    await start(hass, config_entry, reloads)

    # Not through `async_reload`: it is patched out by the `reloads` fixture.
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await settle(hass)

    assert reloads.await_count == 0
