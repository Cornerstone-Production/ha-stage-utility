"""The sidebar entry: Stage Utility's own web UI, framed inside Home Assistant.

Stage Utility serves a full web interface, and an operator running a service
should not have to leave Home Assistant to reach it. The panel is an `iframe`
built-in panel pointing at the configured server.

Framing works because Stage Utility sends no `X-Frame-Options` and no CSP
`frame-ancestors`. It does not work when Home Assistant itself is served over
HTTPS and the server is plain HTTP: the browser blocks the mixed content, and
the frontend's iframe panel renders an error screen rather than the server. In
that case no panel is registered at all — see `_hass_uses_https`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import (
    CONF_HOST,
    DEFAULT_NAME,
    DOMAIN,
    LOGGER,
    OPT_SHOW_IN_SIDEBAR,
    PANEL_COMPONENT,
    PANEL_ICON,
    PANEL_URL_PATH,
)

#: The panel path each entry actually registered, keyed by entry id. Recorded
#: rather than recomputed: the path depends on what was already taken when the
#: entry was set up, so recomputing it at unload could remove somebody else's
#: panel and leave this one in the sidebar forever.
PANEL_PATHS: HassKey[dict[str, str]] = HassKey(f"{DOMAIN}_panel_paths")


@callback
def _hass_uses_https(hass: HomeAssistant) -> bool:
    """Whether a browser reaches this Home Assistant over HTTPS.

    Three ways it can, and all of them break framing a plain-HTTP server:
    Home Assistant terminating TLS itself (`api.use_ssl`), or a proxy doing it
    in front of either configured URL.
    """
    if hass.config.api is not None and hass.config.api.use_ssl:
        return True
    return any(url and urlsplit(url).scheme == "https" for url in (hass.config.external_url, hass.config.internal_url))


@callback
def async_register_panel(hass: HomeAssistant, entry: ConfigEntry, server_name: str) -> None:
    """Put this server in the sidebar, unless something says not to.

    Silent about the option being off — that is the operator's own choice, made
    in the options flow, and needs no explaining. Loud about the HTTPS case,
    which nobody chose and which otherwise reads as a feature that just does
    not work.
    """
    if not entry.options.get(OPT_SHOW_IN_SIDEBAR, True):
        LOGGER.debug("Sidebar entry for %s is switched off in its options", server_name)
        return

    url = str(entry.data[CONF_HOST])
    if _hass_uses_https(hass) and urlsplit(url).scheme != "https":
        LOGGER.info(
            "Not adding %s to the sidebar: Home Assistant is served over HTTPS and %s is plain HTTP, "
            "which a browser will not frame. Open the server directly at %s instead",
            server_name,
            url,
            url,
        )
        return

    # First entry set up takes the plain path; a second server is suffixed with
    # its entry id so both can sit in the sidebar at once.
    path = PANEL_URL_PATH
    if frontend.async_panel_exists(hass, path):
        path = f"{PANEL_URL_PATH}-{entry.entry_id[:8]}"

    frontend.async_register_built_in_panel(
        hass,
        component_name=PANEL_COMPONENT,
        sidebar_title=server_name or DEFAULT_NAME,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=path,
        config={"url": url},
        require_admin=False,
        update=True,
    )
    hass.data.setdefault(PANEL_PATHS, {})[entry.entry_id] = path
    LOGGER.info("Sidebar entry %r added at /%s, framing %s", server_name, path, url)


@callback
def async_remove_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take this entry's panel out of the sidebar, if it put one there."""
    path = hass.data.get(PANEL_PATHS, {}).pop(entry.entry_id, None)
    if path is not None:
        frontend.async_remove_panel(hass, path)
