"""Build and deliver downtime reports."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .analyzer import Finding, Window
from .const import (
    CAT_INTERRUPTED,
    CAT_MISSED,
    CAT_POSSIBLE,
    CAT_SKIPPED,
    CAT_STARTUP_FIRED,
    CAT_UNVERIFIABLE,
    HISTORY_FILE,
    MAX_REPORT_FILES,
    NAME,
    PERSISTENT_NOTIFICATION_ID,
    REPORT_DIR,
)
from .schedule import fmt_duration

_LOGGER = logging.getLogger(__name__)
REPORT_NAME_RE = re.compile(r"^report-(\d{8}-\d{6})(?:-\d+)?\.json$")

SECTION_ORDER = [
    (CAT_INTERRUPTED, "Interrupted mid-run"),
    (CAT_MISSED, "Missed triggers"),
    (CAT_POSSIBLE, "Possibly missed"),
    (CAT_STARTUP_FIRED, "Fired during startup (check these weren't spurious)"),
    (CAT_UNVERIFIABLE, "Can't be verified (event-style triggers)"),
]

CONF_BADGE = {"high": "", "medium": " _(medium confidence)_", "low": " _(low confidence)_"}


def _local(value: Any) -> str:
    if value is None:
        return "?"
    dt = value if hasattr(value, "tzinfo") else dt_util.parse_datetime(str(value))
    if dt is None:
        return str(value)
    return dt_util.as_local(dt).strftime("%Y-%m-%d %H:%M:%S")


def build_report(
    window: Window,
    findings: list[Finding],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the structured report."""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.category] = counts.get(f.category, 0) + 1
    actionable = sum(counts.get(c, 0) for c in (CAT_INTERRUPTED, CAT_MISSED, CAT_POSSIBLE))
    return {
        "schema": 1,
        "generated_at": dt_util.utcnow().isoformat(),
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "start_local": _local(window.start),
            "end_local": _local(window.end),
            "duration_seconds": round(window.seconds, 1),
            "duration": fmt_duration(window.seconds),
            "clean_shutdown": window.clean,
            "start_basis": "recorded shutdown time"
            if window.clean
            else "last heartbeat before an unclean stop (crash, power loss, kill)",
        },
        "counts": counts,
        "actionable": actionable,
        "meta": meta,
        "findings": [f.as_dict() for f in findings],
    }


def to_markdown(report: dict[str, Any], json_path: str | None) -> str:
    """Persistent-notification body."""
    w = report["window"]
    lines = [
        f"**Down:** {w['start_local']} → {w['end_local']} (**{w['duration']}**)",
        f"**Shutdown:** {'clean' if w['clean_shutdown'] else '⚠️ unclean — window start is the last heartbeat'}",
    ]
    meta = report.get("meta") or {}
    if meta.get("ha_version_before") and meta.get("ha_version_before") != meta.get("ha_version_after"):
        lines.append(f"**HA version:** {meta['ha_version_before']} → {meta['ha_version_after']}")
    lines.append(
        f"**Automations checked:** {meta.get('automations_checked', '?')} · "
        f"**Triggers checked:** {meta.get('triggers_checked', '?')}"
    )
    if not meta.get("baseline_available", True):
        lines.append("⚠️ No pre-downtime baseline was available (first run after install?), so state checks are limited.")

    by_cat: dict[str, list[dict]] = {}
    for f in report["findings"]:
        by_cat.setdefault(f["category"], []).append(f)

    if not report["actionable"] and not by_cat.get(CAT_STARTUP_FIRED):
        lines.append("\n✅ Nothing appears to have been missed or interrupted.")

    for cat, title in SECTION_ORDER:
        items = by_cat.get(cat) or []
        if not items:
            continue
        lines.append(f"\n### {title} ({len(items)})")
        if cat == CAT_UNVERIFIABLE:
            names = sorted({f"{i['name']} ({i.get('platform')})" for i in items})
            lines.append(", ".join(names[:40]) + (" …" if len(names) > 40 else ""))
            continue
        for i in items[:40]:
            tid = f" [trigger `{i['trigger_id']}`]" if i.get("trigger_id") else (
                f" [trigger #{i['trigger_index']}]" if i.get("trigger_index") is not None else ""
            )
            note = f" — _{i['details']['note']}_" if (i.get("details") or {}).get("note") else ""
            lines.append(
                f"- **{i['name']}** (`{i['entity_id']}`){tid}: {i['summary']}"
                f"{CONF_BADGE.get(i.get('confidence'), '')}{note}"
            )
        if len(items) > 40:
            lines.append(f"- … and {len(items) - 40} more (see JSON report)")

    skipped = by_cat.get(CAT_SKIPPED) or []
    if skipped:
        lines.append(f"\n_{len(skipped)} automation(s) were off before the downtime and were skipped._")
    lines.append("\n_Conditions are not evaluated — a 'missed' trigger may not have passed its conditions._")
    if json_path:
        lines.append(f"_Full report: `{json_path}`_")
    return "\n".join(lines)


def to_push(report: dict[str, Any]) -> tuple[str, str]:
    """Short (title, message) for a phone notification."""
    w = report["window"]
    c = report["counts"]
    title = f"HA was down {w['duration']}" + ("" if w["clean_shutdown"] else " (unclean)")
    parts = []
    for cat, label in (
        (CAT_INTERRUPTED, "interrupted"),
        (CAT_MISSED, "missed"),
        (CAT_POSSIBLE, "possibly missed"),
        (CAT_STARTUP_FIRED, "fired at startup"),
    ):
        if c.get(cat):
            parts.append(f"{c[cat]} {label}")
    msg = ", ".join(parts) if parts else "Nothing missed or interrupted."
    top = [
        f"{f['name']}: {f['summary']}"
        for f in report["findings"]
        if f["category"] in (CAT_INTERRUPTED, CAT_MISSED)
    ][:3]
    if top:
        msg += "\n" + "\n".join(f"• {t[:140]}" for t in top)
    return title, msg


# Categories that make a downtime worth keeping a full report for.
KEEP_FULL_CATEGORIES = (CAT_INTERRUPTED, CAT_MISSED, CAT_POSSIBLE, CAT_STARTUP_FIRED)


def has_findings(report: dict[str, Any]) -> bool:
    """True when the report has anything beyond unverifiable/skipped noise."""
    counts = report.get("counts") or {}
    return any(counts.get(c) for c in KEEP_FULL_CATEGORIES)


def _report_time(path: Path) -> datetime:
    """When a report file was generated (from its name, falling back to mtime)."""
    m = REPORT_NAME_RE.match(path.name)
    if m:
        local = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
        return dt_util.as_utc(local.replace(tzinfo=dt_util.get_default_time_zone()))
    return datetime.fromtimestamp(path.stat().st_mtime, tz=dt_util.UTC)


def _prune_reports(reports: Path, report_days: int, now: datetime) -> None:
    """Delete report files older than report_days, then enforce the file cap."""
    cutoff = now - timedelta(days=report_days)
    files = sorted(reports.glob("report-*.json"), key=_report_time)
    keep: list[Path] = []
    for f in files:
        if _report_time(f) < cutoff:
            f.unlink(missing_ok=True)
        else:
            keep.append(f)
    for f in keep[:-MAX_REPORT_FILES]:
        f.unlink(missing_ok=True)


def _append_history(base: Path, summary: dict[str, Any], history_days: int, now: datetime) -> None:
    """Append a summary line and drop lines older than history_days."""
    path = base / HISTORY_FILE
    cutoff = now - timedelta(days=history_days)
    kept: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                when = dt_util.parse_datetime(json.loads(line).get("generated_at") or "")
            except ValueError:
                continue
            if when is not None and when >= cutoff:
                kept.append(line)
    kept.append(json.dumps(summary, default=str))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_files(base: Path, report: dict[str, Any], report_days: int, history_days: int) -> str | None:
    """Write the report and history. Returns the report file path, or None if
    the downtime had no findings (only a history line is kept for those)."""
    now = dt_util.utcnow()
    reports = base / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, default=str)
    (base / "last_report.json").write_text(payload, encoding="utf-8")

    path: Path | None = None
    if has_findings(report):
        stamp = dt_util.as_local(dt_util.parse_datetime(report["generated_at"])).strftime("%Y%m%d-%H%M%S")
        path = reports / f"report-{stamp}.json"
        n = 1
        while path.exists():
            path = reports / f"report-{stamp}-{n}.json"
            n += 1
        path.write_text(payload, encoding="utf-8")

    _append_history(
        base,
        {
            "generated_at": report["generated_at"],
            "window": report["window"],
            "counts": report["counts"],
            "actionable": report.get("actionable", 0),
            "file": path.name if path else None,
        },
        history_days,
        now,
    )
    _prune_reports(reports, report_days, now)
    return str(path) if path else None


async def async_write_json(
    hass: HomeAssistant, report: dict[str, Any], report_days: int, history_days: int
) -> str | None:
    """Write report + history in the executor."""
    base = Path(hass.config.path(REPORT_DIR))
    try:
        return await hass.async_add_executor_job(_write_files, base, report, report_days, history_days)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not write downtime report JSON")
        return None


async def async_deliver(
    hass: HomeAssistant,
    report: dict[str, Any],
    *,
    persistent: bool,
    notify_service: str,
    push_only_on_findings: bool,
    json_path: str | None,
    notification_id: str = PERSISTENT_NOTIFICATION_ID,
) -> None:
    """Send the report to the configured outputs."""
    if persistent:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            hass,
            to_markdown(report, json_path),
            title=(
                f"{NAME}: what-if {report['window']['duration']} window"
                if (report.get("meta") or {}).get("what_if")
                else f"{NAME}: {report['window']['duration']} downtime"
            ),
            notification_id=notification_id,
        )

    has_findings = report["actionable"] > 0 or report["counts"].get(CAT_STARTUP_FIRED, 0) > 0
    if notify_service and (has_findings or not push_only_on_findings):
        title, message = to_push(report)
        domain, _, service = notify_service.partition(".")
        if not service:
            _LOGGER.warning("notify_service '%s' should look like notify.mobile_app_x", notify_service)
            return
        data: dict[str, Any] = {"title": title, "message": message}
        try:
            await hass.services.async_call(domain, service, data, blocking=False)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to send downtime push via %s", notify_service)
