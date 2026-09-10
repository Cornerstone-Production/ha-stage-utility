"""The subscription, and the poll that only runs when it is gone.

The server only reads the gear behind a cue while somebody is subscribed to the
`cues` channel, so the subscription is the integration's whole reason to exist
and the poll is the apology for it being down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stage_utility.api import CannotConnect
from custom_components.stage_utility.coordinator import (
    MAX_FALLBACK_POLL_SECONDS,
    StageUtilityCoordinator,
)

from .conftest import HOST, StreamMockResponse
from .test_entities import SWITCH, init_integration


def monkeypatch_manifest(coordinator: StageUtilityCoordinator, replacement: object) -> None:
    """Point the coordinator's manifest read at something the test controls."""
    coordinator.api.async_get_manifest = replacement  # type: ignore[method-assign]


async def test_subscribes_to_only_the_cues_channel(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """One stream, one cid, and a filter naming just `cues`."""
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    streams = [call for call in mock_server.mock_calls if call[0].lower() == "get" and call[1].path == "/api/events"]
    assert len(streams) == 1
    assert streams[0][1].query["cid"] == coordinator.cid

    subscribes = [call for call in mock_server.mock_calls if call[1].path == "/api/events/subscribe"]
    assert len(subscribes) == 1
    assert subscribes[0][2] == {"cid": coordinator.cid, "channels": ["cues"]}
    assert coordinator.stream_connected is True


async def test_no_fallback_poll_while_the_stream_is_up(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """A healthy stream must cost the server nothing on `/api/cues/states`."""
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    # The loop's own guard, driven directly: started against a healthy stream
    # it must return having asked the server for nothing.
    assert coordinator.stream_connected is True
    await coordinator._fallback_loop()  # noqa: SLF001

    assert not any(call[1].path == "/api/cues/states" for call in mock_server.mock_calls)


async def test_fallback_poll_starts_only_when_the_stream_drops(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """Losing the stream starts the poll; regaining it stops the poll."""
    mock_server.get(
        f"{HOST}/api/cues/states",
        json={
            "ok": True,
            "checkedAt": "2026-09-09T14:00:00.000Z",
            "states": {"projectors": {"state": "off", "value": "off"}},
        },
    )
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert hass.states.get(SWITCH).state == STATE_ON

    coordinator._set_connected(False, "socket closed")  # noqa: SLF001
    assert coordinator._fallback_task is not None  # noqa: SLF001

    # The loop polls at once on a drop rather than leaving the switch stale for
    # half a minute, so one turn of the event loop is enough to see it.
    await asyncio.sleep(0)
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == STATE_OFF

    coordinator._set_connected(True, None)  # noqa: SLF001
    assert coordinator._fallback_task is None  # noqa: SLF001


async def test_fallback_poll_reports_a_failure_rather_than_swallowing_it(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """A poll that could not read says so, so the loop can back off.

    With the stream still up it says so to the loop and to nobody else: one
    unanswered request is not a server that is gone, and the entities stay put.
    """
    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    assert coordinator.stream_connected is True
    assert await coordinator.async_poll_states_once() is False
    assert coordinator.last_update_success is True
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_reconnect_refetches_the_manifest(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
) -> None:
    """A server that hung up is caught, and the manifest is read again.

    Nothing is replayed on reconnect, so whatever changed while the socket was
    dead is only learnt by asking.
    """
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    before = sum(1 for call in mock_server.mock_calls if call[1].path == "/api/cues/manifest")

    event_stream.hang_up()
    # Let the read loop notice EOF and record the disconnect, without waiting
    # out the reconnect backoff.
    for _ in range(10):
        await asyncio.sleep(0)
        if coordinator.stream_connected is False:
            break
    assert coordinator.stream_connected is False
    assert coordinator.stream_error == "The server closed the event stream"

    # The reconnect itself: a fresh stream, and the manifest read again.
    event_stream.stream = type(event_stream.stream)(  # a new, open reader
        event_stream.stream._protocol, limit=2**16
    )
    await asyncio.sleep(1.1)
    await hass.async_block_till_done()

    after = sum(1 for call in mock_server.mock_calls if call[1].path == "/api/cues/manifest")
    assert after > before
    assert coordinator.stream_connected is True


async def test_oversized_frame_is_refused(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    event_stream: StreamMockResponse,
) -> None:
    """A runaway frame ends the connection instead of growing without bound."""
    from custom_components.stage_utility.coordinator import MAX_EVENT_BYTES

    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    event_stream.stream.feed_data(b"x" * (MAX_EVENT_BYTES + 1))
    for _ in range(10):
        await asyncio.sleep(0)
        if coordinator.stream_connected is False:
            break

    assert coordinator.stream_connected is False
    assert "oversized" in (coordinator.stream_error or "")


async def test_a_dead_server_makes_its_entities_unavailable(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """Stream down and the poll failing means nothing knows what the gear is doing.

    Leaving the switches available and showing what they last heard is a control
    that lies about a server that is not there.
    """
    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert hass.states.get(SWITCH).state == STATE_ON

    coordinator._set_connected(False, "socket closed")  # noqa: SLF001
    await asyncio.sleep(0)
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert hass.states.get(SWITCH).state == "unavailable"


async def test_the_first_successful_poll_brings_the_entities_back(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """A poll that answers clears the outage without waiting for the stream.

    The recovery payload says exactly what the switch already holds, so nothing
    republishes the data as a side effect of the state changing. What brings the
    entity back has to be the poll noting the server reachable again — which is
    the whole thing under test.
    """
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert hass.states.get(SWITCH).state == STATE_ON

    mock_server.get(f"{HOST}/api/cues/states", status=503)
    coordinator._set_connected(False, "socket closed")  # noqa: SLF001
    assert await coordinator.async_poll_states_once() is False
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "unavailable"

    mock_server.clear_requests()
    mock_server.get(
        f"{HOST}/api/cues/states",
        json={"ok": True, "states": {"projectors": {"state": "on"}}},
    )
    assert await coordinator.async_poll_states_once() is True
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_a_reconnect_brings_the_entities_back(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """A reconnect that reads the manifest is proof enough; no poll needed."""
    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    coordinator._set_connected(False, "socket closed")  # noqa: SLF001
    assert await coordinator.async_poll_states_once() is False
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "unavailable"

    # The reconnect, as `_stream_once` performs it: the socket, then the
    # manifest refetch that proves the server is really answering.
    coordinator._set_connected(True, None)  # noqa: SLF001
    await coordinator._async_refetch_on_reconnect()  # noqa: SLF001
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert coordinator.unreachable_since is None
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_a_socket_that_opens_but_a_manifest_that_500s_is_still_an_outage(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Accepting the socket is not answering; the outage clock must survive it.

    A server that takes the connection and then 500s the manifest reconnects
    over and over. Clearing the outage on the socket alone reset the clock on
    every one of those, so an outage that never ended never reached five
    minutes and the one WARNING an operator has to read never fired.
    """
    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    async def _dead_manifest() -> dict[str, object]:
        raise CannotConnect("/api/cues/manifest answered HTTP 500")

    monkeypatch_manifest(coordinator, _dead_manifest)

    with caplog.at_level(logging.WARNING, "custom_components.stage_utility"):
        for _ in range(7):
            coordinator._set_connected(False, "socket closed")  # noqa: SLF001
            await coordinator.async_poll_states_once()
            # The reconnect: the socket opens, and the manifest refetch fails.
            coordinator._set_connected(True, None)  # noqa: SLF001
            await coordinator._async_refetch_on_reconnect()  # noqa: SLF001
            freezer.tick(timedelta(minutes=1))

    assert coordinator.unreachable_since is not None
    warnings = [r for r in caplog.records if "unreachable for" in r.getMessage()]
    assert len(warnings) == 1


async def test_a_poll_that_fails_after_the_reconnect_leaves_the_entities_alone(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """The stream is back: a poll losing a race with it is not an outage.

    The fallback poll can have a request in flight when the stream reconnects.
    Letting that late failure mark the update failed would strand every entity
    unavailable while the stream is sitting there delivering state.
    """
    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    coordinator._set_connected(False, "socket closed")  # noqa: SLF001
    assert await coordinator.async_poll_states_once() is False
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH).state == "unavailable"

    # The reconnect, as `_stream_once` performs it: the socket, then the
    # manifest refetch that proves the server is really answering.
    coordinator._set_connected(True, None)  # noqa: SLF001
    await coordinator._async_refetch_on_reconnect()  # noqa: SLF001
    await hass.async_block_till_done()
    assert await coordinator.async_poll_states_once() is False
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_diagnostics_carry_how_long_the_server_has_been_away(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """`unreachable_since` is what dates an outage in a downloaded dump.

    `last_update_success: false` says the server is not answering; it does not
    say whether that started a minute ago or on Friday night.
    """
    from custom_components.stage_utility.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert report["coordinator"]["unreachable_since"] is None

    coordinator._set_connected(False, "socket closed")  # noqa: SLF001
    assert await coordinator.async_poll_states_once() is False

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert report["coordinator"]["last_update_success"] is False
    assert report["coordinator"]["unreachable_since"] == coordinator.unreachable_since.isoformat()


async def test_a_long_outage_warns_once_at_five_minutes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One WARNING when an outage passes 5 min, then silence until recovery.

    A server switched off overnight must not fill the log, and an operator
    reading it at 9am on a Sunday must find one line saying why every switch is
    grey.
    """
    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    coordinator._set_connected(False, "socket closed")  # noqa: SLF001

    with caplog.at_level(logging.WARNING, "custom_components.stage_utility"):
        await coordinator.async_poll_states_once()
        # Four minutes in is still just the INFO line the drop already logged.
        freezer.tick(timedelta(minutes=4))
        await coordinator.async_poll_states_once()
        assert [r for r in caplog.records if "unreachable for" in r.getMessage()] == []

        freezer.tick(timedelta(minutes=2))
        for _ in range(5):
            await coordinator.async_poll_states_once()
            freezer.tick(timedelta(minutes=10))

    warnings = [r for r in caplog.records if "unreachable for" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].getMessage() == (
        f"Stage Utility at {HOST} has been unreachable for 5 min; its switches are unavailable"
    )


async def test_the_fallback_tick_caps_its_backoff_and_nudges_the_stream(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll backs off no further than a minute, and wakes the stream each tick.

    Uncapped, eight failed ticks would have the poll an hour and a half apart —
    and since the reconnect rides this tick, that is also how long a recovered
    server would go unnoticed.
    """
    mock_server.get(f"{HOST}/api/cues/states", status=503)
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    # Take the real stream loop out of the way, so the patched sleep below is
    # only ever reached by the fallback loop.
    coordinator._stream_task.cancel()  # noqa: SLF001
    coordinator.stream_connected = False

    delays: list[float] = []
    nudged: list[bool] = []

    async def fake_sleep(seconds, *args, **kwargs):  # noqa: ANN001, ANN202, ARG001
        nudged.append(coordinator._retry_stream.is_set())  # noqa: SLF001
        coordinator._retry_stream.clear()  # noqa: SLF001
        delays.append(seconds)
        if len(delays) >= 8:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await coordinator._fallback_loop()  # noqa: SLF001
    monkeypatch.undo()

    assert MAX_FALLBACK_POLL_SECONDS == 60
    assert max(delays) == 60
    # Every tick, not every other one and not only the ones that answered.
    assert nudged == [True] * 8


async def test_the_stream_backoff_gives_way_to_a_nudge(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_server: AiohttpClientMocker,
) -> None:
    """A reconnect parked on its backoff wakes when the fallback poll nudges it."""
    await init_integration(hass, config_entry)
    coordinator = config_entry.runtime_data
    coordinator._retry_stream.clear()  # noqa: SLF001

    waiter = asyncio.create_task(coordinator._async_wait_to_retry(3600))  # noqa: SLF001
    await asyncio.sleep(0)
    assert not waiter.done()

    coordinator._retry_stream.set()  # noqa: SLF001
    async with asyncio.timeout(1):
        await waiter
