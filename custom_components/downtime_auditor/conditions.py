"""Would an automation's conditions have passed at a past moment?

Three-valued: pass / fail / unknown. HA's own condition helpers read the
current clock and current states, so they can't be pointed at the past; this
module re-implements the common condition types against a *state source*:

* BaselineSource — states captured just before the downtime (real outages).
  HA wasn't running, so there is no history; the last known value is the best
  estimate, and it is treated as such.
* HistorySource — recorder history (what-if windows in the past).

Anything that can't be reconstructed is `unknown`, never a guess.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import logging
import re
from typing import Any, Protocol

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.template import Template, result_as_boolean
from homeassistant.util import dt as dt_util

from .const import UNKNOWN_STATES
from .schedule import WEEKDAYS, in_numeric_range
from .snapshot import as_list

_LOGGER = logging.getLogger(__name__)

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

INPUT_ENTITY_ID = re.compile(r"^input_(?:select|text|number|boolean|datetime)\.(?!.+__)(?!_)[\da-z_]+(?<!_)$")
# Templates that depend on the clock or the triggering event can't be replayed.
UNREPLAYABLE = re.compile(r"\b(now|utcnow)\s*\(|\btrigger\b|\bthis\b|\bas_timestamp\s*\(\s*\)")
MAX_CHECKED_OCCURRENCES = 25


# ---------------------------------------------------------------- state sources


@dataclass
class PastState:
    """An entity's state at some past moment."""

    state: Any
    attributes: dict[str, Any]
    last_changed: datetime | None
    complete_attributes: bool  # False when only some attributes were recorded


class StateSource(Protocol):
    basis: str
    exact: bool  # False: values are estimates, so a "fail" is only likely

    def get(self, entity_id: str, when: datetime | None) -> PastState | None:
        """State in effect at `when`, or None if not known."""


class BaselineSource:
    """Pre-downtime values (from the shutdown snapshot)."""

    basis = "baseline"
    exact = False  # values may have changed while HA was down

    def __init__(self, entities: dict[str, dict | None]) -> None:
        self._entities = entities or {}

    def get(self, entity_id: str, when: datetime | None) -> PastState | None:
        rec = self._entities.get(entity_id)
        if not rec:
            return None
        changed = dt_util.parse_datetime(rec.get("last_changed") or "")
        return PastState(rec.get("state"), dict(rec.get("attributes") or {}), changed, False)


class HistorySource:
    """Recorder history: the state in effect at each moment."""

    basis = "history"
    exact = True

    def __init__(self, history: dict[str, list[State]]) -> None:
        self._history = {eid: sorted(states, key=lambda s: s.last_changed) for eid, states in history.items()}

    def get(self, entity_id: str, when: datetime | None) -> PastState | None:
        if when is None:
            return None
        found = None
        for st in self._history.get(entity_id) or []:
            if st.last_changed > when:
                break
            found = st
        if found is None:
            return None
        return PastState(found.state, dict(found.attributes), found.last_changed, True)


class NoSource:
    """No past states available (e.g. what-if without the recorder)."""

    basis = "none"
    exact = True  # nothing is estimated; every state check is simply unknown

    def get(self, entity_id: str, when: datetime | None) -> PastState | None:
        return None


async def async_history_source(
    hass: HomeAssistant, entity_ids: Iterable[str], start: datetime, end: datetime
) -> StateSource:
    """Recorder history for `entity_ids` over [start, end], or NoSource if unavailable."""
    ids = sorted(set(entity_ids))
    if not ids:
        return HistorySource({})
    if "recorder" not in hass.config.components:
        return NoSource()
    try:
        from homeassistant.components.recorder import history  # noqa: PLC0415
        from homeassistant.helpers.recorder import get_instance  # noqa: PLC0415

        states = await get_instance(hass).async_add_executor_job(
            lambda: history.get_significant_states(
                hass,
                start,
                end,
                ids,
                include_start_time_state=True,
                significant_changes_only=False,
            )
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Recorder history unavailable for condition checks", exc_info=True)
        return NoSource()
    return HistorySource({eid: [s for s in v if isinstance(s, State)] for eid, v in states.items()})


# ---------------------------------------------------------------- config helpers


def condition_configs(entity: Any) -> list[dict]:
    """An automation's validated conditions (blueprints expanded), else its raw config."""
    validated = getattr(getattr(entity, "_condition", None), "config", None)
    if validated is not None:
        confs = list(validated)
    else:
        raw = getattr(entity, "raw_config", None) or {}
        confs = as_list(raw.get("conditions") or raw.get("condition"))
    out = []
    for conf in confs:
        if isinstance(conf, str):  # shorthand template
            conf = {"condition": "template", "value_template": conf}
        if isinstance(conf, dict):
            out.append(conf)
    return out


def _opts(conf: dict) -> dict:
    """Fields, whether top-level or under `options` (integration-provided conditions)."""
    opts = conf.get("options")
    return {**conf, **opts} if isinstance(opts, dict) else conf


def _kind(conf: dict) -> str:
    if "condition" in conf:
        return str(conf["condition"])
    for key in ("and", "or", "not"):  # shorthand {"or": [...]}
        if key in conf:
            return key
    return "unknown"


def _children(conf: dict) -> list[dict]:
    kind = _kind(conf)
    kids = conf.get("conditions", conf.get(kind))
    out = []
    for kid in as_list(kids):
        if isinstance(kid, str):
            kid = {"condition": "template", "value_template": kid}
        if isinstance(kid, dict):
            out.append(kid)
    return out


def _enabled(conf: dict) -> bool:
    enabled = conf.get("enabled", True)
    return enabled if isinstance(enabled, bool) else True


def _src(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "template", None) or str(value)


def _as_timedelta(value: Any) -> timedelta | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    try:
        from homeassistant.helpers import config_validation as cv  # noqa: PLC0415

        return cv.time_period(value)
    except Exception:  # noqa: BLE001
        return None


def condition_entities(hass: HomeAssistant, confs: Iterable[dict]) -> dict[str, set[str]]:
    """entity_id → attributes the conditions read (for the baseline and history fetch)."""
    found: dict[str, set[str]] = {}

    def add(eid: Any, attribute: Any = None) -> None:
        if isinstance(eid, str) and "." in eid:
            found.setdefault(eid, set())
            if isinstance(attribute, str):
                found[eid].add(attribute)

    stack = [c for c in confs if isinstance(c, dict)]
    while stack:
        conf = stack.pop()
        kind = _kind(conf)
        if kind in ("and", "or", "not"):
            stack.extend(_children(conf))
            continue
        o = _opts(conf)
        for eid in as_list(o.get("entity_id")):
            add(eid, o.get("attribute"))
        for key in ("above", "below", "after", "before"):
            if isinstance(o.get(key), str) and "." in o[key]:
                add(o[key])
        for value in as_list(o.get("state")):
            if isinstance(value, str) and INPUT_ENTITY_ID.match(value):
                add(value)
        if kind == "template" and (src := _src(o.get("value_template"))):
            try:
                info = Template(src, hass).async_render_to_info()
                for eid in info.entities:
                    add(eid)
            except Exception:  # noqa: BLE001
                pass
    return found


# ---------------------------------------------------------------- three-valued logic


def all_of(results: Iterable[str]) -> str:
    results = list(results)
    if FAIL in results:
        return FAIL
    return PASS if all(r == PASS for r in results) else UNKNOWN


def any_of(results: Iterable[str]) -> str:
    results = list(results)
    if PASS in results:
        return PASS
    return FAIL if all(r == FAIL for r in results) else UNKNOWN


def none_of(results: Iterable[str]) -> str:
    results = list(results)
    if PASS in results:
        return FAIL
    return PASS if all(r == FAIL for r in results) else UNKNOWN


# ---------------------------------------------------------------- evaluator


class ConditionEvaluator:
    """Evaluate a list of conditions (implicitly AND-ed) at a past moment."""

    def __init__(self, hass: HomeAssistant, source: StateSource, trigger_id: str | None) -> None:
        self.hass = hass
        self.source = source
        self.trigger_id = trigger_id

    def evaluate(self, confs: list[dict], when: datetime | None) -> tuple[str, list[dict]]:
        steps = [self._eval(c, when) for c in confs if _enabled(c)]
        return all_of(s["result"] for s in steps), steps

    # -------------------------------------------------------------- dispatch

    def _eval(self, conf: dict, when: datetime | None) -> dict:
        kind = _kind(conf)
        if kind in ("and", "or", "not"):
            kids = [self._eval(c, when) for c in _children(conf) if _enabled(c)]
            combine = {"and": all_of, "or": any_of, "not": none_of}[kind]
            return {"condition": kind, "result": combine(k["result"] for k in kids), "conditions": kids}
        handler = getattr(self, f"_c_{kind}", None)
        if handler is None:
            return self._step(kind, UNKNOWN, f"'{kind}' conditions can't be checked after the fact")
        try:
            return handler(_opts(conf), when)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Condition check failed for %s: %s", conf, err)
            return self._step(kind, UNKNOWN, f"could not be checked: {err}")

    def _step(self, kind: str, result: str, why: str, basis: str | None = None) -> dict:
        step = {"condition": kind, "result": result, "why": why}
        if basis:
            step["basis"] = basis
        return step

    def _past(self, entity_id: str, when: datetime | None) -> PastState | None:
        return self.source.get(entity_id, when)

    # -------------------------------------------------------------- time / sun

    def _resolve_time(self, value: Any, when: datetime) -> tuple[time | None, str | None]:
        """A time-of-day from a literal or an input_datetime / time / timestamp entity."""
        if value is None or isinstance(value, time):
            return value, None
        text = str(value)
        if "." not in text:
            return dt_util.parse_time(text), None
        past = self._past(text, when)
        if past is None:
            st = self.hass.states.get(text)
            if st is None:
                return None, f"{text} has no known value"
            past = PastState(st.state, dict(st.attributes), st.last_changed, True)
        if past.state in UNKNOWN_STATES:
            return None, f"{text} was '{past.state}'"
        parsed_dt = dt_util.parse_datetime(str(past.state))
        if parsed_dt is not None and parsed_dt.tzinfo is not None:  # sensor timestamp
            return dt_util.as_local(parsed_dt).time(), None
        if parsed_dt is not None:  # input_datetime with date and time
            return parsed_dt.time(), None
        return dt_util.parse_time(str(past.state)), None

    def _c_time(self, o: dict, when: datetime | None) -> dict:
        if when is None:
            return self._step("time", UNKNOWN, "the moment it would have fired is unknown")
        local = dt_util.as_local(when)
        now_time = local.time()
        after, err_a = self._resolve_time(o.get("after"), when)
        before, err_b = self._resolve_time(o.get("before"), when)
        if err_a or err_b:
            return self._step("time", UNKNOWN, err_a or err_b or "")
        after = after or time(0)
        before = before or time(23, 59, 59, 999999)
        if after < before:
            ok = after <= now_time < before
        else:  # spans midnight
            ok = not (before <= now_time < after)
        weekday = o.get("weekday")
        if ok and weekday is not None:
            ok = WEEKDAYS[local.weekday()] in as_list(weekday)
        why = f"at {local.strftime('%a %H:%M:%S')}"
        return self._step("time", PASS if ok else FAIL, why)

    def _c_sun(self, o: dict, when: datetime | None) -> dict:
        from homeassistant.helpers.sun import get_astral_event_date  # noqa: PLC0415

        if when is None:
            return self._step("sun", UNKNOWN, "the moment it would have fired is unknown")
        before, after = o.get("before"), o.get("after")
        before_offset = _as_timedelta(o.get("before_offset")) or timedelta(0)
        after_offset = _as_timedelta(o.get("after_offset")) or timedelta(0)
        utc = dt_util.as_utc(when)
        today = dt_util.as_local(utc).date()
        sunrise = get_astral_event_date(self.hass, "sunrise", today)
        sunset = get_astral_event_date(self.hass, "sunset", today)
        has_rise = "sunrise" in (before, after)
        has_set = "sunset" in (before, after)
        # Mirrors homeassistant.components.sun.condition.sun()
        if sunrise is not None and has_rise and today > dt_util.as_local(sunrise).date():
            sunrise = get_astral_event_date(self.hass, "sunrise", today + timedelta(days=1))
        if sunset is not None and has_set and today > dt_util.as_local(sunset).date():
            sunset = get_astral_event_date(self.hass, "sunset", today + timedelta(days=1))
        why = f"at {dt_util.as_local(utc).strftime('%H:%M')}"
        if before == "sunrise" and after == "sunset":
            if sunrise is None or sunset is None:
                return self._step("sun", UNKNOWN, "no sunrise/sunset that day")
            ok = utc < sunrise + before_offset or utc > sunset + after_offset
            return self._step("sun", PASS if ok else FAIL, why)
        if (sunrise is None and has_rise) or (sunset is None and has_set):
            return self._step("sun", FAIL, "no sunrise/sunset that day")
        ok = True
        if before == "sunrise" and utc > sunrise + before_offset:
            ok = False
        if before == "sunset" and utc > sunset + before_offset:
            ok = False
        if after == "sunrise" and utc < sunrise + after_offset:
            ok = False
        if after == "sunset" and utc < sunset + after_offset:
            ok = False
        return self._step("sun", PASS if ok else FAIL, why)

    # -------------------------------------------------------------- state-ish

    def _value(self, past: PastState, attribute: str | None) -> tuple[bool, Any]:
        """(known, value). An attribute missing from a partial record is unknown."""
        if attribute is None:
            return True, past.state
        if attribute in past.attributes:
            return True, past.attributes[attribute]
        return past.complete_attributes, None

    def _c_state(self, o: dict, when: datetime | None) -> dict:
        attribute = o.get("attribute")
        wanted_raw = as_list(o.get("state"))
        for_period = _as_timedelta(o.get("for"))
        results, reasons = [], []
        for eid in as_list(o.get("entity_id")):
            past = self._past(eid, when)
            if past is None:
                results.append(UNKNOWN)
                reasons.append(f"{eid}: no recorded value")
                continue
            known, value = self._value(past, attribute)
            if not known:
                results.append(UNKNOWN)
                reasons.append(f"{eid}: attribute '{attribute}' not recorded")
                continue
            wanted = []
            unresolved = False
            for w in wanted_raw:
                if isinstance(w, str) and INPUT_ENTITY_ID.match(w):
                    ref = self._past(w, when)
                    if ref is None:
                        unresolved = True
                        continue
                    w = ref.state
                wanted.append(w)
            target = f"{eid}" + (f"[{attribute}]" if attribute else "")
            if value in wanted:
                if for_period is not None:
                    if when is None or past.last_changed is None:
                        results.append(UNKNOWN)
                        reasons.append(f"{target} was '{value}', but how long can't be told")
                        continue
                    if when - for_period <= past.last_changed:
                        results.append(FAIL)
                        reasons.append(f"{target} was '{value}' for less than {for_period}")
                        continue
                results.append(PASS)
                reasons.append(f"{target} was '{value}'")
            elif unresolved:
                results.append(UNKNOWN)
                reasons.append(f"{target} was '{value}'; the state it is compared to is unknown")
            else:
                results.append(FAIL)
                reasons.append(f"{target} was '{value}' (wanted {', '.join(map(str, wanted))})")
        combine = any_of if o.get("match") == "any" else all_of
        return self._step("state", combine(results), "; ".join(reasons), self.source.basis)

    def _bound(self, value: Any, when: datetime | None) -> tuple[bool, Any]:
        if isinstance(value, str) and "." in value:
            past = self._past(value, when)
            if past is None or past.state in UNKNOWN_STATES:
                return False, None
            return True, past.state
        return True, value

    def _c_numeric_state(self, o: dict, when: datetime | None) -> dict:
        if o.get("value_template") is not None:
            return self._step("numeric_state", UNKNOWN, "value_template can't be replayed")
        ok_a, above = self._bound(o.get("above"), when)
        ok_b, below = self._bound(o.get("below"), when)
        if not (ok_a and ok_b):
            return self._step("numeric_state", UNKNOWN, "threshold entity has no recorded value")
        attribute = o.get("attribute")
        results, reasons = [], []
        for eid in as_list(o.get("entity_id")):
            past = self._past(eid, when)
            known, value = self._value(past, attribute) if past else (False, None)
            if not known or value in UNKNOWN_STATES:
                results.append(UNKNOWN)
                reasons.append(f"{eid}: no usable value")
                continue
            inside = in_numeric_range(value, above, below)
            results.append(UNKNOWN if inside is None else PASS if inside else FAIL)
            reasons.append(f"{eid} was {value}")
        return self._step("numeric_state", all_of(results), "; ".join(reasons), self.source.basis)

    def _c_zone(self, o: dict, when: datetime | None) -> dict:
        zones = as_list(o.get("zone"))
        names = set()
        for zone_id in zones:
            if zone_id == "zone.home":
                names.add("home")
            elif (st := self.hass.states.get(zone_id)) is not None:
                names.add(st.attributes.get("friendly_name") or st.name)
        results, reasons = [], []
        for eid in as_list(o.get("entity_id")):
            past = self._past(eid, when)
            if past is None or past.state in UNKNOWN_STATES:
                results.append(UNKNOWN)
                reasons.append(f"{eid}: no usable value")
                continue
            results.append(PASS if past.state in names else FAIL)
            reasons.append(f"{eid} was '{past.state}'")
        return self._step("zone", all_of(results), "; ".join(reasons) + " (by zone name)", self.source.basis)

    def _c_trigger(self, o: dict, when: datetime | None) -> dict:
        ids = [str(i) for i in as_list(o.get("id"))]
        ok = self.trigger_id is not None and self.trigger_id in ids
        return self._step("trigger", PASS if ok else FAIL, f"trigger id '{self.trigger_id}'")

    def _c_template(self, o: dict, when: datetime | None) -> dict:
        src = _src(o.get("value_template"))
        if not src:
            return self._step("template", UNKNOWN, "empty template")
        if UNREPLAYABLE.search(src):
            return self._step("template", UNKNOWN, "uses the clock or trigger data")
        info = Template(src, self.hass).async_render_to_info()
        if (
            info.exception
            or info.has_time
            or info.all_states
            or info.all_states_lifecycle
            or info.domains
            or info.domains_lifecycle
        ):
            return self._step("template", UNKNOWN, "reads states that can't be pinned to one moment")
        # Rendered against current states: only valid if every entity it reads
        # had the same value then as now.
        for eid in info.entities:
            past = self._past(eid, when)
            cur = self.hass.states.get(eid)
            if past is None or cur is None:
                return self._step("template", UNKNOWN, f"{eid} has no recorded value", self.source.basis)
            if str(past.state) != cur.state:
                return self._step(
                    "template", UNKNOWN, f"{eid} was '{past.state}' then, '{cur.state}' now", self.source.basis
                )
            if past.complete_attributes and past.attributes != dict(cur.attributes):
                return self._step("template", UNKNOWN, f"{eid} attributes differ from now", self.source.basis)
        ok = result_as_boolean(info.result())
        return self._step("template", PASS if ok else FAIL, "same inputs then as now", self.source.basis)


def first_reason(steps: list[dict], result: str) -> str | None:
    """The first leaf step with `result`, as a sentence (for severity_reason)."""
    for step in steps:
        if "conditions" in step:
            if step["result"] != result:
                continue
            if step["condition"] == "not":  # a NOT fails because a child passed (and vice versa)
                flipped = {PASS: FAIL, FAIL: PASS}.get(result, result)
                inner = first_reason(step["conditions"], flipped)
                return f"not ({inner})" if inner else "a 'not' condition"
            if inner := first_reason(step["conditions"], result):
                return inner
            continue
        if step["result"] == result:
            return f"{step['condition']}: {step['why']}"
    return None


def evaluate_finding(
    hass: HomeAssistant,
    confs: list[dict],
    source: StateSource,
    trigger_id: str | None,
    occurrences: list[str],
) -> dict:
    """Evaluate at each occurrence; any pass → pass, all fail → fail, else unknown.

    With estimated values (the pre-downtime baseline) a fail is reported as
    `unknown` with `likely: "fail"`: the user may have changed something while
    HA was down, so it must not hide the finding.
    """
    whens: list[datetime | None] = [
        dt_util.parse_datetime(o) for o in occurrences[:MAX_CHECKED_OCCURRENCES]
    ] or [None]
    evaluator = ConditionEvaluator(hass, source, trigger_id)
    runs = []
    for when in whens:
        result, steps = evaluator.evaluate(confs, when)
        runs.append((when, result, steps))
    overall = any_of(r for _, r, _ in runs)
    when, _, steps = next(((w, r, s) for w, r, s in runs if r == overall), runs[0])
    why = first_reason(steps, overall) if overall != PASS else None
    out = {
        "result": overall,
        "basis": source.basis,
        "checked": len(runs),
        "shown_for": when.isoformat() if when else None,
        "why": why,
        "steps": steps,
    }
    if overall == FAIL and not getattr(source, "exact", True):
        # Does it still fail using only exact checks (clock, sun, trigger id)?
        exact = all(all_of(_without_estimates(s) for s in steps) == FAIL for _, _, steps in runs)
        if not exact:
            out["result"] = UNKNOWN
            out["likely"] = FAIL
            out["why"] = f"would have failed with pre-downtime values ({why})" if why else None
        else:
            out["why"] = first_reason(_exact_steps(steps), FAIL) or why
    return out


def _without_estimates(step: dict) -> str:
    """A step's result if every check based on estimated values were unknown."""
    if "conditions" in step:
        combine = {"and": all_of, "or": any_of, "not": none_of}[step["condition"]]
        return combine(_without_estimates(k) for k in step["conditions"])
    return UNKNOWN if step.get("basis") == BaselineSource.basis else step["result"]


def _exact_steps(steps: list[dict]) -> list[dict]:
    """Steps with estimated checks marked unknown (to explain an exact fail)."""
    out = []
    for step in steps:
        if "conditions" in step:
            out.append({**step, "result": _without_estimates(step), "conditions": _exact_steps(step["conditions"])})
        elif step.get("basis") == BaselineSource.basis:
            out.append({**step, "result": UNKNOWN})
        else:
            out.append(step)
    return out
