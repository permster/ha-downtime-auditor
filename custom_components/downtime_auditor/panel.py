"""Register the sidebar dashboard."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN, NAME, PANEL_COMPONENT, PANEL_STATIC_URL, PANEL_URL

_LOGGER = logging.getLogger(__name__)
FRONTEND_DIR = Path(__file__).parent / "frontend"


async def async_register_panel(hass: HomeAssistant, version: str) -> None:
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        _LOGGER.debug("Frontend/HTTP not available; skipping sidebar panel")
        return
    from homeassistant.components import frontend, panel_custom
    from homeassistant.components.http import StaticPathConfig

    if not hass.data.get(f"{DOMAIN}_static_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_STATIC_URL, str(FRONTEND_DIR), cache_headers=False)]
        )
        hass.data[f"{DOMAIN}_static_registered"] = True

    if PANEL_URL in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=NAME,
        sidebar_icon="mdi:timeline-alert-outline",
        module_url=f"{PANEL_STATIC_URL}/panel.js?v={version}",
        require_admin=True,
        config={},
    )


def async_unregister_panel(hass: HomeAssistant) -> None:
    try:
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
    except Exception:  # noqa: BLE001
        pass
