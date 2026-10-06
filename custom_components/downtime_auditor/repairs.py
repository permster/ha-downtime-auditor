"""Surface findings in Settings → Repairs (one issue per finding)."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONFIDENCE_LABELS, DOMAIN, PANEL_URL, SEVERITY_LABELS, Severity
from .severity import at_least

MAX_ISSUES = 50


def _issue_id(report: dict, finding: dict) -> str:
    key = "|".join(
        str(finding.get(k)) for k in ("type", "entity_id", "trigger_index", "summary")
    )
    digest = hashlib.sha1(f"{report['generated_at']}|{key}".encode()).hexdigest()[:12]
    return f"{finding['type']}_{finding['entity_id']}_{digest}".replace(".", "_")


def _detail_text(finding: dict) -> str:
    d = finding.get("details") or {}
    lines: list[str] = []
    if finding.get("occurrences"):
        occ = finding["occurrences"]
        lines.append("Due at: " + ", ".join(occ[:10]) + (" …" if len(occ) > 10 else ""))
    if "before" in d or "after" in d:
        lines.append(f"Before downtime: `{d.get('before')}` · After: `{d.get('after')}`")
    if d.get("for") is not None:
        lines.append(f"`for:` {d['for']}")
    for run in d.get("runs") or []:
        if isinstance(run, dict) and run.get("last_step"):
            lines.append(f"Stopped at `{run['last_step']}` (run {str(run.get('run_id'))[:8]})")
    cond = d.get("conditions") or {}
    if cond.get("likely") == "fail":
        lines.append(f"Conditions: probably failed — {cond.get('why') or 'see the dashboard'}")
    elif cond.get("result"):
        basis = {"baseline": "pre-downtime values", "history": "recorder history"}.get(cond.get("basis"), "")
        lines.append(
            f"Conditions: {cond['result']}" + (f" (from {basis})" if basis else "")
            + (f" — {cond['why']}" if cond.get("why") else "")
        )
    if d.get("note"):
        lines.append(d["note"])
    return "\n".join(f"- {line}" for line in lines) if lines else "- (no extra detail)"


def async_sync_issues(
    hass: HomeAssistant,
    report: dict[str, Any] | None,
    previous_ids: list[str],
    min_severity: str,
) -> list[str]:
    """Delete the previous report's issues and raise one per finding at or above min_severity."""
    for issue_id in previous_ids:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    if not report:
        return []

    window = report["window"]
    new_ids: list[str] = []
    for finding in report["findings"]:  # most severe first
        if len(new_ids) >= MAX_ISSUES:
            break
        if finding.get("severity") == Severity.NONE or not at_least(finding.get("severity"), min_severity):
            continue
        issue_id = _issue_id(report, finding)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=True,
            severity=(
                ir.IssueSeverity.ERROR
                if at_least(finding["severity"], Severity.HIGH)
                else ir.IssueSeverity.WARNING
            ),
            translation_key=finding["type"],
            translation_placeholders={
                "name": str(finding.get("name")),
                "entity_id": str(finding.get("entity_id")),
                "summary": str(finding.get("summary")),
                "severity": SEVERITY_LABELS.get(finding.get("severity"), str(finding.get("severity"))),
                "severity_reason": str(finding.get("severity_reason") or ""),
                "confidence": CONFIDENCE_LABELS.get(finding.get("confidence"), str(finding.get("confidence"))),
                "window": f"{window['start_local']} → {window['end_local']} ({window['duration']})",
                "details": _detail_text(finding),
                "panel": f"/{PANEL_URL}",
            },
            data={"report": report["generated_at"], "entity_id": finding.get("entity_id")},
        )
        new_ids.append(issue_id)
    return new_ids
