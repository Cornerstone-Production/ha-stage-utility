"""Constants for the Stage Utility integration."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "stage_utility"
LOGGER: Final = logging.getLogger(__package__)

CONF_HOST: Final = "host"
CONF_TOKEN: Final = "token"

#: Options, not config: a sidebar entry can be turned off without touching
#: the server or the token.
OPT_SHOW_IN_SIDEBAR: Final = "show_in_sidebar"

#: The name to fall back to when the server does not give one.
DEFAULT_NAME: Final = "Stage Utility"

#: The sidebar panel: the frontend's own iframe panel, at this path, taking
#: the framed address as `config={"url": ...}`. A second server added later
#: is suffixed with its entry id — see panel.py.
PANEL_COMPONENT: Final = "iframe"
PANEL_URL_PATH: Final = "stage-utility"
PANEL_ICON: Final = "mdi:microphone-variant"

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

#: The stream reconnect doubles from a second up to half a minute, which is the
#: longest a recovered server goes unnoticed by the stream itself.
STREAM_BACKOFF_MIN_SECONDS: Final = 1
STREAM_BACKOFF_MAX_SECONDS: Final = 30

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
