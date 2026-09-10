"""Constants for the Stage Utility integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "stage_utility"
LOGGER: Final = logging.getLogger(__package__)

CONF_HOST: Final = "host"
CONF_TOKEN: Final = "token"

DEFAULT_PORT: Final = 8788
DEFAULT_SCHEME: Final = "http"

#: A cue name that cannot exist, used to prove a token is accepted. The server
#: checks the bearer token before it looks the cue up, so a 401 means the token
#: is wrong and a 404 means the token was accepted.
PROBE_CUE: Final = "__probe__"

#: The SSE channel carrying cue state and manifest events. The server only polls
#: Companion for cue state while at least one client is subscribed to it, so the
#: integration keeps exactly one subscription open for the life of the entry.
CUE_CHANNEL: Final = "cues"

#: Only used while the event stream is down. See coordinator.py.
FALLBACK_POLL_SECONDS: Final = 30

STREAM_BACKOFF_MIN_SECONDS: Final = 1
STREAM_BACKOFF_MAX_SECONDS: Final = 60

#: What the last cue call this entity made actually did. `dispatched` pressed
#: something, `skipped` found the gear already there, `simulated` means the
#: server's automation engine is in simulate mode and pressed nothing at all,
#: and `failed` is the server answering HTTP 200 with `ok: false` — the press
#: was attempted and did not land.
RESULT_DISPATCHED: Final = "dispatched"
RESULT_FAILED: Final = "failed"
RESULT_SKIPPED: Final = "skipped"
RESULT_SIMULATED: Final = "simulated"

#: Cue state values as the server spells them.
STATE_ON: Final = "on"
STATE_OFF: Final = "off"
STATE_UNKNOWN: Final = "unknown"

ATTR_REASON: Final = "reason"
ATTR_LAST_RESULT: Final = "last_result"
ATTR_STATE_SOURCE: Final = "state_source"
ATTR_TOGGLE: Final = "toggle"
ATTR_CUE_ON: Final = "cue_on"
ATTR_CUE_OFF: Final = "cue_off"
