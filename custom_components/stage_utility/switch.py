"""A switch per on/off cue pair in the manifest."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_CUE_OFF,
    ATTR_CUE_ON,
    ATTR_REASON,
    ATTR_STATE_SOURCE,
    ATTR_TOGGLE,
    STATE_ON,
    STATE_UNKNOWN,
)
from .coordinator import CueSwitch, StageUtilityCoordinator
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
    def assumed_state(self) -> bool:
        """True while the state is unknown, so the card offers both buttons.

        A cue with no state binding cannot be read back, so a single toggle
        would show a guess. Two buttons say plainly that Home Assistant is
        asking rather than reporting.
        """
        row = self._row
        return row is None or row.state == STATE_UNKNOWN

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
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Call the ON cue."""
        await self._async_call(on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Call the OFF cue."""
        await self._async_call(on=False)

    async def _async_call(self, *, on: bool) -> None:
        row = self._row
        if row is None:
            return
        cue = row.on if on else row.off
        result = await self.async_run_cue(cue)
        # Only what the SERVER says it landed on. Nothing is assumed here: a
        # cue with no state source reads `unknown` for as long as that is true,
        # and `assumed_state` is what tells the operator so. Guessing "on"
        # because we asked for on is the optimism the state source exists to
        # replace.
        if result.state is not None:
            self.coordinator.async_apply_state(self.cue_id, result.state)
