"""Shared entity plumbing: the device, the error mapping, the registry sync."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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
    OPTION_NAMED_FROM,
    RESULT_DISPATCHED,
    RESULT_FAILED,
    RESULT_SIMULATED,
    RESULT_SKIPPED,
)
from .coordinator import StageUtilityCoordinator
from .homekit import async_note_entity_changes

if TYPE_CHECKING:
    from . import StageUtilityConfigEntry


class StageUtilityEntity(CoordinatorEntity[StageUtilityCoordinator]):
    """Anything belonging to one Stage Utility server."""

    # True is the truth about these names: "Projectors" describes the entity
    # alone, not the server it lives on. It does NOT keep the server's name out
    # of the friendly name — see `_async_shorten_name` for that.
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

    @property
    def _cue_name(self) -> str | None:
        """What the manifest calls this cue now, or None once it is gone."""
        return None

    async def async_added_to_hass(self) -> None:
        """Join the coordinator, then take the server's name off this entity."""
        await super().async_added_to_hass()
        self._async_shorten_name()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Follow a cue renamed in Stage Utility, then publish the new data."""
        self._async_shorten_name()
        super()._handle_coordinator_update()

    @callback
    def _async_shorten_name(self) -> None:
        """Write the cue's own words into the registry's `name`.

        Home Assistant composes a device-bound entity's friendly name as the
        device name followed by the entity name, and there is no integration-side
        way out of it: `has_entity_name = False` only tells the registry to strip
        a device-name prefix off `original_name` before composing, so a cue named
        "VCR Light" on a server named "Cornerstone Worship" reads "Cornerstone
        Worship VCR Light" either way. That is the name HomeKit publishes and the
        name Siri has to hear.

        The one name that escapes the composition is the registry's own `name` —
        the field an operator edits in the entity's settings — so the cue's words
        go there. `entity_id` is untouched: it was generated at registration and
        keeps the server's slug, which is what makes `switch.stage_utility_*`
        unambiguous across two appliances.

        Written only while the registry still holds what this integration last
        wrote, recorded in the entry's options. An operator who renames the
        switch keeps their rename, and a cue renamed in Stage Utility follows
        only until somebody does.
        """
        entry = self.registry_entry
        wanted = self._cue_name or self.name
        if entry is None or self.hass is None or not isinstance(wanted, str) or not wanted:
            return
        options: Mapping[str, Any] = entry.options.get(DOMAIN) or {}
        ours = options.get(OPTION_NAMED_FROM)
        # Cheapest first: this runs on every coordinator update, and settled is
        # the case it is in almost always.
        if entry.name == wanted and ours == wanted:
            return
        if entry.name is not None and entry.name != ours:
            return  # the operator renamed it; theirs wins
        LOGGER.debug(
            "Naming %s %r, without the server's %r in front",
            entry.entity_id,
            wanted,
            self.coordinator.data.server.name,
        )
        registry = er.async_get(self.hass)
        registry.async_update_entity(entry.entity_id, name=wanted)
        registry.async_update_entity_options(entry.entity_id, DOMAIN, {OPTION_NAMED_FROM: wanted})

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

        `ok: false` at HTTP 200 is the server saying the press itself did not
        land — Companion did not answer, or answered a failure — so it raises
        like any other failed cue. Simulate mode is not a failure: the server
        did exactly what it is configured to do, and the operator asked for the
        cue. But a cue that pressed nothing must not read as one that did, so
        both are logged and published as `last_result`.
        """
        if not result.ok:
            self._warned_simulated = False
            LOGGER.warning("Cue %s did not run: %s", cue, result.detail)
            self._publish_result(RESULT_FAILED)
            raise HomeAssistantError(f"Stage Utility could not run {cue}: {result.detail}")
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
        self._publish_result(outcome)

    def _publish_result(self, outcome: str) -> None:
        """Publish `last_result`, but only when it actually changed.

        A cue can be called on an entity that Home Assistant has not added yet,
        or has already taken away; `async_write_ha_state` raises on either.
        Recording what the call did is not worth turning a press that worked
        into an error, so the write is skipped and the attribute still stands.
        """
        if outcome == self._last_result:
            return
        self._last_result = outcome
        if self.hass is not None and self.entity_id:
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
        added = 0
        removed: list[str] = []
        if new := current - known:
            known.update(new)
            # Only cues Home Assistant has never had count as added. Reloading
            # this entry re-adds every entity it already has, and a bridge
            # already publishing them needs nothing for that.
            added = len(_async_unregistered(hass, entry, platform, new))
            async_add_entities(build(cue_id) for cue_id in sorted(new))
        if gone := known - current:
            known.difference_update(gone)
            removed = _async_remove(hass, entry, platform, gone)
        # A HomeKit Bridge rebuilds its accessory list only when it reloads, so
        # a cue added or deleted here needs one. See homekit.py.
        async_note_entity_changes(hass, entry, added, removed)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


@callback
def _async_unregistered(hass: HomeAssistant, entry: ConfigEntry, platform: str, cue_ids: set[str]) -> set[str]:
    """Which of these cues have no entity in the registry yet."""
    registry = er.async_get(hass)
    return {
        cue_id
        for cue_id in cue_ids
        if registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{cue_id}") is None
    }


@callback
def _async_remove(hass: HomeAssistant, entry: ConfigEntry, platform: str, cue_ids: set[str]) -> list[str]:
    """Take departed cues out of the registry, and say which ids went."""
    registry = er.async_get(hass)
    removed: list[str] = []
    for cue_id in cue_ids:
        unique_id = f"{entry.entry_id}_{cue_id}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id is None:
            continue
        LOGGER.info("Cue %s is gone from the manifest; removing %s", cue_id, entity_id)
        registry.async_remove(entity_id)
        removed.append(entity_id)
    return removed
