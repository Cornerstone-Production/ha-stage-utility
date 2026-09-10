"""Diagnostics for one Stage Utility config entry."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN

TO_REDACT = {CONF_TOKEN, "token", "secret"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Any
) -> dict[str, Any]:
    """The manifest, the last states and how the event stream is doing.

    The bearer token is the only secret an entry holds, and it is redacted. The
    cue names, rooms and Companion variable names are the whole point of the
    dump — they are what an operator debugging a switch needs to see.
    """
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "unique_id": entry.unique_id,
        },
        "server": asdict(data.server) if data else None,
        "manifest_version": data.version if data else None,
        "switches": [asdict(row) for row in data.switches.values()] if data else [],
        "buttons": [asdict(row) for row in data.buttons.values()] if data else [],
        "states": (
            {row.id: {"state": row.state, "reason": row.reason} for row in data.switches.values()}
            if data
            else {}
        ),
        "stream": {
            "connected": coordinator.stream_connected,
            "last_error": coordinator.stream_error,
            "client_id": coordinator.cid,
            "channel": "cues",
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
        },
    }
