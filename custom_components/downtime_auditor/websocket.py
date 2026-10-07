"""Websocket API used by the sidebar dashboard."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PUSH_MIN_SEVERITY,
    CONF_REPAIRS_MIN_SEVERITY,
    CONF_SHOW_SEVERITY_NONE,
    DOMAIN,
    HISTORY_FILE,
    REPORT_DIR,
    Severity,
)
from .labels import async_create_labels, async_set_severity
from .report import upgrade_legacy
from .snapshot import capture_running

REPORT_FILE_RE = re.compile(r"^report-\d{8}-\d{6}(-\d{1,4})?\.json$")


def _auditor(hass: HomeAssistant):
    return hass.data.get(DOMAIN)


@callback
def async_register(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_ws_registered"):
        return
    hass.data[f"{DOMAIN}_ws_registered"] = True
    websocket_api.async_register_command(hass, ws_status)
    websocket_api.async_register_command(hass, ws_report)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_what_if)
    websocket_api.async_register_command(hass, ws_set_severity)
    websocket_api.async_register_command(hass, ws_create_severity_labels)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/status"})
@callback
def ws_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    auditor = _auditor(hass)
    if auditor is None:
        connection.send_error(msg["id"], "not_loaded", "Downtime Auditor is not set up")
        return
    sess = (auditor.data or {}).get("session") or {}
    baseline = (auditor.data or {}).get("baseline") or {}
    connection.send_result(
        msg["id"],
        {
            "tracking": auditor.active,
            "pending": auditor.pending(),
            "version": auditor.version,
            "session_started": sess.get("setup_at"),
            "last_heartbeat": sess.get("last_heartbeat"),
            "heartbeat_interval": auditor.opt("heartbeat_interval"),
            "startup_delay": auditor.opt("startup_delay"),
            "baseline_entities": len(baseline.get("entities") or {}),
            "baseline_templates": len(baseline.get("templates") or {}),
            "baseline_automations": len(baseline.get("automations") or {}),
            "running_now": capture_running(hass, auditor.opt("include_scripts")),
            "json_enabled": bool(auditor.opt("write_json")),
            "repairs_min_severity": auditor.opt(CONF_REPAIRS_MIN_SEVERITY),
            "push_min_severity": auditor.opt(CONF_PUSH_MIN_SEVERITY),
            "show_severity_none": bool(auditor.opt(CONF_SHOW_SEVERITY_NONE)),
            "now": dt_util.utcnow().isoformat(),
        },
    )


def _read_report(base: Path, name: str | None) -> dict | None:
    path = base / "last_report.json" if not name else base / "reports" / name
    if not path.exists():
        return None
    return upgrade_legacy(json.loads(path.read_text(encoding="utf-8")))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/report", vol.Optional("file"): str}
)
@websocket_api.async_response
async def ws_report(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    auditor = _auditor(hass)
    name = msg.get("file")
    if not name:
        connection.send_result(msg["id"], auditor.last_report if auditor else None)
        return
    if not REPORT_FILE_RE.match(name):
        connection.send_error(msg["id"], "invalid_format", "Bad report file name")
        return
    base = Path(hass.config.path(REPORT_DIR))
    report = await hass.async_add_executor_job(_read_report, base, name)
    if report is None:
        connection.send_error(msg["id"], "not_found", "Report file no longer exists")
        return
    connection.send_result(msg["id"], report)


def _read_history(base: Path, limit: int) -> list[dict]:
    path = base / HISTORY_FILE
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            item = upgrade_legacy(json.loads(line))
        except ValueError:
            continue
        name = item.get("file")
        item["available"] = bool(name) and (base / "reports" / str(name)).exists()
        out.append(item)
    return list(reversed(out))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/history", vol.Optional("limit", default=100): vol.All(int, vol.Range(1, 5000))}
)
@websocket_api.async_response
async def ws_history(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    base = Path(hass.config.path(REPORT_DIR))
    items = await hass.async_add_executor_job(_read_history, base, msg["limit"])
    connection.send_result(msg["id"], items)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/what_if",
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
    }
)
@websocket_api.async_response
async def ws_what_if(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    auditor = _auditor(hass)
    if auditor is None:
        connection.send_error(msg["id"], "not_loaded", "Downtime Auditor is not set up")
        return
    tz = dt_util.get_default_time_zone()
    start: Any = msg["start"]
    end: Any = msg["end"]
    start = start if start.tzinfo else start.replace(tzinfo=tz)
    end = end if end.tzinfo else end.replace(tzinfo=tz)
    if end <= start:
        connection.send_error(msg["id"], "invalid_format", "End must be after start")
        return
    report = await auditor.async_analyze_window(start, end, notify=False)
    connection.send_result(msg["id"], report)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_severity",
        vol.Required("entity_id"): vol.All(cv.entity_id, cv.entity_domain(("automation", "script"))),
        vol.Required("severity"): vol.Any(None, vol.In([s.value for s in Severity])),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_severity(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Rate an automation/script by swapping its downtime_auditor_sev label (null = unrated)."""
    severity = Severity(msg["severity"]) if msg["severity"] else None
    try:
        async_set_severity(hass, msg["entity_id"], severity)
    except ValueError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    if (auditor := _auditor(hass)) is not None:
        await auditor.async_rerate_last_report()
    connection.send_result(msg["id"], {"entity_id": msg["entity_id"], "severity": msg["severity"]})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/create_severity_labels"})
@websocket_api.require_admin
@callback
def ws_create_severity_labels(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Dashboard button: create any missing severity labels."""
    connection.send_result(msg["id"], {"created": async_create_labels(hass)})
