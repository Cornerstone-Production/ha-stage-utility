"""Reload the HomeKit Bridge when the set of cue entities changes.

A cue deleted in Stage Utility leaves Home Assistant properly — the entity goes
from the registry and its state goes with it — and the Apple Home app still
shows the accessory, as "No Response", until something reloads the bridge.

That is not a bug in the removal. Home Assistant's `homekit` component derives
its accessory list exactly once per run of the bridge, in
`HomeKit.async_configure_accessories`, which walks `hass.states.async_all()`
through the entry's filter (`homeassistant/components/homekit/__init__.py`,
around line 855) and is called from `HomeKit.async_start` (line 894), itself
wired to `async_at_started` at setup (line 402). Nothing in the component
subscribes to `EVENT_ENTITY_REGISTRY_UPDATED`, and no `homekit` module tracks
entity-registry events at all: the only listeners in the package are
`async_track_state_change_event` calls inside accessory classes, which follow
the state of an accessory that already exists. So an entity that appears after
the bridge started is not published either — an ADD lags exactly as a REMOVE
does. The three things that rebuild the list are the `homekit.reload` service
(line 523, YAML only), an options change through `_async_update_listener`
(line 407), and a config-entry reload; `SIGNAL_RELOAD_ENTITIES` (line 901) only
rebuilds accessories the bridge already holds.

So a config-entry reload is what it takes, and this module schedules one,
debounced, after a sync that actually added or removed an entity. `homekit`
YAML is never touched; nothing here writes to a `homekit` entry.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, OperationNotAllowed
from homeassistant.const import Platform
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entityfilter import FILTER_SCHEMA, EntityFilter
from homeassistant.helpers.event import async_call_later
from homeassistant.util.hass_dict import HassKey

from .const import (
    DOMAIN,
    HOMEKIT_CONF_FILTER,
    HOMEKIT_CONF_MODE,
    HOMEKIT_DOMAIN,
    HOMEKIT_MODE_BRIDGE,
    HOMEKIT_RELOAD_DELAY_SECONDS,
    LOGGER,
    OPT_RELOAD_HOMEKIT,
)

if TYPE_CHECKING:
    from . import StageUtilityConfigEntry

#: Where each entry's reloader lives, keyed by config entry id.
RELOADERS: HassKey[dict[str, HomeKitReloader]] = HassKey(f"{DOMAIN}_homekit_reloaders")

#: The platforms this integration publishes. A bridge whose filter admits
#: neither has nothing of ours on it.
OUR_PLATFORMS: tuple[str, ...] = (Platform.SWITCH, Platform.BUTTON)


@callback
def async_set_up_reloader(hass: HomeAssistant, entry: StageUtilityConfigEntry) -> None:
    """Give this entry a reloader, and take it away when the entry unloads."""
    reloader = HomeKitReloader(hass, entry)
    hass.data.setdefault(RELOADERS, {})[entry.entry_id] = reloader
    entry.async_on_unload(reloader.async_shutdown)


@callback
def async_note_entity_changes(
    hass: HomeAssistant,
    entry: StageUtilityConfigEntry,
    added: int,
    removed: Collection[str],
) -> None:
    """Tell this entry's reloader what a sync just did.

    A no-op when the entry has no reloader, which is a sync arriving after the
    entry unloaded.
    """
    reloader: HomeKitReloader | None = hass.data.get(RELOADERS, {}).get(entry.entry_id)
    if reloader is not None:
        reloader.async_note_changes(added, removed)


class HomeKitReloader:
    """One debounced HomeKit Bridge reload per Stage Utility server."""

    def __init__(self, hass: HomeAssistant, entry: StageUtilityConfigEntry) -> None:
        """Start with nothing owed and no reload scheduled."""
        self.hass = hass
        self.entry = entry
        self._added = 0
        #: The entity ids of entities removed since the last reload. Kept, not
        #: counted, because they are the surest test of whether a bridge's
        #: filter had them: by the time the reload runs they are out of the
        #: registry and cannot be looked up any more.
        self._removed: set[str] = set()
        self._unsub: CALLBACK_TYPE | None = None

    @callback
    def async_note_changes(self, added: int, removed: Collection[str]) -> None:
        """Push the reload out to `HOMEKIT_RELOAD_DELAY_SECONDS` from now.

        A rename or a state change is not a reason to reload — the bridge
        follows both on its own — so only an add or a remove schedules one.
        """
        if not added and not removed:
            return
        if not self.entry.options.get(OPT_RELOAD_HOMEKIT, True):
            return
        if self.hass.state is not CoreState.running:
            # Home Assistant is still starting, so the bridge has not built its
            # accessory list yet and will build it from the entities this sync
            # just created. Reloading now would be churn during a restart.
            LOGGER.debug(
                "Not scheduling a HomeKit Bridge reload while Home Assistant is starting;"
                " the bridge builds its own list after start"
            )
            return
        self._added += added
        self._removed.update(removed)
        self._async_cancel()
        self._unsub = async_call_later(self.hass, HOMEKIT_RELOAD_DELAY_SECONDS, self._async_reload)

    @callback
    def async_shutdown(self) -> None:
        """Drop a scheduled reload and this entry's reloader with it."""
        self._async_cancel()
        self.hass.data.get(RELOADERS, {}).pop(self.entry.entry_id, None)

    @callback
    def _async_cancel(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _async_reload(self, _now: datetime) -> None:
        """Reload every loaded bridge that could be publishing our entities."""
        self._unsub = None
        added, removed = self._added, self._removed
        self._added = 0
        self._removed = set()
        ours = _async_our_entity_ids(self.hass, self.entry) | removed

        for bridge in self.hass.config_entries.async_loaded_entries(HOMEKIT_DOMAIN):
            options: dict[str, Any] = {**bridge.data, **bridge.options}
            if options.get(HOMEKIT_CONF_MODE, HOMEKIT_MODE_BRIDGE) != HOMEKIT_MODE_BRIDGE:
                # Accessory mode is one entity published on its own, paired in
                # its own right. It is not ours, and reloading it would drop a
                # working accessory for nothing.
                LOGGER.debug("Not reloading HomeKit %r: it is in accessory mode", bridge.title)
                continue
            if not _admits_any(options.get(HOMEKIT_CONF_FILTER) or {}, ours, bridge.title):
                continue
            LOGGER.info(
                'Reloading HomeKit Bridge "%s" so Home picks up %d added and %d removed switches',
                bridge.title,
                added,
                len(removed),
            )
            try:
                await self.hass.config_entries.async_reload(bridge.entry_id)
            except OperationNotAllowed as err:
                # Mid-setup or mid-unload. Say so and carry on to the next
                # bridge: Home will follow at that entry's own next reload.
                LOGGER.warning("Could not reload HomeKit Bridge %r: %s", bridge.title, err)


@callback
def _async_our_entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """Every entity this config entry currently has in the registry."""
    registry = er.async_get(hass)
    return {known.entity_id for known in er.async_entries_for_config_entry(registry, entry.entry_id)}


def _admits_any(filter_config: dict[str, Any], entity_ids: Collection[str], title: str) -> bool:
    """Whether a bridge's filter could be publishing anything of ours.

    Our own entity ids first, which is exact. A bridge that had every one of
    them and has just lost the last is caught by the second test: a filter that
    admits one of our domains wholesale admits whatever we publish in it.
    """
    try:
        entity_filter: EntityFilter = FILTER_SCHEMA(filter_config)
    except vol.Invalid as err:
        # Not ours to validate — `homekit` did, or will. Reloading a bridge we
        # cannot read the filter of costs a rebuild; not reloading one that had
        # our accessories costs "No Response" until somebody notices.
        LOGGER.debug("Cannot read the filter on HomeKit %r (%s); reloading it anyway", title, err)
        return True
    if any(entity_filter(entity_id) for entity_id in entity_ids):
        return True
    if any(entity_filter(f"{platform}.{DOMAIN}") for platform in OUR_PLATFORMS):
        return True
    LOGGER.debug("Not reloading HomeKit %r: its filter admits none of our entities", title)
    return False
