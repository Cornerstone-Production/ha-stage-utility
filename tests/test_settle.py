"""The settle window: what a switch shows between a press and the gear agreeing.

Companion polls the plug behind a cue on its own interval, so the first state
read after a press still carries the value from before it. Flipping the switch
back on that read is what left Apple Home and the device disagreeing after a few
quick taps.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stage_utility.const import SETTLE_SECONDS

from .conftest import HOST, StreamMockResponse
from .test_entities import SWITCH, init_integration


async def turn_on(hass: HomeAssistant) -> None:
    """Press the ON cue for the bound pair."""
    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: SWITCH}, blocking=True)


def read(stream: StreamMockResponse, state: str) -> None:
    """One `state` event on the `cues` channel, as the server sends them."""
    stream.send("cues", json.dumps({"type": "state", "id": "projectors", "state": state}))


async def press_from_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
) -> None:
    """Set the pair to off from a read, then command it on.

    The ON cue answers without a `state`, which is the answer that leaves the
    integration nothing but the command to go on — the case the window covers.
    """
    mock_server.post(f"{HOST}/api/cues/projectors_on", json={"ok": True, "detail": "Pressed Projectors ON"})
    await init_integration(hass, config_entry)
    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == STATE_OFF

    await turn_on(hass)
    state = hass.states.get(SWITCH)
    assert state.state == STATE_ON
    assert state.attributes["settling"] is True
    assert state.attributes["assumed_state"] is True
    assert state.attributes["last_commanded"] == STATE_ON
    assert state.attributes["commanded_at"] is not None


async def test_one_contradicting_read_inside_the_window_is_ignored(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The stale read two seconds after the press does not flip the switch back."""
    await press_from_off(hass, config_entry, mock_server, event_stream)

    freezer.tick(timedelta(seconds=2))
    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()

    state = hass.states.get(SWITCH)
    assert state.state == STATE_ON
    assert state.attributes["settling"] is True


async def test_a_contradiction_that_repeats_is_the_truth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two reads agreeing with each other beat the command; somebody is at the wall."""
    await press_from_off(hass, config_entry, mock_server, event_stream)

    freezer.tick(timedelta(seconds=2))
    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == STATE_ON

    freezer.tick(timedelta(seconds=2))
    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()

    state = hass.states.get(SWITCH)
    assert state.state == STATE_OFF
    assert state.attributes["settling"] is False


async def test_a_read_that_agrees_ends_the_window_at_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The gear caught up, so the switch stops assuming without waiting out the eight seconds."""
    await press_from_off(hass, config_entry, mock_server, event_stream)

    freezer.tick(timedelta(seconds=1))
    read(event_stream, STATE_ON)
    await hass.async_block_till_done()

    state = hass.states.get(SWITCH)
    assert state.state == STATE_ON
    assert state.attributes["settling"] is False
    assert state.attributes.get("assumed_state") is not True

    # The window is genuinely gone: the next contradicting read is not swallowed.
    freezer.tick(timedelta(seconds=1))
    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_after_the_window_a_single_contradicting_read_applies(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Eight seconds on, one read is enough again.

    The pair is commanded into the state it is already in, so nothing is
    restored when the window lapses and the read below is the only thing that
    can move the switch.
    """
    mock_server.post(
        f"{HOST}/api/cues/projectors_on",
        json={"ok": True, "detail": "Pressed Projectors ON"},
    )
    await init_integration(hass, config_entry)
    assert hass.states.get(SWITCH).state == STATE_ON

    await turn_on(hass)
    assert hass.states.get(SWITCH).attributes["settling"] is True

    freezer.tick(timedelta(seconds=SETTLE_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).attributes["settling"] is False

    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_a_window_that_lapses_unconfirmed_gives_the_state_back(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A press nobody confirmed is not evidence, however recent it is.

    The window is an exception to only ever showing a state that was read, so it
    has to end: with nothing read in eight seconds the switch goes back to what
    the gear last said rather than keeping the command on show for good.
    """
    await press_from_off(hass, config_entry, mock_server, event_stream)

    freezer.tick(timedelta(seconds=SETTLE_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(SWITCH)
    assert state.state == STATE_OFF
    assert state.attributes["settling"] is False


async def test_an_unbound_pair_goes_back_to_unknown(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A pair with no state source reads `unknown` again, reason and all."""
    mock_server.post(f"{HOST}/api/cues/house_lights_on", json={"ok": True, "detail": "Pressed"})
    await init_integration(hass, config_entry)
    unbound = "switch.stage_utility_house_lights"
    assert hass.states.get(unbound).state == STATE_UNKNOWN

    await hass.services.async_call("switch", "turn_on", {ATTR_ENTITY_ID: unbound}, blocking=True)
    assert hass.states.get(unbound).state == STATE_ON

    freezer.tick(timedelta(seconds=SETTLE_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(unbound)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["settling"] is False
    assert state.attributes["reason"] == "No state variable is bound to this pair"


async def test_a_failed_command_opens_no_window(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
) -> None:
    """A cue that did not run must not leave the switch showing that it did."""
    mock_server.post(
        f"{HOST}/api/cues/projectors_off",
        json={"ok": False, "detail": "Companion did not answer"},
    )
    await init_integration(hass, config_entry)
    assert hass.states.get(SWITCH).state == STATE_ON

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("switch", "turn_off", {ATTR_ENTITY_ID: SWITCH}, blocking=True)

    state = hass.states.get(SWITCH)
    assert state.state == STATE_ON
    assert state.attributes["settling"] is False
    # Nothing was commanded either: the cue did not run.
    assert state.attributes["last_commanded"] is None
    assert state.attributes["commanded_at"] is None
    # And the next read is believed straight away, with nothing to swallow it.
    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == STATE_OFF


async def test_a_late_timer_does_not_keep_the_window_open(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The eight seconds are read off the clock, not owed to the timer.

    The scheduled write is what publishes the end of a window, and Home
    Assistant can run it late. The clock is checked on every read as well, so a
    read arriving in that gap is believed rather than swallowed by a window that
    is already over.
    """
    await press_from_off(hass, config_entry, mock_server, event_stream)

    # Past the window, but the scheduled write has not run.
    freezer.tick(timedelta(seconds=SETTLE_SECONDS + 1))
    read(event_stream, STATE_OFF)
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == STATE_OFF
    assert hass.states.get(SWITCH).attributes["settling"] is False
