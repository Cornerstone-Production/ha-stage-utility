"""A switch per on/off cue pair in the manifest."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_COMMANDED_AT,
    ATTR_CUE_OFF,
    ATTR_CUE_ON,
    ATTR_LAST_COMMANDED,
    ATTR_REASON,
    ATTR_SETTLING,
    ATTR_STATE_SOURCE,
    ATTR_TOGGLE,
    SETTLE_SECONDS,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from .coordinator import CueSwitch, Settle, StageUtilityCoordinator
from .entity import StageUtilityEntity, async_sync_entities

if TYPE_CHECKING:
    from . import StageUtilityConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StageUtilityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a switch per manifest switch, and keep the set in step with it."""
    coordinator = entry.runtime_data
    async_sync_entities(
        hass,
        entry,
        async_add_entities,
        Platform.SWITCH,
        lambda: coordinator.data.switches,
        lambda cue_id: StageUtilitySwitch(coordinator, cue_id),
    )


class StageUtilitySwitch(StageUtilityEntity, SwitchEntity):
    """One cue pair — the ON cue, the OFF cue, and what the gear reports back."""

    def __init__(self, coordinator: StageUtilityCoordinator, cue_id: str) -> None:
        """Name the switch from the manifest and suggest the room it names."""
        super().__init__(coordinator, cue_id)
        row = self._row
        self._attr_name = None if row is None else row.name
        if row is not None and row.room:
            self._attr_suggested_area = row.room
        #: Cancels the write scheduled for the end of a settle window.
        self._settle_unsub: CALLBACK_TYPE | None = None
        #: The state this switch last asked the server for, and when. Kept past
        #: the end of the settle window: what Home Assistant last asked for is
        #: the first thing worth knowing when a switch and its gear disagree.
        self._last_commanded: str | None = None
        self._commanded_at: datetime | None = None

    @property
    def _cue_name(self) -> str | None:
        """The manifest's current words for this cue, however late they change."""
        row = self._row
        return None if row is None else row.name

    @property
    def _row(self) -> CueSwitch | None:
        return self.coordinator.data.switches.get(self.cue_id)

    @property
    def available(self) -> bool:
        """False when the cue is gone, or its Companion button is missing.

        `available: false` in the manifest means the button behind the cue is no
        longer in Companion's export — pressing it would do nothing at all, and
        an operator is better told that than shown a switch that lies.
        """
        row = self._row
        return super().available and row is not None and row.available

    @property
    def is_on(self) -> bool | None:
        """On, off, or None when nothing can say."""
        row = self._row
        if row is None or row.state == STATE_UNKNOWN:
            return None
        return row.state == STATE_ON

    @property
    def _settle(self) -> Settle | None:
        """The live settle window for this cue, or None."""
        return self.coordinator.settle_of(self.cue_id)

    @property
    def settling(self) -> bool:
        """Whether this switch is showing a commanded state rather than a read."""
        return self._settle is not None

    @property
    def assumed_state(self) -> bool:
        """True while the state is unknown or commanded, not read.

        A cue with no state binding cannot be read back, so a single toggle
        would show a guess. Two buttons say plainly that Home Assistant is
        asking rather than reporting. Inside a settle window the same is true
        for a different reason: the state on show is the one just asked for, and
        the gear has not confirmed it yet.
        """
        row = self._row
        return self.settling or row is None or row.state == STATE_UNKNOWN

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Why the state is what it is, and which cues this switch calls."""
        row = self._row
        if row is None:
            return super().extra_state_attributes
        return {
            **super().extra_state_attributes,
            ATTR_REASON: row.reason,
            ATTR_STATE_SOURCE: row.state_source,
            ATTR_TOGGLE: row.toggle,
            ATTR_CUE_ON: row.on,
            ATTR_CUE_OFF: row.off,
            ATTR_SETTLING: self.settling,
            ATTR_LAST_COMMANDED: self._last_commanded,
            ATTR_COMMANDED_AT: (None if self._commanded_at is None else self._commanded_at.isoformat()),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Call the ON cue."""
        await self._async_call(on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Call the OFF cue."""
        await self._async_call(on=False)

    async def _async_call(self, *, on: bool) -> None:
        """Call a cue, then hold the state it was called for while it settles.

        `async_run_cue` raises on anything that did not run, so a failed command
        reaches none of this: no window, and no optimistic state. A press that
        landed opens one; a press that only simulated, or one the server answers
        with a state other than the one asked for, does not — that answer is a
        report about the gear, not the lag the window exists to cover.
        """
        row = self._row
        if row is None:
            return
        wanted = STATE_ON if on else STATE_OFF
        cue = row.on if on else row.off
        result = await self.async_run_cue(cue)
        if result.simulated:
            # Nothing was pressed, so nothing was commanded either: a simulated
            # call must not read as a press in any of this.
            return
        self._last_commanded = wanted
        self._commanded_at = dt_util.utcnow()
        if result.state is not None:
            # The server's own report is a read, applied before the window opens
            # so that it becomes the state the window falls back to. A state of
            # None says nothing about the gear, so nothing is written: a cue with
            # no state source keeps reading `unknown` when the window lapses.
            self.coordinator.async_apply_state(self.cue_id, result.state)
        if result.state is not None and result.state != wanted:
            return
        self.coordinator.async_begin_settle(self.cue_id, wanted)
        self._async_schedule_settle_end()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Publish new data, and drop a write scheduled for a window now over."""
        if not self.settling:
            self._async_cancel_settle_end()
        super()._handle_coordinator_update()

    @callback
    def _async_schedule_settle_end(self) -> None:
        """Write the state again the moment the window lapses.

        Nothing else would. A switch whose gear never reports back gets no
        further update at all, so `settling` and `assumed_state` would stay true
        in the state machine long after the eight seconds were up.
        """
        self._async_cancel_settle_end()
        self._settle_unsub = async_call_later(self.hass, SETTLE_SECONDS, self._async_settle_ended)

    @callback
    def _async_cancel_settle_end(self) -> None:
        if self._settle_unsub is not None:
            self._settle_unsub()
            self._settle_unsub = None

    @callback
    def _async_settle_ended(self, _now: datetime) -> None:
        """The window is up: stop holding the commanded state.

        The coordinator publishes the state it hands back, which is what writes
        this entity — there is one publisher for a switch's state and it is not
        the timer.
        """
        self._settle_unsub = None
        self.coordinator.async_end_settle(self.cue_id)

    async def async_will_remove_from_hass(self) -> None:
        """Drop the scheduled write; the entity is going away."""
        self._async_cancel_settle_end()
        await super().async_will_remove_from_hass()
