"""Severity rules (pure: no Home Assistant imports, unit-tested directly)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .const import (
    SEVERITY_LABELS,
    SEVERITY_RANK,
    SEVERITY_SOURCE_LABEL,
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


def _cap_low(base: Severity) -> Severity:
    return base if rank(base) <= rank(Severity.LOW) else Severity.LOW


def effective_severity(
    base: Severity,
    source: str,
    finding_type: str,
    confidence: str,
    conditions: str | None,
) -> tuple[Severity, str]:
    """Return (severity, reason) for one finding.

    Critical is never capped; only failed conditions bring it down.
    """
    why_base = "set by label" if source == SEVERITY_SOURCE_LABEL else "default for unrated automations"
    if conditions == "fail":
        return Severity.NONE, "conditions would not have passed"
    if base == Severity.CRITICAL:
        return base, why_base
    if finding_type == FindingType.FIRED_AT_STARTUP and rank(base) > rank(Severity.LOW):
        return Severity.LOW, f"fired at startup, so capped at Low ({SEVERITY_LABELS[base]} {why_base})"
    if confidence == Confidence.UNKNOWN and rank(base) > rank(Severity.LOW):
        return Severity.LOW, f"can't be confirmed, so capped at Low ({SEVERITY_LABELS[base]} {why_base})"
    return base, why_base
