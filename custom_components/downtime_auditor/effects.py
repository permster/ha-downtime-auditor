"""What an automation's actions would have changed: target entities and the state to expect.

Used to show, next to a finding, the entities that the actions that didn't run would
have touched, so the dashboard can compare them with their live state.
"""

from __future__ import annotations

import re
from typing import Any

MAX_EFFECTS = 10

# action suffix → the state to expect afterwards (None: can't say, e.g. toggle or a script)
_EXPECT = {
    "turn_on": "on", "turn_off": "off",
    "open_cover": "open", "close_cover": "closed", "open_valve": "open", "close_valve": "closed",
    "lock": "locked", "unlock": "unlocked", "open": "open",
    "alarm_arm_away": "armed_away", "alarm_arm_home": "armed_home", "alarm_arm_night": "armed_night",
    "alarm_arm_vacation": "armed_vacation", "alarm_disarm": "disarmed",
    "start": "cleaning", "return_to_base": "returning",
    "media_play": "playing", "media_pause": "paused", "media_stop": "idle",
}
# action → the data field whose value is the new state
_EXPECT_FROM_DATA = {"select_option": "option", "set_value": "value", "set_hvac_mode": "hvac_mode",
                     "set_preset_mode": None, "set_options": None}
_NO_STATE_DOMAINS = {"notify", "persistent_notification", "script", "scene", "tts", "logbook", "system_log",
                     "homeassistant"}
_TEMPLATE = re.compile(r"{[{%]")


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _enabled(step: Any) -> bool:
    return isinstance(step, dict) and step.get("enabled", True) is not False


def _plain(value: Any) -> str | None:
    """A literal value as a string; None for templates."""
    if value is None or hasattr(value, "template") or (isinstance(value, str) and _TEMPLATE.search(value)):
        return None
    return str(value)


def step_effects(step: dict) -> list[dict]:
    """Entities one action step targets, with the expected state where it's clear."""
    action = step.get("action") or step.get("service")
    if not isinstance(action, str) or "." not in action:
        return []
    domain, name = action.split(".", 1)
    data = step.get("data") or step.get("data_template") or {}
    target = {**(data if isinstance(data, dict) else {}), **(step.get("target") or {})}
    entities = [e for e in _as_list(target.get("entity_id")) if isinstance(e, str) and "." in e and not _TEMPLATE.search(e)]
    expected: str | None = None
    if name in _EXPECT:
        expected = _EXPECT[name]
    elif name in _EXPECT_FROM_DATA and _EXPECT_FROM_DATA[name] and isinstance(data, dict):
        expected = _plain(data.get(_EXPECT_FROM_DATA[name]))
    if domain == "cover" and name in ("open_cover", "close_cover"):
        expected = "open" if name == "open_cover" else "closed"
    out = []
    for eid in entities:
        # homeassistant.turn_on/off mean on/off; a script or scene being "on" says nothing
        exp = expected if domain not in _NO_STATE_DOMAINS or domain == "homeassistant" else None
        out.append({"entity_id": eid, "action": action, "expected": exp})
    return out


def extract_effects(seq: Any) -> list[dict]:
    """Effects of a sequence: its action steps, and those in plain groups (sequence/parallel).

    Branches inside it (choose/if) are left out: which one would run isn't known here.
    """
    out: list[dict] = []
    for step in _as_list(seq):
        if not _enabled(step):
            continue
        if "sequence" in step and not any(k in step for k in ("choose", "repeat")):
            out += extract_effects(step["sequence"])
        elif "parallel" in step:
            for branch in _as_list(step["parallel"]):
                out += extract_effects(branch.get("sequence") if isinstance(branch, dict) and "sequence" in branch else [branch])
        else:
            out += step_effects(step)
    seen, unique = set(), []
    for e in out:
        if e["entity_id"] not in seen:
            seen.add(e["entity_id"])
            unique.append(e)
    return unique[:MAX_EFFECTS]


_SEQ_KEYS = ("action", "sequence", "then", "else", "default")


def remaining_effects(seq: Any, path: str | None) -> list[dict]:
    """Effects of what didn't run after a run stopped at `path` (a trace step path).

    "action/0/choose/5/sequence/0": that step and the rest of its sequence, then the
    rest of every enclosing sequence.
    """
    if not path:
        return []
    parts = path.split("/")
    levels: list[tuple[list, int]] = []  # (sequence, index of the step in it), outermost first
    node: Any = {"action": _as_list(seq), "sequence": _as_list(seq)}
    i = 0
    try:
        while i + 1 < len(parts):
            key, idx = parts[i], int(parts[i + 1])
            if key in _SEQ_KEYS:
                current = _as_list(node.get(key) if isinstance(node, dict) else None)
                levels.append((current, idx))
                node = current[idx]
            elif key in ("choose", "parallel"):
                node = _as_list(node[key])[idx]
            elif key == "repeat":
                node = node["repeat"]
                i += 1
                continue
            else:
                break
            i += 2
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    if not levels:
        return []
    out: list[dict] = []
    inner_seq, inner_idx = levels[-1]
    out += extract_effects(inner_seq[inner_idx:])
    for outer_seq, outer_idx in reversed(levels[:-1]):
        out += extract_effects(outer_seq[outer_idx + 1:])
    seen, unique = set(), []
    for e in out:
        if e["entity_id"] not in seen:
            seen.add(e["entity_id"])
            unique.append(e)
    return unique[:MAX_EFFECTS]


def uses_trigger_ids(seq: Any) -> bool:
    """Do the actions branch on `trigger` conditions? Then running them by hand won't pick the same branch."""
    text = repr(seq)
    return "'condition': 'trigger'" in text or "trigger." in text
