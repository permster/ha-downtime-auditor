"""States that came back wrong after an unclean stop.

Entities that restore their state (helpers, trigger-based template sensors, utility
meters, optimistic entities, ...) get it back from Home Assistant's saved copy, which is
written every 15 minutes and at a clean stop. After a crash or power loss, a change made
since the last save is lost. We save their values within seconds of a change, so after an
unclean stop the two can be compared.

Device-backed entities aren't checked: they read the real device after startup, so a
different value there is a real change, not a lost one.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import restore_state

from .const import UNKNOWN_STATES
from .snapshot import automation_entities

SCOPE_USED = "used"  # restored entities an automation or script uses (default)
SCOPE_ALL = "all"
SCOPE_OFF = "off"
SCOPES = (SCOPE_USED, SCOPE_ALL, SCOPE_OFF)

# Automations and scripts restore too, but their runs are covered by the other finding types.
_SKIP_DOMAINS = ("automation", "script")

# domain → (action, data field for the value, {value: action}) to set a value back
_SETTERS: dict[str, tuple[str | None, str | None, dict[str, str]]] = {
    "input_boolean": (None, None, {"on": "turn_on", "off": "turn_off"}),
    "switch": (None, None, {"on": "turn_on", "off": "turn_off"}),
    "light": (None, None, {"on": "turn_on", "off": "turn_off"}),
    "fan": (None, None, {"on": "turn_on", "off": "turn_off"}),
    "input_number": ("set_value", "value", {}),
    "number": ("set_value", "value", {}),
    "counter": ("set_value", "value", {}),
    "input_text": ("set_value", "value", {}),
    "text": ("set_value", "value", {}),
    "input_select": ("select_option", "option", {}),
    "select": ("select_option", "option", {}),
    "input_datetime": ("set_datetime", None, {}),
}


def restoring_entities(hass: HomeAssistant) -> set[str]:
    """Entity ids that get their state from Home Assistant's saved copy at startup."""
    try:
        ids = set(restore_state.async_get(hass).entities)
    except Exception:  # noqa: BLE001  (internal structure; degrade to nothing)
        return set()
    return {e for e in ids if e.split(".", 1)[0] not in _SKIP_DOMAINS}


def users(hass: HomeAssistant) -> dict[str, list[str]]:
    """entity id → automations and scripts that use it (triggers, conditions, actions)."""
    out: dict[str, list[str]] = {}
    script_component = hass.data.get("script")
    scripts = list(getattr(script_component, "entities", []) or [])
    for ent in [*automation_entities(hass), *scripts]:
        try:
            refs = ent.referenced_entities
        except Exception:  # noqa: BLE001
            continue
        for ref in refs or ():
            out.setdefault(ref, []).append(ent.entity_id)
    return out


def tracked(hass: HomeAssistant, scope: str) -> set[str]:
    """The restored entities to watch for this scope."""
    if scope == SCOPE_OFF:
        return set()
    ids = restoring_entities(hass)
    if scope == SCOPE_USED:
        ids &= set(users(hass))
    return ids


def capture(hass: HomeAssistant, ids: set[str] | list[str]) -> dict[str, dict]:
    """Current value of each entity (unknown/unavailable left out)."""
    out = {}
    for eid in ids:
        st = hass.states.get(eid)
        if st is None or st.state in UNKNOWN_STATES:
            continue
        out[eid] = {"state": st.state, "changed": st.last_changed.isoformat()}
    return out


def saved_copies(hass: HomeAssistant, ids: set[str] | list[str]) -> dict[str, dict]:
    """What Home Assistant restored from: its saved state and when it was saved."""
    try:
        last = restore_state.async_get(hass).last_states
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for eid in ids:
        if (stored := last.get(eid)) is not None:
            out[eid] = {"state": stored.state.state, "saved_at": stored.last_seen.isoformat()}
    return out


def compare(before: dict[str, dict], restored: dict[str, dict], saved: dict[str, dict]) -> list[dict]:
    """Entities whose value at startup isn't the one they had before the stop.

    `before`: our last values; `restored`: values at startup; `saved`: Home Assistant's
    saved copies. An entity that didn't come back with its saved copy doesn't use it
    (an `initial:` value, or it resets by design), so it isn't a lost change.
    """
    out = []
    for eid, was in before.items():
        now = restored.get(eid)
        if now is None or now["state"] == was["state"]:
            continue
        copy = saved.get(eid)
        if copy is not None and copy["state"] != now["state"]:
            continue
        out.append({
            "entity_id": eid,
            "before": was["state"],
            "after": now["state"],
            "changed": was.get("changed"),
            "saved_at": copy["saved_at"] if copy else None,
        })
    return out


def set_back(entity_id: str, value: str) -> dict | None:
    """The action that sets the entity back to `value`: {"action", "data"}; None if there's none."""
    domain = entity_id.split(".", 1)[0]
    if domain not in _SETTERS:
        return None
    action, field, by_value = _SETTERS[domain]
    if by_value:
        if value not in by_value:
            return None
        return {"action": f"{domain}.{by_value[value]}", "data": {"entity_id": entity_id}}
    if domain == "input_datetime":
        return {"action": "input_datetime.set_datetime", "data": {"entity_id": entity_id, _datetime_field(value): value}}
    return {"action": f"{domain}.{action}", "data": {"entity_id": entity_id, field: value}}


def _datetime_field(value: str) -> str:
    if " " in value or "T" in value:
        return "datetime"
    return "time" if ":" in value else "date"
