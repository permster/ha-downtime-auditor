"""Downtime Auditor: report automations missed or interrupted by HA downtime."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .auditor import DowntimeAuditor
from .labels import async_create_labels
from .const import (
    CONF_SIDEBAR_PANEL,
    CONF_PUSH_MIN_SEVERITY,
    CONF_REPAIRS_MIN_SEVERITY,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    LEGACY_OPTIONS,
    Severity,
    SERVICE_ANALYZE_WINDOW,
    SERVICE_CREATE_SEVERITY_LABELS,
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

    async def create_severity_labels(call: ServiceCall) -> ServiceResponse:
        return {"created": async_create_labels(hass)}

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
    hass.services.async_register(
        DOMAIN,
        SERVICE_CREATE_SEVERITY_LABELS,
        create_severity_labels,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_ws(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the auditor."""
    from homeassistant.loader import async_get_integration

    auditor = DowntimeAuditor(hass, entry)
    auditor.version = str((await async_get_integration(hass, DOMAIN)).version)
    hass.data[DOMAIN] = auditor
    await auditor.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if auditor.opt(CONF_SIDEBAR_PANEL):
        await async_register_panel(hass, auditor.version)
    else:
        async_unregister_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 (≤ v0.4) → v2: drop removed options, rename/remove the old category sensors."""
    if entry.version > CONFIG_ENTRY_VERSION:
        return False  # downgrade: refuse rather than guess
    if entry.version == 1:
        options = {k: v for k, v in entry.options.items() if k not in LEGACY_OPTIONS}
        data = {k: v for k, v in entry.data.items() if k not in LEGACY_OPTIONS}
        # Every automation starts unrated (Medium). Keep v0.4's behaviour of raising
        # Repairs/push for ordinary findings until the user labels and raises these.
        options.setdefault(CONF_REPAIRS_MIN_SEVERITY, Severity.MEDIUM.value)
        options.setdefault(CONF_PUSH_MIN_SEVERITY, Severity.MEDIUM.value)

        reg = er.async_get(hass)
        for key in ("possibly_missed", "unverifiable"):
            if entity_id := reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}"):
                reg.async_remove(entity_id)
        old_uid = f"{entry.entry_id}_fired_during_startup"
        if entity_id := reg.async_get_entity_id("sensor", DOMAIN, old_uid):
            changes: dict = {"new_unique_id": f"{entry.entry_id}_fired_at_startup"}
            new_id = "sensor.downtime_auditor_fired_at_startup"
            # Only rename an ID the user never customised, and never onto a taken ID.
            if entity_id == "sensor.downtime_auditor_fired_during_startup" and reg.async_get(new_id) is None:
                changes["new_entity_id"] = new_id
            reg.async_update_entity(entity_id, **changes)

        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=CONFIG_ENTRY_VERSION
        )
        _LOGGER.info("Migrated Downtime Auditor config entry to version %s", CONFIG_ENTRY_VERSION)
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
