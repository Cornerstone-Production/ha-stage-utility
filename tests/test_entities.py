"""Entities: what they show, what they call, and how a refusal reads."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.stage_utility.const import DOMAIN

from .conftest import HOST, MANIFEST, TOKEN, StreamMockResponse

SWITCH = "switch.stage_utility_projectors"
UNBOUND_SWITCH = "switch.stage_utility_house_lights"
BUTTON = "button.stage_utility_reset_ultrix"


async def init_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Set the entry up and let the stream task reach its read loop."""
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


def cue_calls(mock: AiohttpClientMocker, cue: str) -> list[tuple[Any, ...]]:
    """Every POST made to one cue."""
    return [
        call
        for call in mock.mock_calls
        if call[0].lower() == "post" and call[1].path == f"/api/cues/{cue}"
    ]


async def test_entities_appear_with_the_server_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """Each manifest row becomes an entity on one device for the server."""
    await init_integration(hass, config_entry)

    bound = hass.states.get(SWITCH)
    assert bound is not None
    assert bound.state == STATE_ON
    assert bound.attributes["friendly_name"] == "Stage Utility Projectors"
    assert bound.attributes["state_source"] == "MA_HL_Projector:powerState"
    assert bound.attributes["cue_on"] == "projectors_on"
    assert bound.attributes["cue_off"] == "projectors_off"
    # A bound pair reports; it does not assume.
    assert bound.attributes.get("assumed_state") is not True

    unbound = hass.states.get(UNBOUND_SWITCH)
    assert unbound is not None
    assert unbound.state == STATE_UNKNOWN
    assert unbound.attributes["assumed_state"] is True
    assert unbound.attributes["reason"] == "No state variable is bound to this pair"

    assert hass.states.get(BUTTON) is not None

    registry = er.async_get(hass)
    entry = registry.async_get(SWITCH)
    assert entry is not None
    assert entry.unique_id == f"{config_entry.entry_id}_projectors"


async def test_turn_on_posts_the_on_cue_with_the_token(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """`switch.turn_on` posts the manifest's ON cue, bearing the token."""
    mock_server.post(
        f"{HOST}/api/cues/projectors_on",
        json={"ok": True, "detail": "Pressed Projectors ON", "state": "on"},
    )
    await init_integration(hass, config_entry)

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: SWITCH}, blocking=True
    )

    calls = cue_calls(mock_server, "projectors_on")
    assert len(calls) == 1
    assert calls[0][3]["Authorization"] == f"Bearer {TOKEN}"
    # No other cue was pressed.
    assert cue_calls(mock_server, "projectors_off") == []


async def test_turn_off_posts_the_off_cue(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """`switch.turn_off` posts the OFF cue, and the reported state lands."""
    mock_server.post(
        f"{HOST}/api/cues/projectors_off",
        json={"ok": True, "detail": "Pressed Projectors OFF", "state": "off"},
    )
    await init_integration(hass, config_entry)

    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: SWITCH}, blocking=True
    )

    assert len(cue_calls(mock_server, "projectors_off")) == 1
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_refusal_surfaces_the_servers_sentence(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """A 409 reaches the operator as the server wrote it, not as a status code."""
    mock_server.post(
        f"{HOST}/api/cues/projectors_off",
        status=409,
        json={
            "error": "The Gospel Way is live",
            "reason": "service-live",
            "plan": "The Gospel Way",
        },
    )
    await init_integration(hass, config_entry)

    with pytest.raises(HomeAssistantError, match="The Gospel Way is live"):
        await hass.services.async_call(
            "switch", "turn_off", {ATTR_ENTITY_ID: SWITCH}, blocking=True
        )

    # Refused means nothing moved: the switch keeps the state the gear reports.
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_confirmation_is_answered_immediately(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """A 202 is followed by the same call carrying the confirm token.

    Home Assistant has no second person to ask — the operator already pressed
    the switch — so the integration is the intent and confirms at once.
    """
    answers = [
        {"status": 202, "json": {"confirm": "tok-123", "expiresInSec": 30}},
        {"status": 200, "json": {"ok": True, "detail": "Reset sent"}},
    ]

    async def _side_effect(method, url, data):  # noqa: ANN001, ARG001
        from pytest_homeassistant_custom_component.test_util.aiohttp import (
            AiohttpClientMockResponse,
        )

        answer = answers.pop(0)
        return AiohttpClientMockResponse(
            method=method, url=url, status=answer["status"], json=answer["json"]
        )

    mock_server.post(f"{HOST}/api/cues/reset_ultrix", side_effect=_side_effect)
    await init_integration(hass, config_entry)

    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: BUTTON}, blocking=True
    )

    calls = cue_calls(mock_server, "reset_ultrix")
    assert len(calls) == 2
    assert calls[0][1].query.get("confirm") is None
    assert calls[1][1].query["confirm"] == "tok-123"


async def test_skipped_is_success(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """`skipped: true` means the gear was already there — not a failure."""
    mock_server.post(
        f"{HOST}/api/cues/projectors_on",
        json={
            "ok": True,
            "detail": "Projectors are already on",
            "skipped": True,
            "state": "on",
        },
    )
    await init_integration(hass, config_entry)

    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: SWITCH}, blocking=True
    )

    assert hass.states.get(SWITCH).state == STATE_ON


async def test_unavailable_when_the_button_is_missing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    event_stream: StreamMockResponse,
) -> None:
    """`available: false` means the Companion button is gone; say so."""
    manifest = copy.deepcopy(MANIFEST)
    manifest["switches"][0]["available"] = False
    aioclient_mock.get(f"{HOST}/api/cues/manifest", json=manifest)
    aioclient_mock.post(f"{HOST}/api/events/subscribe", json={"ok": True})

    await init_integration(hass, config_entry)

    assert hass.states.get(SWITCH).state == "unavailable"


async def test_state_event_flips_the_switch(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
) -> None:
    """A `state` event on the `cues` channel moves the entity without a poll."""
    await init_integration(hass, config_entry)
    assert hass.states.get(SWITCH).state == STATE_ON

    event_stream.send(
        "cues",
        json.dumps(
            {"type": "state", "id": "projectors", "state": "off", "reason": None}
        ),
    )
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == STATE_OFF
    # Nothing was polled to learn that.
    assert not any(
        call[1].path == "/api/cues/states" for call in mock_server.mock_calls
    )


async def test_manifest_event_adds_and_removes_entities(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    event_stream: StreamMockResponse,
) -> None:
    """A manifest bump adds the new cue and takes the departed one away.

    A cue the operator deleted must not linger as an unavailable switch, so the
    entity leaves the registry rather than just going grey.
    """
    aioclient_mock.post(f"{HOST}/api/events/subscribe", json={"ok": True})
    aioclient_mock.get(f"{HOST}/api/cues/manifest", json=MANIFEST)
    await init_integration(hass, config_entry)

    registry = er.async_get(hass)
    assert registry.async_get(SWITCH) is not None
    assert hass.states.get(BUTTON) is not None

    # Version 43: house lights gone, a new "stage wash" pair, no buttons.
    updated = copy.deepcopy(MANIFEST)
    updated["version"] = 43
    updated["switches"] = [
        MANIFEST["switches"][0],
        {
            "id": "stage_wash",
            "name": "Stage Wash",
            "room": "Main Auditorium",
            "on": "stage_wash_on",
            "off": "stage_wash_off",
            "toggle": False,
            "state": "off",
            "reason": None,
            "stateSource": "MA_Wash:powerState",
            "available": True,
        },
    ]
    updated["buttons"] = []
    aioclient_mock.clear_requests()
    aioclient_mock._mocks.insert(0, event_stream)  # noqa: SLF001
    aioclient_mock.get(f"{HOST}/api/cues/manifest", json=updated)
    aioclient_mock.post(f"{HOST}/api/events/subscribe", json={"ok": True})

    event_stream.send("cues", json.dumps({"type": "manifest", "version": 43}))
    await hass.async_block_till_done()

    assert hass.states.get("switch.stage_utility_stage_wash").state == STATE_OFF
    assert registry.async_get(UNBOUND_SWITCH) is None
    assert registry.async_get(BUTTON) is None
    assert registry.async_get(SWITCH) is not None


async def test_diagnostics_redacts_the_token(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_server: AiohttpClientMocker
) -> None:
    """Diagnostics carry the manifest and the stream status, never the token."""
    from homeassistant.components.diagnostics import REDACTED

    from custom_components.stage_utility.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await init_integration(hass, config_entry)
    report = await async_get_config_entry_diagnostics(hass, config_entry)

    assert report["entry"]["data"]["token"] == REDACTED
    assert TOKEN not in json.dumps(report)
    assert report["manifest_version"] == 42
    assert report["states"]["projectors"]["state"] == "on"
    assert report["stream"]["connected"] is True
    assert report["stream"]["channel"] == "cues"
    assert len(report["buttons"]) == 1
