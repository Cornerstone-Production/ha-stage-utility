"""The Stage Utility integration.

One config entry per Stage Utility server. The entry owns an API client, a
coordinator holding the cue manifest, and the single event-stream subscription
that keeps it current.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StageUtilityApi
from .const import CONF_HOST, CONF_TOKEN
from .coordinator import StageUtilityCoordinator
from .homekit import async_set_up_reloader
from .panel import async_register_panel, async_remove_panel

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SWITCH]

type StageUtilityConfigEntry = ConfigEntry[StageUtilityCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: StageUtilityConfigEntry) -> bool:
    """Set up one Stage Utility server."""
    api = StageUtilityApi(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_TOKEN],
    )
    coordinator = StageUtilityCoordinator(hass, api, entry)
    # The manifest is an open read, so this fails only on a server that is not
    # there — which is ConfigEntryNotReady, raised by the coordinator itself.
    # The token is checked in the config flow and again the first time a cue is
    # called; there is nothing to authenticate against on a read.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    # The server names its own sidebar entry, so this waits for the manifest.
    async_register_panel(hass, entry, coordinator.data.server.name)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # Before the platforms: their first entity sync is what tells a newly added
    # entry's bridges they have accessories to publish.
    async_set_up_reloader(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # After the platforms, so the first state event lands on entities that exist.
    coordinator.async_start_stream()
    return True


async def _async_options_updated(hass: HomeAssistant, entry: StageUtilityConfigEntry) -> None:
    """Reload the entry so the options take effect.

    A reload rather than reaching into the running entry: the sidebar option is
    read once, at setup, and a reload is the one path that both removes a panel
    that should go and registers one that should appear.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: StageUtilityConfigEntry) -> bool:
    """Close the stream, drop the sidebar entry, and unload the platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_remove_panel(hass, entry)
        await entry.runtime_data.async_shutdown()
    return unloaded
