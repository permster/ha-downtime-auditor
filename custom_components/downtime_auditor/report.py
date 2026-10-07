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
    CONFIDENCE_LABELS,
    CONFIDENCE_LEVEL,
    HISTORY_FILE,
    MAX_REPORT_FILES,
    NAME,
    PERSISTENT_NOTIFICATION_ID,
    REPORT_DIR,
    SEVERITY_LABELS,
    SEVERITY_SOURCE_DEFAULT,
    TYPE_LABELS,
    Confidence,
    FindingType,
    Severity,
)
from .schedule import fmt_duration
from .severity import at_least, effective_severity, highest, rank

_LOGGER = logging.getLogger(__name__)
REPORT_NAME_RE = re.compile(r"^report-(\d{8}-\d{6})(?:-\d+)?\.json$")

REPORT_SCHEMA = 3  # 3: findings grouped per automation (v0.5.1); 2: one per trigger (v0.5.0)

SECTION_ORDER = [
    (FindingType.INTERRUPTED, "Interrupted mid-run"),
    (FindingType.MISSED, "Missed triggers"),
    (FindingType.FIRED_AT_STARTUP, "Fired at startup (check these weren't spurious)"),
]


def _local(value: Any) -> str:
    if value is None:
        return "?"
    dt = value if hasattr(value, "tzinfo") else dt_util.parse_datetime(str(value))
    if dt is None:
        return str(value)
    return dt_util.as_local(dt).strftime("%Y-%m-%d %H:%M:%S")


def _summarize(findings: list[dict[str, Any]], attention_min: str | None) -> dict[str, Any]:
    """Counts and severity roll-ups shared by new and upgraded reports."""
    counts = {t.value: 0 for t in FindingType}
    by_severity = {s.value: 0 for s in Severity}
    for f in findings:
        counts[f["type"]] = counts.get(f["type"], 0) + 1
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1
    top = highest(f["severity"] for f in findings)
    attention = (
        None
        if attention_min is None
        else sum(1 for f in findings if at_least(f["severity"], attention_min))
    )
    return {
        "counts": counts,
        "counts_by_severity": by_severity,
        "highest_severity": top.value if top else None,
        "needs_attention": attention,
    }


def _confidence_level(value: Any) -> int:
    return CONFIDENCE_LEVEL.get(value, 0)


def _merge(children: list[dict[str, Any]]) -> dict[str, Any]:
    """One entry for an automation/script: its triggers (or runs) are listed under `triggers`.

    Group-level severity and reason come from the most severe trigger; confidence is
    the most certain trigger's (how sure we are that *something* was missed).
    """
    children = sorted(
        ({k: v for k, v in c.items() if k != "triggers"} for c in children),
        key=lambda c: (-rank(c["severity"]), -_confidence_level(c.get("confidence"))),
    )
    top, n = children[0], len(children)
    platforms = sorted({str(c["platform"]) for c in children if c.get("platform")})
    group = {
        "type": top["type"],
        "entity_id": top["entity_id"],
        "name": top["name"],
        "item_id": top.get("item_id"),
        "platform": ", ".join(platforms) or None,
        "trigger_index": top.get("trigger_index") if n == 1 else None,
        "trigger_id": top.get("trigger_id") if n == 1 else None,
        "confidence": max((c.get("confidence") for c in children), key=_confidence_level),
        "severity": top["severity"],
        "severity_source": top.get("severity_source"),
        "severity_base": top.get("severity_base"),
        "severity_reason": top.get("severity_reason"),
        "conditions": top.get("conditions"),
        "summary": top["summary"],
        "count": top.get("count"),
        "occurrences": top.get("occurrences") or [],
        "occurrences_iso": top.get("occurrences_iso") or [],
        "details": top.get("details") or {},
        "triggers": children,
    }
    if n > 1:
        noun = "triggers" if top["type"] == FindingType.MISSED else "items"
        group.update(
            summary=f"{n} {noun}: " + "; ".join(c["summary"] for c in children[:2]) + (" …" if n > 2 else ""),
            count=sum(int(c.get("count") or 1) for c in children),
            occurrences=sorted({o for c in children for o in c.get("occurrences") or []})[:25],
            occurrences_iso=sorted({o for c in children for o in c.get("occurrences_iso") or []})[:200],
            details={},
        )
    return group


def group_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One finding per (type, automation/script), most severe first.

    Accepts flat per-trigger findings or already-grouped ones (regrouping is a no-op
    apart from refreshing the group-level fields).
    """
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in findings:
        for child in f.get("triggers") or [f]:
            by_key.setdefault((f["type"], f["entity_id"]), []).append(child)
    return sorted((_merge(c) for c in by_key.values()), key=lambda g: -rank(g["severity"]))


def build_report(
    window: Window,
    findings: list[Finding],
    meta: dict[str, Any],
    attention_min: str = Severity.HIGH,
) -> dict[str, Any]:
    """Assemble the structured report.

    `attention_min` is the Repairs threshold; `needs_attention` counts findings at
    or above it. `actionable` is a deprecated alias of it (removed in v0.6.0).
    """
    items = group_findings([f.as_dict() for f in findings])
    summary = _summarize(items, attention_min)
    return {
        "schema": REPORT_SCHEMA,
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
        **summary,
        "actionable": summary["needs_attention"],
        "skipped": meta.get("automations_skipped", 0),
        "meta": meta,
        "findings": items,
    }


def refresh_summary(report: dict[str, Any], attention_min: str) -> None:
    """Recompute groups, ordering and roll-ups after findings' severities changed."""
    report["findings"] = group_findings(report["findings"])
    summary = _summarize(report["findings"], attention_min)
    report.update(summary, actionable=summary["needs_attention"])


def _badge(finding: dict[str, Any]) -> str:
    """'High · Probable' — severity first, confidence second."""
    sev = SEVERITY_LABELS.get(finding.get("severity"), str(finding.get("severity")))
    conf = CONFIDENCE_LABELS.get(finding.get("confidence"), str(finding.get("confidence")))
    return f"{sev} · {conf}"


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

    shown = [f for f in report["findings"] if f.get("severity") != Severity.NONE]
    hidden = len(report["findings"]) - len(shown)
    by_type: dict[str, list[dict]] = {}
    for f in shown:
        by_type.setdefault(f["type"], []).append(f)

    if not any(worth_keeping(f) for f in shown):
        lines.append("\n✅ Nothing appears to have been missed or interrupted.")

    for ftype, title in SECTION_ORDER:
        items = by_type.get(ftype) or []
        if not items:
            continue
        lines.append(f"\n### {title} ({len(items)})")
        # Unknown-confidence triggers at Low are listed by name only, to keep this short.
        brief = [i for i in items if i.get("confidence") == Confidence.UNKNOWN and not worth_keeping(i)]
        full = [i for i in items if i not in brief]
        for i in full[:40]:
            tid = f" [trigger `{i['trigger_id']}`]" if i.get("trigger_id") else (
                f" [trigger #{i['trigger_index']}]" if i.get("trigger_index") is not None else ""
            )
            note = f" — _{i['details']['note']}_" if (i.get("details") or {}).get("note") else ""
            triggers = i.get("triggers") or []
            if len(triggers) > 1:
                lines.append(f"- **{_badge(i)}** — **{i['name']}** (`{i['entity_id']}`): {len(triggers)} triggers")
                for c in triggers[:5]:
                    conf = CONFIDENCE_LABELS.get(c.get("confidence"), c.get("confidence"))
                    lines.append(f"  - {c['summary']} _({conf})_")
                if len(triggers) > 5:
                    lines.append(f"  - … and {len(triggers) - 5} more")
                continue
            lines.append(f"- **{_badge(i)}** — **{i['name']}** (`{i['entity_id']}`){tid}: {i['summary']}{note}")
        if len(full) > 40:
            lines.append(f"- … and {len(full) - 40} more (see JSON report)")
        if brief:
            names = sorted({f"{i['name']} ({i.get('platform')})" for i in brief})
            lines.append(
                "- _Can't be confirmed (event-style triggers):_ "
                + ", ".join(names[:40])
                + (" …" if len(names) > 40 else "")
            )

    if hidden:
        lines.append(
            f"\n_{hidden} finding(s) with severity None are not shown "
            "(conditions would have failed, or rated None)._"
        )
    skipped = report.get("skipped") or meta.get("automations_skipped") or 0
    if skipped:
        lines.append(f"\n_{skipped} automation(s) were turned off and were skipped._")
    if pending := report.get("pending_checks"):
        ents = sorted({p["entity"] for p in pending})
        lines.append(
            f"\n_Waiting for {len(ents)} entit{'y' if len(ents) == 1 else 'ies'} to report after the restart "
            f"({', '.join(ents[:10])}{' …' if len(ents) > 10 else ''}); their triggers are checked then, "
            "and the report is updated if anything was missed._"
        )
    if errors := meta.get("analysis_errors"):
        names = sorted({e.get("name") or e["automation"] for e in errors})
        lines.append(
            f"\n_Couldn't analyze {len(errors)} trigger(s) ({', '.join(names[:10])}{' …' if len(names) > 10 else ''}). "
            "This is a Downtime Auditor problem, not a missed trigger: please report it, with the warning "
            "from the Home Assistant log._"
        )
    if unchecked := report.get("unchecked"):
        ents = sorted({u["entity"] for u in unchecked})
        lines.append(
            f"\n_Couldn't check {len(unchecked)} trigger(s): {', '.join(ents[:10])}{' …' if len(ents) > 10 else ''} "
            "never reported a value after the restart._"
        )
    lines.append(
        "\n_Conditions are checked where possible: against pre-downtime values after a real outage, "
        "against recorder history for what-if windows._"
    )
    if json_path:
        lines.append(f"_Full report: `{json_path}`_")
    return "\n".join(lines)


def to_push(report: dict[str, Any]) -> tuple[str, str]:
    """Short (title, message) for a phone notification: severity first."""
    w = report["window"]
    title = f"HA was down {w['duration']}" + ("" if w["clean_shutdown"] else " (unclean)")
    by_sev = report.get("counts_by_severity") or {}
    parts = [
        f"{by_sev[s]} {SEVERITY_LABELS[s].lower()}"
        for s in (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW)
        if by_sev.get(s)
    ]
    msg = ", ".join(parts) if parts else "Nothing missed or interrupted."
    top = [
        f"[{SEVERITY_LABELS[f['severity']]}] {f['name']}: {f['summary']}"
        for f in report["findings"]
        if at_least(f["severity"], Severity.LOW)
    ][:3]  # findings are already sorted most severe first
    if top:
        msg += "\n" + "\n".join(f"• {t[:140]}" for t in top)
    return title, msg


def worth_keeping(finding: dict[str, Any]) -> bool:
    """A finding worth a saved report file (not None, and not just unconfirmable noise)."""
    sev = finding.get("severity")
    if sev == Severity.NONE:
        return False
    return finding.get("confidence") != Confidence.UNKNOWN or at_least(sev, Severity.MEDIUM)


def has_findings(report: dict[str, Any]) -> bool:
    """True when the report has anything beyond unconfirmable/None noise."""
    return any(worth_keeping(f) for f in report.get("findings") or [])


# ---------------------------------------------------------------- v0.4 compatibility

LEGACY_TYPES = {
    "interrupted": FindingType.INTERRUPTED,
    "missed": FindingType.MISSED,
    "possibly_missed": FindingType.MISSED,
    "unverifiable": FindingType.MISSED,
    "fired_during_startup": FindingType.FIRED_AT_STARTUP,
}
LEGACY_REASON = "Recorded before severity ratings existed"


def _legacy_confidence(finding: dict[str, Any], clean: bool) -> Confidence:
    category = finding.get("category")
    if category == "unverifiable":
        return Confidence.UNKNOWN
    if category == "fired_during_startup":
        return Confidence.CONFIRMED  # it did fire; whether it was spurious is the question
    old = finding.get("confidence")
    if old == "high":
        return Confidence.CONFIRMED if clean else Confidence.PROBABLE
    if old == "medium":
        return Confidence.PROBABLE
    return Confidence.POSSIBLE


def upgrade_legacy(obj: Any) -> Any:
    """Read an older report or history line in current terms. Idempotent; never touches files.

    schema 1 (v0.4): old categories → types/confidence/severity, then grouped.
    schema 2 (v0.5.0): one finding per trigger → grouped per automation.
    """
    if not isinstance(obj, dict):
        return obj
    schema = int(obj.get("schema") or 1)
    if schema >= REPORT_SCHEMA:
        return obj
    if schema == 2:
        out = dict(obj)
        if isinstance(obj.get("findings"), list):
            out["findings"] = group_findings(obj["findings"])
            summary = _summarize(out["findings"], None)
            del summary["needs_attention"]  # keep the recorded value; the threshold isn't known here
            out.update(summary)
        out["schema"] = REPORT_SCHEMA
        return out
    out = dict(obj)
    old_counts = obj.get("counts") or {}
    counts = {t.value: 0 for t in FindingType}
    for key, value in old_counts.items():
        if key in LEGACY_TYPES:
            counts[LEGACY_TYPES[key]] += int(value or 0)
    skipped = int(old_counts.get("skipped") or 0)

    if isinstance(obj.get("findings"), list):  # a full report file
        clean = bool((obj.get("window") or {}).get("clean_shutdown", True))
        findings = []
        for f in obj["findings"]:
            category = f.get("category")
            if category not in LEGACY_TYPES:
                continue  # 'skipped' (counted above) or anything unrecognized
            new = {k: v for k, v in f.items() if k != "category"}
            new["type"] = LEGACY_TYPES[category].value
            new["confidence"] = _legacy_confidence(f, clean).value
            sev, _ = effective_severity(
                Severity.MEDIUM, SEVERITY_SOURCE_DEFAULT, new["type"], new["confidence"], None
            )
            new.update(
                severity=sev.value,
                severity_source=SEVERITY_SOURCE_DEFAULT,
                severity_base=Severity.MEDIUM.value,
                severity_reason=LEGACY_REASON,
                conditions=None,
            )
            findings.append(new)
        findings = group_findings(findings)
        out["findings"] = findings
        out.update(_summarize(findings, None))
        out["counts"] = counts  # keep the recorded totals
    else:  # a history line: no findings, so no severity
        out.update(counts=counts, counts_by_severity=None, highest_severity=None, needs_attention=None)

    out["skipped"] = skipped
    out["actionable"] = obj.get("actionable")
    out["schema"] = REPORT_SCHEMA
    out["legacy"] = True
    return out


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
        path = _new_report_path(reports, report)
        path.write_text(payload, encoding="utf-8")

    _append_history(base, _history_line(report, path), history_days, now)
    _prune_reports(reports, report_days, now)
    return str(path) if path else None


def _new_report_path(reports: Path, report: dict[str, Any]) -> Path:
    stamp = dt_util.as_local(dt_util.parse_datetime(report["generated_at"])).strftime("%Y%m%d-%H%M%S")
    path = reports / f"report-{stamp}.json"
    n = 1
    while path.exists():
        path = reports / f"report-{stamp}-{n}.json"
        n += 1
    return path


def _history_line(report: dict[str, Any], path: Path | None) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": report["generated_at"],
        "window": report["window"],
        "counts": report["counts"],
        "counts_by_severity": report.get("counts_by_severity"),
        "highest_severity": report.get("highest_severity"),
        "needs_attention": report.get("needs_attention"),
        "actionable": report.get("needs_attention"),  # deprecated; removed in v0.6.0
        "skipped": report.get("skipped", 0),
        "file": path.name if path else None,
    }


def _update_files(base: Path, report: dict[str, Any]) -> str | None:
    """Rewrite an already-saved report after it changed (late re-checks).

    Overwrites last_report.json and the report file (creating it if the report
    now has findings worth keeping) and replaces this report's history line.
    """
    reports = base / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    current = (report.get("meta") or {}).get("json_path")
    path = Path(current) if current else None
    if path is None and has_findings(report):
        path = _new_report_path(reports, report)
    if path is not None:
        report.setdefault("meta", {})["json_path"] = str(path)
    payload = json.dumps(report, indent=2, default=str)
    (base / "last_report.json").write_text(payload, encoding="utf-8")
    if path is not None:
        path.write_text(payload, encoding="utf-8")

    hist = base / HISTORY_FILE
    if hist.exists():
        lines = hist.read_text(encoding="utf-8").splitlines()
        for i in range(len(lines) - 1, -1, -1):
            try:
                if json.loads(lines[i]).get("generated_at") == report["generated_at"]:
                    lines[i] = json.dumps(_history_line(report, path), default=str)
                    break
            except ValueError:
                continue
        tmp = hist.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(hist)
    return str(path) if path else None


async def async_update_json(hass: HomeAssistant, report: dict[str, Any]) -> str | None:
    """Rewrite the saved report and its history line in the executor."""
    base = Path(hass.config.path(REPORT_DIR))
    try:
        return await hass.async_add_executor_job(_update_files, base, report)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not update the downtime report JSON")
        return None


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
    push_min_severity: str = Severity.HIGH,
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

    worth_push = any(at_least(f["severity"], push_min_severity) for f in report["findings"])
    if notify_service and (worth_push or not push_only_on_findings):
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
