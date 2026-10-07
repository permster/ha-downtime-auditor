"""Severity ratings stored as Home Assistant labels (`downtime_auditor_sev: high`, ...)."""

from __future__ import annotations

import logging
import re

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, label_registry as lr

from .const import SEVERITY_COLORS, SEVERITY_LABEL_PREFIX, Severity
from .severity import highest

_LOGGER = logging.getLogger(__name__)

LABEL_ICON = "mdi:timeline-alert-outline"
LABEL_DESCRIPTION = "Downtime Auditor severity for this automation."
# Matched against HA's normalized label name (case-folded, spaces removed).
_LABEL_RE = re.compile(rf"^{SEVERITY_LABEL_PREFIX}:(critical|high|medium|low|none)$")


def label_name(severity: Severity) -> str:
    """Display name of the label for a severity."""
    return f"{SEVERITY_LABEL_PREFIX}: {severity.value}"


def _severity_of(name: str) -> Severity | None:
    m = _LABEL_RE.match(name.casefold().replace(" ", ""))
    return Severity(m.group(1)) if m else None


@callback
def severity_labels(hass: HomeAssistant) -> dict[str, Severity]:
    """label_id → severity, for every label that names a severity."""
    out: dict[str, Severity] = {}
    for label in lr.async_get(hass).async_list_labels():
        if (sev := _severity_of(label.name)) is not None:
            out[label.label_id] = sev
    return out


@callback
def async_create_labels(hass: HomeAssistant) -> list[str]:
    """Create whichever of the five severity labels are missing. Returns the names created."""
    reg = lr.async_get(hass)
    present = set(severity_labels(hass).values())
    created: list[str] = []
    for sev in Severity:
        if sev in present:
            continue
        name = label_name(sev)
        try:
            reg.async_create(name, color=SEVERITY_COLORS[sev], icon=LABEL_ICON, description=LABEL_DESCRIPTION)
        except ValueError:  # name taken by a label we don't recognize; leave it alone
            _LOGGER.warning("Could not create label %s: name already in use", name)
            continue
        created.append(name)
    return created


class SeverityLookup:
    """Reads label-based severity for many entities with one pass over the label registry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._labels = severity_labels(hass)
        self._entities = er.async_get(hass)

    def get(self, entity_id: str) -> Severity | None:
        """Highest severity labeled on the entity, or None if unlabeled."""
        entry = self._entities.async_get(entity_id)
        if entry is None or not self._labels:
            return None
        return highest(self._labels[label] for label in entry.labels if label in self._labels)


@callback
def async_set_severity(hass: HomeAssistant, entity_id: str, severity: Severity | None) -> None:
    """Replace the entity's severity label (None removes it, i.e. back to the default)."""
    ents = er.async_get(hass)
    entry = ents.async_get(entity_id)
    if entry is None:
        raise ValueError(f"{entity_id} is not in the entity registry")
    sev_labels = severity_labels(hass)
    labels = {label for label in entry.labels if label not in sev_labels}
    if severity is not None:
        label_id = next((lid for lid, sev in sev_labels.items() if sev == severity), None)
        if label_id is None:  # the user deleted it; choosing it again is explicit consent
            label_id = lr.async_get(hass).async_create(
                label_name(severity),
                color=SEVERITY_COLORS[severity],
                icon=LABEL_ICON,
                description=LABEL_DESCRIPTION,
            ).label_id
        labels.add(label_id)
    ents.async_update_entity(entity_id, labels=labels)
