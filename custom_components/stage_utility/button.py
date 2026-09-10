"""A button per press-only cue in the manifest."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CueButton, StageUtilityCoordinator
from .entity import StageUtilityEntity, async_sync_entities

if TYPE_CHECKING:
    from . import StageUtilityConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StageUtilityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a button per manifest button, and keep the set in step with it."""
    coordinator = entry.runtime_data
    async_sync_entities(
        hass,
        entry,
        async_add_entities,
        Platform.BUTTON,
        lambda: coordinator.data.buttons,
        lambda cue_id: StageUtilityButton(coordinator, cue_id),
    )


class StageUtilityButton(StageUtilityEntity, ButtonEntity):
    """One cue with nothing to read back — pressing it is the whole story."""

    def __init__(self, coordinator: StageUtilityCoordinator, cue_id: str) -> None:
        """Name the button from the manifest and suggest the room it names."""
        super().__init__(coordinator, cue_id)
        row = self._row
        self._attr_name = None if row is None else row.name
        if row is not None and row.room:
            self._attr_suggested_area = row.room

    @property
    def _cue_name(self) -> str | None:
        """The manifest's current words for this cue, however late they change."""
        row = self._row
        return None if row is None else row.name

    @property
    def _row(self) -> CueButton | None:
        return self.coordinator.data.buttons.get(self.cue_id)

    @property
    def available(self) -> bool:
        """False when the cue is gone, or its Companion button is missing."""
        row = self._row
        return super().available and row is not None and row.available

    async def async_press(self) -> None:
        """Call the cue. A refusal surfaces the server's own sentence."""
        row = self._row
        if row is None:
            return
        await self.async_run_cue(row.cue)
