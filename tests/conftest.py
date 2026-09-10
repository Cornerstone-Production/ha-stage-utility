"""Fixtures for the Stage Utility tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from aiohttp.streams import StreamReader
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.stage_utility.const import CONF_HOST, CONF_TOKEN, DOMAIN

HOST = "http://192.168.16.61:8788"
TOKEN = "su_testtoken"

MANIFEST: dict[str, Any] = {
    "version": 42,
    "server": {"name": "Stage Utility", "lanUrl": "http://192.168.16.61:8788"},
    "switches": [
        {
            "id": "projectors",
            "name": "Projectors",
            "room": "Main Auditorium",
            "on": "projectors_on",
            "off": "projectors_off",
            "toggle": False,
            "state": "on",
            "reason": None,
            "stateSource": "MA_HL_Projector:powerState",
            "available": True,
        },
        {
            "id": "house_lights",
            "name": "House Lights",
            "room": None,
            "on": "house_lights_on",
            "off": "house_lights_off",
            "toggle": False,
            "state": "unknown",
            "reason": "No state variable is bound to this pair",
            "stateSource": None,
            "available": True,
        },
    ],
    "buttons": [
        {
            "id": "reset_ultrix",
            "name": "Reset Ultrix",
            "room": None,
            "cue": "reset_ultrix",
            "available": True,
        }
    ],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> None:
    """Let Home Assistant load `custom_components/stage_utility`."""
    return


class StreamMockResponse(AiohttpClientMockResponse):
    """A GET /api/events that stays open until the test feeds or ends it.

    The stock mocker hands back a stream that is already at EOF, which is a
    server that hung up rather than one that is streaming — the case this
    integration exists to hold open.
    """

    def __init__(self, url: str) -> None:
        """Register as a GET matcher and build the reader the test drives."""
        super().__init__("get", URL(url), status=200)
        self.stream = StreamReader(Mock(_reading_paused=False), limit=2**16)

    @property
    def content(self) -> StreamReader:
        """The one persistent reader, not a fresh copy per access."""
        return self.stream

    def send(self, event: str, data: str) -> None:
        """Write one SSE frame into the open stream."""
        self.stream.feed_data(f"event: {event}\ndata: {data}\n\n".encode())

    def hang_up(self) -> None:
        """End the stream, as a restarting server would."""
        self.stream.feed_eof()


@pytest.fixture
def event_stream(aioclient_mock: AiohttpClientMocker) -> StreamMockResponse:
    """An open `/api/events` stream, matched ahead of anything else."""
    response = StreamMockResponse(f"{HOST}/api/events")
    aioclient_mock._mocks.insert(0, response)  # noqa: SLF001
    return response


@pytest.fixture
def mock_server(aioclient_mock: AiohttpClientMocker, event_stream: StreamMockResponse) -> AiohttpClientMocker:
    """A Stage Utility answering the manifest, the subscribe and the probe."""
    aioclient_mock.get(f"{HOST}/api/cues/manifest", json=MANIFEST)
    aioclient_mock.post(f"{HOST}/api/events/subscribe", json={"ok": True})
    return aioclient_mock


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry for the fixture server."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Stage Utility",
        data={CONF_HOST: HOST, CONF_TOKEN: TOKEN},
        unique_id="192.168.16.61",
    )
    entry.add_to_hass(hass)
    return entry
