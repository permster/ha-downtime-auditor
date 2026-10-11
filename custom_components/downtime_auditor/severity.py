"""Severity rules (pure: no Home Assistant imports, unit-tested directly)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .const import (
    DEFAULT_SEVERITY,
    SEVERITY_LABELS,
    SEVERITY_RANK,
    SEVERITY_SOURCE_LABEL,
    SEVERITY_SOURCE_TRIGGER,
    Confidence,
    FindingType,
    Severity,
)


def rank(value: Any) -> int:
    """Order of a severity (unknown values sort lowest)."""
    try:
        return SEVERITY_RANK[Severity(value)]
    except ValueError:
        return -1


def at_least(value: Any, minimum: Any) -> bool:
    """True when `value` is `minimum` or more severe."""
    return rank(value) >= rank(minimum)


def highest(values: Iterable[Any]) -> Severity | None:
    """Most severe of `values`, or None if empty."""
    best: Severity | None = None
    for value in values:
        if best is None or rank(value) > rank(best):
            best = Severity(value)
    return best


def _minutes(value: float) -> str:
    whole = max(0, round(value))
    return "under a minute" if whole == 0 else f"{whole} min"


def _cap_low(base: Severity) -> Severity:
    return base if rank(base) <= rank(Severity.LOW) else Severity.LOW


def effective_severity(
    base: Severity,
    source: str,
    finding_type: str,
    confidence: str,
    conditions: str | None,
    unconfirmable: str = Severity.LOW,
    caught_up_in: float | None = None,
    catch_up_limit: int = 0,
) -> tuple[Severity, str]:
    """Return (severity, reason) for one finding.

    Precedence: failed conditions (None) > a rating set for this trigger (as is) >
    the "can't be confirmed" option set to None > a time pattern that runs again soon (None) >
    Critical (never capped) > caps.
    `unconfirmable` is that option: Low (cap Unknown-confidence triggers at Low) or None.
    `caught_up_in`: minutes from startup to a time pattern's next tick; None within
    `catch_up_limit` minutes (0 = off).
    """
    if conditions == "fail":
        return Severity.NONE, "conditions would not have passed"
    if source == SEVERITY_SOURCE_TRIGGER:
        return base, "set for this trigger"
    why_base = "set by label" if source == SEVERITY_SOURCE_LABEL else "default for unrated automations"
    if confidence == Confidence.UNKNOWN and unconfirmable == Severity.NONE:
        return Severity.NONE, "can't be confirmed, and the integration's options set those to None"
    if caught_up_in is not None and catch_up_limit > 0 and caught_up_in <= catch_up_limit:
        return Severity.NONE, f"the pattern runs again {_minutes(caught_up_in)} after Home Assistant started"
    if base == Severity.CRITICAL:
        return base, why_base
    if finding_type == FindingType.FIRED_AT_STARTUP and rank(base) > rank(Severity.LOW):
        return Severity.LOW, f"fired at startup, so capped at Low ({SEVERITY_LABELS[base]} {why_base})"
    if confidence == Confidence.UNKNOWN and rank(base) > rank(Severity.LOW):
        return Severity.LOW, f"can't be confirmed, so capped at Low ({SEVERITY_LABELS[base]} {why_base})"
    return base, why_base


def severity_rules(unconfirmable: str = Severity.LOW, catch_up_limit: int = 0) -> dict[str, list[dict]]:
    """The rules effective_severity applies, in order, as the dashboard's Rules tab shows them.

    Keep in step with effective_severity (tests/test_rules.py checks each rule against it).
    """
    none = unconfirmable == Severity.NONE
    unconfirmable_rule = {
        "key": "unconfirmable",
        "when": "The trigger can't be confirmed (event, webhook, MQTT, tag, voice, device presses)",
        "then": "None" if none else "capped at Low",
        "option": "Severity for triggers that can't be confirmed",
        "value": SEVERITY_LABELS[Severity(unconfirmable)],
        "on": True,
    }
    adjust = [
        {"key": "conditions", "when": "Its conditions, or the gates in its actions, would have failed",
         "then": "None", "on": True},
        {"key": "trigger", "when": "You rated this trigger on the dashboard", "then": "kept as rated", "on": True},
    ]
    if none:
        adjust.append(unconfirmable_rule)
    adjust += [
        {"key": "catch_up",
         "when": f"A time pattern runs again within {catch_up_limit} min of startup" if catch_up_limit > 0
         else "A time pattern runs again soon after startup",
         "then": "None", "option": "Time patterns that run again soon",
         "value": f"{catch_up_limit} min" if catch_up_limit > 0 else "off", "on": catch_up_limit > 0},
        {"key": "critical", "when": "The rating is Critical", "then": "kept: the rules below don't apply", "on": True},
        {"key": "startup", "when": "It fired at startup", "then": "capped at Low", "on": True},
    ]
    if not none:
        adjust.append(unconfirmable_rule)
    start = [
        {"key": "trigger", "text": "A rating you set for this trigger on the dashboard"},
        {"key": "label", "text": "otherwise the automation's downtime_auditor_sev label"},
        {"key": "default", "text": f"otherwise {SEVERITY_LABELS[DEFAULT_SEVERITY]} (unrated)"},
    ]
    return {"start": start, "adjust": adjust}
