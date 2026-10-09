"""Findings that wouldn't have done anything: unchanged condition values, gated actions, time patterns."""

import copy
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.conditions import (
    FAIL,
    PASS,
    UNKNOWN,
    BaselineSource,
    action_gate,
    automation_conditions,
    condition_entities,
    evaluate_finding,
)
from custom_components.downtime_auditor.const import DOMAIN, Severity
from custom_components.downtime_auditor.severity import effective_severity


def _rec(state, **attrs):
    return {"state": state, "attributes": attrs, "last_changed": (dt_util.utcnow() - timedelta(hours=5)).isoformat()}


def _at(hh, mm):
    return dt_util.as_utc(datetime(2026, 10, 9, hh, mm, tzinfo=dt_util.get_default_time_zone())).isoformat()


DOORS_OPEN = {"condition": "state", "entity_id": "cover.garage", "state": "open"}


# ---------------------------------------------------------------- unchanged values


async def test_value_unchanged_after_startup_is_not_an_estimate(hass):
    """Closed before the downtime and still closed after startup: the condition really failed."""
    source = BaselineSource({"cover.garage": _rec("closed")}, hass)
    hass.states.async_set("cover.garage", "closed")
    res = evaluate_finding(hass, [DOORS_OPEN], source, None, [_at(1, 30)])
    assert res["result"] == FAIL and "likely" not in res
    assert res["steps"][0]["basis"] == "unchanged"
    assert res["why"].startswith("state: cover.garage was 'closed'")

    # Different after startup: it may have been open at 01:30, so only "probably failed".
    hass.states.async_set("cover.garage", "open")
    res = evaluate_finding(hass, [DOORS_OPEN], source, None, [_at(1, 30)])
    assert (res["result"], res["likely"]) == (UNKNOWN, FAIL)
    assert res["steps"][0]["basis"] == "baseline"


@pytest.mark.parametrize(
    ("now_state", "now_attrs", "conf", "unchanged"),
    [
        ("unavailable", {}, DOORS_OPEN, False),  # not back yet
        ("closed", {"position": 0}, {**DOORS_OPEN, "attribute": "position", "state": 100}, True),
        ("closed", {"position": 40}, {**DOORS_OPEN, "attribute": "position", "state": 100}, False),
    ],
)
async def test_unchanged_needs_the_same_value(hass, now_state, now_attrs, conf, unchanged):
    source = BaselineSource({"cover.garage": _rec("closed", position=0)}, hass)
    hass.states.async_set("cover.garage", now_state, now_attrs)
    res = evaluate_finding(hass, [conf], source, None, [_at(1, 30)])
    assert (res["result"] == FAIL) is unchanged
    assert res["steps"][0]["basis"] == ("unchanged" if unchanged else "baseline")


async def test_without_current_states_nothing_is_unchanged(hass):
    hass.states.async_set("cover.garage", "closed")
    res = evaluate_finding(hass, [DOORS_OPEN], BaselineSource({"cover.garage": _rec("closed")}), None, [_at(1, 30)])
    assert (res["result"], res["likely"]) == (UNKNOWN, FAIL)


# ---------------------------------------------------------------- gated actions


def _auto(actions, conditions=()):
    return SimpleNamespace(action_script=SimpleNamespace(sequence=actions), _condition=None,
                           raw_config={"conditions": list(conditions)})


NOTIFY = {"action": "notify.phone", "data": {"message": "hi"}}
LATE = {"condition": "time", "after": "22:00:00", "before": "23:00:00"}


def _gate_result(hass, actions, at=(1, 30), trigger_id=None, source=None):
    confs = automation_conditions(_auto(actions))
    return evaluate_finding(hass, confs, source or BaselineSource({}, hass), trigger_id, [_at(*at)])


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        # CPAP-style: everything is inside a choose whose only option needs a later time.
        ([{"choose": [{"conditions": [LATE], "sequence": [NOTIFY]}]}], FAIL),
        ([{"choose": [{"conditions": [LATE], "sequence": [NOTIFY]}], "default": [NOTIFY]}], PASS),
        ([{"if": [LATE], "then": [NOTIFY]}], FAIL),
        ([{"if": [LATE], "then": [NOTIFY], "else": [NOTIFY]}], PASS),
        ([LATE, NOTIFY], FAIL),  # a condition step stops the rest
        ([{"choose": [{"conditions": [LATE], "sequence": [NOTIFY]}]}, NOTIFY], PASS),  # runs after the choose
        ([{"variables": {"x": 1}}, {"if": [LATE], "then": [NOTIFY]}], FAIL),
        ([{**NOTIFY, "enabled": False}, {"if": [LATE], "then": [NOTIFY]}], FAIL),
    ],
)
async def test_action_gates(hass, actions, expected):
    res = _gate_result(hass, actions)
    assert res["result"] == expected
    if expected == FAIL:
        assert res["why"].startswith("actions: nothing would have run (")


async def test_no_gate_when_the_first_action_always_runs(hass):
    assert action_gate(_auto([NOTIFY, {"if": [LATE], "then": [NOTIFY]}])) is None
    assert action_gate(_auto([{"stop": "done"}])) is None
    assert action_gate(_auto([])) is None
    # Falls back to the raw config when there is no script object.
    raw = SimpleNamespace(raw_config={"actions": [{"if": [LATE], "then": [NOTIFY]}]})
    assert action_gate(raw)["items"][0]["kind"] == "branch"


async def test_gate_with_unknowns(hass):
    unknown = {"condition": "state", "entity_id": "input_boolean.nobody", "state": "on"}
    # An option that can't be told: maybe something ran.
    res = _gate_result(hass, [{"choose": [{"conditions": [unknown], "sequence": [NOTIFY]},
                                          {"conditions": [LATE], "sequence": [NOTIFY]}]}])
    assert res["result"] == UNKNOWN
    # A condition step that can't be told, then nothing else that could run: still nothing ran.
    assert _gate_result(hass, [unknown, {"if": [LATE], "then": [NOTIFY]}])["result"] == FAIL
    # ... but before an action that always runs, it's unknown.
    assert _gate_result(hass, [unknown, NOTIFY])["result"] == UNKNOWN


async def test_gate_uses_the_trigger_id(hass):
    actions = [{"choose": [{"conditions": [{"condition": "trigger", "id": "sync"}], "sequence": [NOTIFY]}]}]
    assert _gate_result(hass, actions, trigger_id="sync")["result"] == PASS
    assert _gate_result(hass, actions, trigger_id="other")["result"] == FAIL


async def test_gate_with_estimates_is_only_probable(hass):
    """A gate that fails on a pre-downtime value that changed since is 'probably', not certainly."""
    source = BaselineSource({"cover.garage": _rec("closed")}, hass)
    hass.states.async_set("cover.garage", "open")
    res = _gate_result(hass, [{"if": [DOORS_OPEN], "then": [NOTIFY]}], source=source)
    assert (res["result"], res["likely"]) == (UNKNOWN, FAIL)
    hass.states.async_set("cover.garage", "closed")
    assert _gate_result(hass, [{"if": [DOORS_OPEN], "then": [NOTIFY]}], source=source)["result"] == FAIL


async def test_gate_entities_are_captured(hass):
    confs = automation_conditions(_auto([{"choose": [{"conditions": [DOORS_OPEN], "sequence": [NOTIFY]}]}]))
    assert "cover.garage" in condition_entities(hass, confs)


# ---------------------------------------------------------------- time patterns that run again soon


@pytest.mark.parametrize(
    ("caught_up_in", "limit", "source", "base", "expected"),
    [
        (15, 60, "default", "medium", "none"),
        (60, 60, "label", "critical", "none"),  # the option beats Critical, like the unconfirmable one
        (61, 60, "default", "medium", "medium"),
        (15, 0, "default", "medium", "medium"),  # 0 turns it off
        (15, 60, "trigger", "high", "high"),  # a rating for this trigger wins
        (None, 60, "default", "medium", "medium"),  # not a time pattern
    ],
)
def test_catch_up_precedence(caught_up_in, limit, source, base, expected):
    sev, reason = effective_severity(
        Severity(base), source, "missed", "confirmed", None, caught_up_in=caught_up_in, catch_up_limit=limit
    )
    assert sev == expected
    if expected == "none":
        assert reason.startswith("the pattern runs again ")


async def _outage(hass, autos, options=None, hours=25):
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False, **(options or {})})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=hours)).isoformat()
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    rep = await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    return entry, {f["entity_id"]: f for f in rep["findings"]}


async def test_time_patterns_that_run_again_soon(hass, enable_custom_integrations):
    daily_hour = (dt_util.now() + timedelta(hours=3)).hour  # next tick 2-3 h after startup
    autos = [
        {"id": "q", "alias": "Quarter", "triggers": [{"trigger": "time_pattern", "minutes": "/15"}], "actions": []},
        {"id": "d", "alias": "Daily", "triggers": [{"trigger": "time_pattern", "hours": daily_hour, "minutes": 0}],
         "actions": []},
    ]
    entry, by = await _outage(hass, autos)
    quarter, daily = by["automation.quarter"], by["automation.daily"]
    assert quarter["severity"] == "none" and quarter["severity_reason"].startswith("the pattern runs again")
    assert 0 <= quarter["details"]["next_due_minutes"] <= 15
    assert daily["severity"] == "medium" and 120 <= daily["details"]["next_due_minutes"] <= 180

    # The option is read when rating, so changing it re-rates the current report.
    hass.config_entries.async_update_entry(entry, options={**entry.options, "time_pattern_catch_up_minutes": 0})
    await hass.async_block_till_done()
    rep = hass.data[DOMAIN].last_report
    assert {f["entity_id"]: f["severity"] for f in rep["findings"]}["automation.quarter"] == "medium"


async def test_real_outage_end_to_end(hass, enable_custom_integrations):
    """The two cases from the field: doors still closed, and a sync gated by a choose."""
    hass.states.async_set("cover.garage", "closed")
    autos = [
        {"id": "g", "alias": "Garage doors", "triggers": [{"trigger": "time_pattern", "hours": "/2"}],
         "conditions": [DOORS_OPEN], "actions": []},
        {"id": "c", "alias": "CPAP sync", "triggers": [{"trigger": "time_pattern", "hours": "/2", "id": "sync"}],
         "actions": [{"choose": [{"conditions": [{"condition": "trigger", "id": "nope"}], "sequence": [NOTIFY]}]}]},
    ]
    _entry, by = await _outage(hass, autos, {"time_pattern_catch_up_minutes": 0}, hours=5)
    garage, cpap = by["automation.garage_doors"], by["automation.cpap_sync"]
    assert "cover.garage" in hass.data[DOMAIN].prev["baseline"]["entities"]
    assert (garage["conditions"], garage["severity"]) == ("fail", "none")
    assert garage["details"]["conditions"]["steps"][0]["basis"] == "unchanged"
    assert (cpap["conditions"], cpap["severity"]) == ("fail", "none")
    assert "actions: nothing would have run" in cpap["severity_reason"]
