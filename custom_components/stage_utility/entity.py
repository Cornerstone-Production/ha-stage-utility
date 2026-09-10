"""Shared entity plumbing: the device, the error mapping, the registry sync."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import CueRefused, CueResult, CueUnknown, StageUtilityError
from .const import (
    ATTR_LAST_RESULT,
    DOMAIN,
    LOGGER,
    RESULT_DISPATCHED,
    RESULT_SIMULATED,
    RESULT_SKIPPED,
)
from .coordinator import StageUtilityCoordinator

if TYPE_CHECKING:
    from . import StageUtilityConfigEntry


class StageUtilityEntity(CoordinatorEntity[StageUtilityCoordinator]):
    """Anything belonging to one Stage Utility server."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StageUtilityCoordinator, cue_id: str) -> None:
        """Bind the entity to one manifest row by its stable id."""
        super().__init__(coordinator)
        self.cue_id = cue_id
        #: What this entity's last cue call landed on, or None before its first.
        self._last_result: str | None = None
        #: Whether the simulate-mode warning has already been said for the run
        #: of simulated calls this entity is currently in. A server left in
        #: simulate mode would otherwise log a line per press, forever.
        self._warned_simulated = False
        assert coordinator.config_entry is not None  # always entry-scoped; see coordinator.py
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{cue_id}"
        server = coordinator.data.server
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=server.name,
            manufacturer="Stage Utility",
            configuration_url=server.lan_url,
        )

    async def async_run_cue(self, cue: str) -> CueResult:
        """Call a cue and turn a refusal into something a person can read.

        The server's `409` body carries a sentence written for an operator —
        "The Gospel Way is live" — so it is surfaced verbatim. Every other
        failure is still raised: a cue that did not run must never read as one
        that did.
        """
        try:
            result = await self.coordinator.api.async_call_cue(cue)
        except CueRefused as err:
            raise HomeAssistantError(str(err)) from err
        except CueUnknown as err:
            raise HomeAssistantError(str(err)) from err
        except StageUtilityError as err:
            raise HomeAssistantError(f"Stage Utility could not run {cue}: {err}") from err
        self._async_note_result(cue, result)
        return result

    def _async_note_result(self, cue: str, result: CueResult) -> None:
        """Record what the call did, and say so when nothing was pressed.

        Simulate mode is not a failure — the server did exactly what it is
        configured to do, and the operator asked for the cue — so it never
        raises. But a cue that pressed nothing must not read as one that did,
        so it is logged and published as `last_result`.
        """
        if result.simulated:
            outcome = RESULT_SIMULATED
            if not self._warned_simulated:
                self._warned_simulated = True
                LOGGER.warning(
                    "Cue %s ran in Stage Utility's simulate mode; nothing was pressed",
                    cue,
                )
        else:
            self._warned_simulated = False
            if result.skipped:
                # Not a failure: the server refused to press a button the device
                # did not need. The entity is already in the state asked for.
                LOGGER.debug("Cue %s was skipped: %s", cue, result.detail)
            outcome = RESULT_SKIPPED if result.skipped else RESULT_DISPATCHED
        if outcome != self._last_result:
            self._last_result = outcome
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the last cue call this entity made actually did."""
        if self._last_result is None:
            return {}
        return {ATTR_LAST_RESULT: self._last_result}


@callback
def async_sync_entities(
    hass: HomeAssistant,
    entry: StageUtilityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    platform: str,
    rows: Callable[[], Iterable[str]],
    build: Callable[[str], Entity],
) -> None:
    """Add entities for new manifest ids, and drop entities for departed ones.

    A cue deleted in Stage Utility must not linger in Home Assistant as an
    unavailable switch forever, so it is removed from the entity registry
    outright — the operator deleted it on purpose.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _sync() -> None:
        current = set(rows())
        if new := current - known:
            known.update(new)
            async_add_entities(build(cue_id) for cue_id in sorted(new))
        if gone := known - current:
            known.difference_update(gone)
            _async_remove(hass, entry, platform, gone)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


@callback
def _async_remove(hass: HomeAssistant, entry: ConfigEntry, platform: str, cue_ids: set[str]) -> None:
    registry = er.async_get(hass)
    for cue_id in cue_ids:
        unique_id = f"{entry.entry_id}_{cue_id}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id is None:
            continue
        LOGGER.info("Cue %s is gone from the manifest; removing %s", cue_id, entity_id)
        registry.async_remove(entity_id)
