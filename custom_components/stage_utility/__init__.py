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
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # After the platforms, so the first state event lands on entities that exist.
    coordinator.async_start_stream()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: StageUtilityConfigEntry) -> bool:
    """Close the stream and unload the platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded
