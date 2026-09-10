"""The manifest, the event stream, and the fallback poll.

Stage Utility pushes cue state down its single Server-Sent Events stream, and it
only reads the underlying gear while somebody is subscribed to the `cues`
channel. So this integration holds exactly one subscription open for the life of
the config entry, and polls `/api/cues/states` only while that stream is down.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import CannotConnect, StageUtilityApi, StageUtilityError
from .const import (
    CUE_CHANNEL,
    FALLBACK_POLL_SECONDS,
    LOGGER,
    SETTLE_SECONDS,
    STATE_UNKNOWN,
    STREAM_BACKOFF_MAX_SECONDS,
    STREAM_BACKOFF_MIN_SECONDS,
)

#: An SSE event larger than this is a server that has lost its mind, or a proxy
#: injecting something. Drop the buffer rather than grow it without bound.
MAX_EVENT_BYTES = 1_000_000

#: No total timeout on the stream — it is meant to stay open for weeks. A read
#: timeout longer than the server's 20 s heartbeat catches a half-open socket.
STREAM_TIMEOUT = ClientTimeout(total=None, sock_connect=10, sock_read=60)

#: The fallback poll backs off this far, so an appliance switched off overnight
#: is not asked for its cue states 2,880 times before morning. A minute is the
#: trade: a recovered server is polled within a minute of coming back, and a
#: twelve-hour outage costs at most 720 fallback requests.
MAX_FALLBACK_POLL_SECONDS = 60

#: How long the server may be unreachable before it is worth more than the one
#: INFO line the drop already logged. One WARNING, then silence until recovery.
UNREACHABLE_WARNING = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class CueSwitch:
    """One on/off cue pair, as the manifest describes it."""

    id: str
    name: str
    room: str | None
    on: str
    off: str
    toggle: bool
    state: str
    reason: str | None
    state_source: str | None
    available: bool


@dataclass(frozen=True, slots=True)
class CueButton:
    """One press-only cue."""

    id: str
    name: str
    room: str | None
    cue: str
    available: bool


@dataclass(slots=True)
class Settle:
    """A commanded state being given a moment to actually come true.

    Companion polls the plug behind a cue on its own interval, so the first
    state read after a press still carries the value from before it. Believing
    that read flips the switch straight back, which invites another tap, and a
    fast sequence of taps lands on the wrong state. So the commanded state is
    shown for `SETTLE_SECONDS` and one contradicting read is let go by.

    One, not all of them. A contradiction that repeats is not Companion lagging,
    it is somebody at the wall — and that has to get through, or the integration
    would be substituting its own optimism for what the gear reports, which is
    the thing a state source exists to replace. For the same reason the window
    is an exception with an end: when it lapses with nothing confirmed, the last
    thing actually read comes back, even when that is `unknown`.
    """

    #: The state that was commanded: what the switch shows while this holds.
    state: str
    #: When the command landed.
    at: datetime
    #: State and reason the switch showed before the command, restored if the
    #: window lapses without the gear ever reporting.
    previous: tuple[str, str | None]
    #: How many consecutive reads have contradicted `state`.
    contradictions: int = 0
    #: The contradicting read this window swallowed, if any. It stands once the
    #: window is over: it is the only thing that was actually read.
    held: tuple[str, str | None] | None = None


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """Who answered, and where a person can open it."""

    name: str
    lan_url: str | None


@dataclass(slots=True)
class StageUtilityData:
    """Everything the entities read."""

    version: int
    server: ServerInfo
    switches: dict[str, CueSwitch] = field(default_factory=dict)
    buttons: dict[str, CueButton] = field(default_factory=dict)


def _parse_manifest(body: dict[str, Any], fallback_url: str) -> StageUtilityData:
    """Turn the manifest payload into typed data, tolerating missing keys.

    Anything unparseable is skipped rather than taking the whole manifest down:
    one malformed row must not cost the operator every other switch.
    """
    server_raw = body.get("server") or {}
    server = ServerInfo(
        name=str(server_raw.get("name") or "Stage Utility"),
        lan_url=server_raw.get("lanUrl") or fallback_url,
    )
    switches: dict[str, CueSwitch] = {}
    for row in body.get("switches") or []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            LOGGER.debug("Skipping unusable manifest switch row: %s", row)
            continue
        switches[row["id"]] = CueSwitch(
            id=row["id"],
            name=str(row.get("name") or row["id"]),
            room=row.get("room"),
            on=str(row.get("on") or ""),
            off=str(row.get("off") or ""),
            toggle=bool(row.get("toggle", False)),
            state=str(row.get("state") or STATE_UNKNOWN),
            reason=row.get("reason"),
            state_source=row.get("stateSource"),
            available=bool(row.get("available", True)),
        )
    buttons: dict[str, CueButton] = {}
    for row in body.get("buttons") or []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            LOGGER.debug("Skipping unusable manifest button row: %s", row)
            continue
        buttons[row["id"]] = CueButton(
            id=row["id"],
            name=str(row.get("name") or row["id"]),
            room=row.get("room"),
            cue=str(row.get("cue") or row["id"]),
            available=bool(row.get("available", True)),
        )
    return StageUtilityData(
        version=int(body.get("version") or 0),
        server=server,
        switches=switches,
        buttons=buttons,
    )


class StageUtilityCoordinator(DataUpdateCoordinator[StageUtilityData]):
    """Holds the manifest; the stream keeps it current."""

    def __init__(self, hass: HomeAssistant, api: StageUtilityApi, entry: Any) -> None:
        """Create a push coordinator — no update interval, the stream drives it."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name="Stage Utility cues",
            update_interval=None,
        )
        self.api = api
        #: The client id this connection reports, so the server can filter its
        #: fan-out down to the one channel we render.
        self.cid = uuid4().hex
        #: None until the first connection attempt has resolved either way.
        self.stream_connected: bool | None = None
        self.stream_error: str | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._fallback_task: asyncio.Task[None] | None = None
        #: When the server first stopped answering, or None while it answers.
        self._unreachable_since: datetime | None = None
        #: Whether this outage has already had its one WARNING.
        self._unreachable_warned = False
        #: Live settle windows, keyed by cue id. See `Settle`.
        self._settling: dict[str, Settle] = {}

    # ── Manifest ──────────────────────────────────────────────────────────

    async def _async_update_data(self) -> StageUtilityData:
        """Refetch the manifest. Called on setup, on a `manifest` event, and on
        every stream reconnect (where anything could have changed while we were
        not listening)."""
        try:
            body = await self.api.async_get_manifest()
        except StageUtilityError as err:
            raise UpdateFailed(str(err)) from err
        return _parse_manifest(body, self.api.base_url)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def async_start_stream(self) -> None:
        """Open the one subscription this integration holds."""
        if self._stream_task is None:
            # The base class types config_entry as optional for coordinators that
            # are not entry-scoped; this one always is — it is constructed from
            # async_setup_entry and never outlives its entry.
            assert self.config_entry is not None
            self._stream_task = self.config_entry.async_create_background_task(
                self.hass, self._stream_loop(), f"{self.name} event stream"
            )

    async def async_shutdown(self) -> None:
        """Stop the stream and any fallback poll, then let the base class go."""
        for task in (self._stream_task, self._fallback_task):
            if task is not None:
                task.cancel()
        self._stream_task = None
        self._fallback_task = None
        await super().async_shutdown()

    # ── Stream ────────────────────────────────────────────────────────────

    async def _stream_loop(self) -> None:
        """Stay subscribed to the `cues` channel, forever, with backoff."""
        delay = STREAM_BACKOFF_MIN_SECONDS
        while True:
            try:
                await self._stream_once()
                # A clean end of stream is still a disconnect: the server
                # restarted, or a proxy closed us out.
                self._set_connected(False, "The server closed the event stream")
            except asyncio.CancelledError:
                raise
            except (ClientError, TimeoutError, StageUtilityError, OSError) as err:
                self._set_connected(False, str(err) or type(err).__name__)
            except Exception:  # noqa: BLE001 — the loop must outlive a surprise
                LOGGER.exception("Unexpected failure on the Stage Utility stream")
                self._set_connected(False, "Unexpected failure; see the log")
            else:
                delay = STREAM_BACKOFF_MIN_SECONDS
            await asyncio.sleep(delay)
            delay = min(delay * 2, STREAM_BACKOFF_MAX_SECONDS)

    async def _stream_once(self) -> None:
        """One connection: open it, subscribe, then read until it ends."""
        url = self.api.url("/api/events")
        async with self.api.session.get(url, params={"cid": self.cid}, timeout=STREAM_TIMEOUT) as response:
            if response.status != 200:
                raise CannotConnect(f"/api/events answered HTTP {response.status}")
            # Report the channel filter only once the stream is actually open —
            # the server keys the filter by cid and drops it when the socket
            # closes, so subscribing first would set a filter for nothing.
            await self._async_subscribe()
            self._set_connected(True, None)
            await self._async_refetch_on_reconnect()
            await self._read_events(response)

    async def _async_refetch_on_reconnect(self) -> None:
        """Read the manifest again, and only then call the server reachable.

        Anything could have changed while we were away, and the server replays
        no history. `async_refresh`, not the debounced request: a reconnect is
        a fact, not a nudge, and the debouncer's cooldown would swallow the one
        refetch that matters.

        Accepting a socket is not answering. A server that takes the connection
        and then 500s the manifest reconnects over and over, and clearing the
        outage on the socket alone reset the clock on every one of them — so an
        outage that never ended never reached its five-minute WARNING.
        """
        await self.async_refresh()
        if self.last_update_success:
            self._note_reachable()

    async def _async_subscribe(self) -> None:
        """Tell the server this connection only wants the `cues` channel."""
        async with self.api.session.post(
            self.api.url("/api/events/subscribe"),
            json={"cid": self.cid, "channels": [CUE_CHANNEL]},
            timeout=ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                raise CannotConnect(f"/api/events/subscribe answered HTTP {response.status}")

    async def _read_events(self, response: Any) -> None:
        """Parse the SSE framing and hand each `cues` payload on.

        aiohttp's own line reader raises on a line longer than its limit, which
        would look like a protocol error rather than a runaway server, so the
        framing is parsed here with an explicit cap.
        """
        buffer = bytearray()
        event = ""
        data: list[str] = []
        async for chunk in response.content.iter_any():
            buffer.extend(chunk)
            if len(buffer) > MAX_EVENT_BYTES:
                raise CannotConnect("Event stream sent an oversized frame")
            while (index := buffer.find(b"\n")) != -1:
                raw = bytes(buffer[:index]).decode("utf-8", "replace").rstrip("\r")
                del buffer[: index + 1]
                if not raw:  # blank line ends the event
                    if event == CUE_CHANNEL and data:
                        await self._handle_cue_event("\n".join(data))
                    event, data = "", []
                elif raw.startswith(":"):
                    continue  # heartbeat comment
                elif raw.startswith("event:"):
                    event = raw[6:].strip()
                elif raw.startswith("data:"):
                    data.append(raw[5:].lstrip())

    async def _handle_cue_event(self, payload: str) -> None:
        """Apply one `cues` event to the held manifest."""
        try:
            body = json.loads(payload)
        except ValueError:
            LOGGER.warning("Ignoring an unparseable cues event")
            return
        if not isinstance(body, dict):
            return
        kind = body.get("type")
        if kind == "manifest":
            LOGGER.info(
                "Stage Utility cue list changed (version %s); refetching",
                body.get("version"),
            )
            await self.async_refresh()
            return
        if kind != "state":
            return
        self.async_apply_state(body.get("id"), body.get("state"), body.get("reason"))

    def async_apply_state(
        self,
        cue_id: Any,
        state: Any,
        reason: Any = None,
    ) -> None:
        """Set one switch's state from a read, unless a settle window holds it.

        Every observed state reaches this one method — the SSE `state` event and
        the fallback poll both — which is why the settle window is resolved here
        rather than in the entity. A read the window swallows never reaches the
        held data at all, so the NEXT read of the same value is still a change
        and still arrives: counting consecutive contradictions on the entity
        side would have missed the second one, because an update that changes
        nothing is dropped before any listener sees it.
        """
        data = self.data
        if data is None or not isinstance(cue_id, str):
            return
        if data.switches.get(cue_id) is None:
            LOGGER.debug("State event for unknown cue %s; ignoring", cue_id)
            return
        observed = str(state or STATE_UNKNOWN)
        read = (observed, reason if isinstance(reason, str) else None)
        # Whether this read ended a settle window. `settling` is an attribute
        # of its own, so the entities have to be told even when the read agreed
        # with what they were already showing and the data did not move.
        ended = False
        if (settle := self._settling.get(cue_id)) is not None:
            if self._settle_lapsed(settle):
                # Lapsed but never swept: the read below is what stands, and the
                # record must go with it or the switch's own end-of-window write
                # would put the held state back over the top of it.
                del self._settling[cue_id]
                ended = True
            elif observed == settle.state:
                LOGGER.debug("Cue %s read %s, as commanded; settle window over", cue_id, observed)
                del self._settling[cue_id]
                ended = True
            elif settle.contradictions == 0:
                settle.contradictions = 1
                settle.held = read
                LOGGER.debug(
                    "Cue %s read %s inside its settle window but %s was commanded; holding",
                    cue_id,
                    observed,
                    settle.state,
                )
                return
            else:
                LOGGER.debug(
                    "Cue %s read %s twice running; taking it over the commanded %s",
                    cue_id,
                    observed,
                    settle.state,
                )
                del self._settling[cue_id]
                ended = True
        self._async_set_state(cue_id, *read, force=ended)

    def _async_set_state(self, cue_id: str, state: str, reason: str | None = None, *, force: bool = False) -> None:
        """Write one switch's state into the held data and publish it.

        `force` publishes data that did not move, which is how a settle window
        opening or closing reaches the entities: the state on show can be the
        same either side of it and only the `settling` attribute changed.
        """
        data = self.data
        if data is None or (current := data.switches.get(cue_id)) is None:
            return
        updated = replace(current, state=state, reason=reason)
        if updated == current and not force:
            return
        data.switches[cue_id] = updated
        self.async_set_updated_data(data)

    # ── Settle windows ────────────────────────────────────────────────────

    @staticmethod
    def _settle_lapsed(settle: Settle) -> bool:
        return dt_util.utcnow() - settle.at >= timedelta(seconds=SETTLE_SECONDS)

    def settle_of(self, cue_id: str) -> Settle | None:
        """The live settle window for a cue, or None once it has ended.

        A lapsed record is left in place rather than swept here: this is read
        from entity properties, which must not change anything, and the switch
        that opened the window has a write scheduled for the moment it lapses.
        """
        settle = self._settling.get(cue_id)
        if settle is None or self._settle_lapsed(settle):
            return None
        return settle

    def async_begin_settle(self, cue_id: str, state: str) -> None:
        """Show a commanded state and hold it for `SETTLE_SECONDS`."""
        data = self.data
        if data is None or (row := data.switches.get(cue_id)) is None:
            return
        self._settling[cue_id] = Settle(
            state=state,
            at=dt_util.utcnow(),
            previous=(row.state, row.reason),
        )
        LOGGER.debug("Cue %s commanded %s; settling for %s s", cue_id, state, SETTLE_SECONDS)
        self._async_set_state(cue_id, state, row.reason, force=True)

    def async_end_settle(self, cue_id: str) -> None:
        """Stop holding a commanded state; go back to what was last read.

        Called when the window lapses. The swallowed contradiction stands if
        there was one — it is the only thing the gear actually said — and
        otherwise the state from before the command comes back, because a press
        nobody confirmed is not evidence about the gear however recent it is.
        """
        settle = self._settling.pop(cue_id, None)
        if settle is None:
            return
        state, reason = settle.held or settle.previous
        LOGGER.debug("Cue %s settle window is up; back to %s", cue_id, state)
        self._async_set_state(cue_id, state, reason, force=True)

    def async_settle_report(self) -> dict[str, dict[str, Any]]:
        """Every live settle window, for diagnostics.

        A switch showing a state the gear has not confirmed is exactly what an
        operator is looking at when they say Home and the device disagree, so
        the dump says which switches are in that state and what they are holding.
        """
        return {
            cue_id: {
                "commanded": settle.state,
                "at": settle.at.isoformat(),
                "contradictions": settle.contradictions,
                "held": None if settle.held is None else settle.held[0],
            }
            for cue_id in list(self._settling)
            if (settle := self.settle_of(cue_id)) is not None
        }

    # ── Reachability ──────────────────────────────────────────────────────

    @property
    def unreachable_since(self) -> datetime | None:
        """When the server first stopped answering, or None while it answers.

        Read by diagnostics: `last_update_success: false` says the server is
        not there, but not whether that started a minute ago or on Friday.
        """
        return self._unreachable_since

    def _note_unreachable(self, err: StageUtilityError) -> None:
        """The server is not answering: mark the update failed.

        With the stream down AND the fallback poll failing, nothing knows what
        the gear is doing. Leaving `last_update_success` True would keep every
        switch available and showing whatever it last heard — a control that
        lies about a server that is not there. `async_set_update_error` makes
        them `unavailable`, and logs its own line once per outage.
        """
        now = dt_util.utcnow()
        if self._unreachable_since is None:
            self._unreachable_since = now
        elif not self._unreachable_warned and (elapsed := now - self._unreachable_since) >= UNREACHABLE_WARNING:
            self._unreachable_warned = True
            # The elapsed time, not the threshold: the poll backs off, so the
            # tick that crosses five minutes can be well past it, and a line
            # that always says "5 min" is reporting the constant.
            LOGGER.warning(
                "Stage Utility at %s has been unreachable for %d min; its switches are unavailable",
                self.api.base_url,
                elapsed.total_seconds() // 60,
            )
        self.async_set_update_error(err)

    def _note_reachable(self) -> None:
        """The server answered again: clear the outage and republish the data.

        Called from both paths that prove reachability — a successful fallback
        poll, and a stream reconnect whose manifest refetch came back — because
        either one is enough to bring the entities back, and an operator should
        not have to wait for the other. The socket on its own proves nothing.
        """
        if self._unreachable_since is None:
            return
        self._unreachable_since = None
        self._unreachable_warned = False
        LOGGER.info("Stage Utility at %s is answering again", self.api.base_url)
        if self.data is not None:
            self.async_set_updated_data(self.data)

    # ── Fallback poll ─────────────────────────────────────────────────────

    def _set_connected(self, connected: bool, error: str | None) -> None:
        """Record the stream's status, logging and switching the poll on a change."""
        self.stream_error = error
        if connected == self.stream_connected:
            return
        self.stream_connected = connected
        if connected:
            LOGGER.info("Subscribed to the Stage Utility cue stream at %s", self.api.base_url)
            self._stop_fallback()
        else:
            LOGGER.info(
                "Stage Utility cue stream at %s is down (%s); polling every %s s until it returns",
                self.api.base_url,
                error or "no reason given",
                FALLBACK_POLL_SECONDS,
            )
            self._start_fallback()

    def _start_fallback(self) -> None:
        if self._fallback_task is None or self._fallback_task.done():
            assert self.config_entry is not None  # see async_start_stream above
            self._fallback_task = self.config_entry.async_create_background_task(
                self.hass, self._fallback_loop(), f"{self.name} fallback poll"
            )

    def _stop_fallback(self) -> None:
        if self._fallback_task is not None:
            self._fallback_task.cancel()
            self._fallback_task = None

    async def _fallback_loop(self) -> None:
        """Poll `/api/cues/states` while the stream is down, and only then.

        Backs off on failure so an appliance that is switched off overnight is
        not asked for its cue states 2,880 times before morning.
        """
        delay = FALLBACK_POLL_SECONDS
        # The flag is checked BEFORE the first sleep, so a drop is answered at
        # once rather than leaving every switch stale for half a minute — and so
        # a loop started against a healthy stream does nothing at all.
        while not self.stream_connected:
            if await self.async_poll_states_once():
                delay = FALLBACK_POLL_SECONDS
            else:
                delay = min(delay * 2, MAX_FALLBACK_POLL_SECONDS)
                LOGGER.debug("Fallback poll failed; next in %s s", delay)
            await asyncio.sleep(delay)

    async def async_poll_states_once(self) -> bool:
        """Read `/api/cues/states` and apply it. False when it could not be read."""
        try:
            body = await self.api.async_get_states()
        except StageUtilityError as err:
            LOGGER.debug("Fallback poll of cue states failed: %s", err)
            # Both halves of the condition, checked here rather than trusted to
            # the caller: a poll failing while the stream is still delivering
            # state is one unanswered request, not a server that is gone.
            if not self.stream_connected:
                self._note_unreachable(err)
            return False
        self._note_reachable()
        states = body.get("states")
        if isinstance(states, dict):
            for cue_id, row in states.items():
                if isinstance(row, dict):
                    self.async_apply_state(cue_id, row.get("state"), row.get("reason"))
        return True
