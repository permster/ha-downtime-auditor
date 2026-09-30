"""Downtime Auditor: report automations missed or interrupted by HA downtime."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .auditor import DowntimeAuditor
from .const import (
    CONF_SIDEBAR_PANEL,
    DOMAIN,
    SERVICE_ANALYZE_WINDOW,
    SERVICE_DISMISS_REPAIRS,
    SERVICE_RESEND_LAST,
    SERVICE_SNAPSHOT_NOW,
)
from .panel import async_register_panel, async_unregister_panel
from .websocket import async_register as async_register_ws

PLATFORMS = ["sensor", "binary_sensor"]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ANALYZE_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Optional("notify", default=True): cv.boolean,
    }
)


def _auditor(hass: HomeAssistant) -> DowntimeAuditor:
    auditor = hass.data.get(DOMAIN)
    if auditor is None:
        raise HomeAssistantError("Downtime Auditor is not set up")
    return auditor


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register services once."""

    async def analyze_window(call: ServiceCall) -> ServiceResponse:
        start = call.data["start"]
        end = call.data["end"]
        tz = dt_util.get_default_time_zone()
        start = start if start.tzinfo else start.replace(tzinfo=tz)
        end = end if end.tzinfo else end.replace(tzinfo=tz)
        if end <= start:
            raise HomeAssistantError("end must be after start")
        return await _auditor(hass).async_analyze_window(start, end, call.data["notify"])

    async def resend_last(call: ServiceCall) -> None:
        if not await _auditor(hass).async_resend_last():
            raise HomeAssistantError("No report has been generated yet")

    async def snapshot_now(call: ServiceCall) -> ServiceResponse:
        return await _auditor(hass).async_snapshot_now()

    async def dismiss_repairs(call: ServiceCall) -> ServiceResponse:
        return {"dismissed": await _auditor(hass).async_dismiss_repairs()}

    hass.services.async_register(
        DOMAIN, SERVICE_ANALYZE_WINDOW, analyze_window, ANALYZE_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_RESEND_LAST, resend_last)
    hass.services.async_register(
        DOMAIN, SERVICE_SNAPSHOT_NOW, snapshot_now, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DISMISS_REPAIRS, dismiss_repairs, supports_response=SupportsResponse.OPTIONAL
    )
    async_register_ws(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the auditor."""
    auditor = DowntimeAuditor(hass, entry)
    hass.data[DOMAIN] = auditor
    await auditor.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if auditor.opt(CONF_SIDEBAR_PANEL):
        from homeassistant.loader import async_get_integration

        version = str((await async_get_integration(hass, DOMAIN)).version)
        await async_register_panel(hass, version)
    else:
        async_unregister_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the auditor."""
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    async_unregister_panel(hass)
    auditor: DowntimeAuditor | None = hass.data.pop(DOMAIN, None)
    if auditor:
        await auditor.async_stop()
    return True
