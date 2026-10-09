"""Condition checks at a past moment (three-valued: pass / fail / unknown)."""

import copy
from datetime import datetime, time, timedelta

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.conditions import (
    FAIL,
    PASS,
    UNKNOWN,
    BaselineSource,
    ConditionEvaluator,
    HistorySource,
    NoSource,
    evaluate_finding,
)
from custom_components.downtime_auditor.const import DOMAIN


def _rec(state, minutes_ago=600, **attrs):
    return {
        "state": state,
        "attributes": attrs,
        "last_changed": (dt_util.utcnow() - timedelta(minutes=minutes_ago)).isoformat(),
    }


BASE = BaselineSource(
    {
        "input_boolean.vac": _rec("off"),
        "input_boolean.recent": _rec("on", minutes_ago=2),
        "sensor.temp": _rec("21.5", unit="C"),
        "sensor.limit": _rec("20"),
        "input_datetime.wake": _rec("06:30:00"),
        "person.anne": _rec("Work"),
        "input_select.mode": _rec("away"),
        "input_text.wanted": _rec("away"),
    }
)


def _at(hass, hh, mm, weekday=0):
    """A local datetime on a known weekday (2026-10-05 is a Monday)."""
    day = datetime(2026, 10, 5 + weekday, hh, mm, tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(day)


def _one(hass, conf, when=None, source=BASE, trigger_id=None):
    result, steps = ConditionEvaluator(hass, source, trigger_id).evaluate([conf], when)
    return result, steps[0]


# ---------------------------------------------------------------- time / sun


@pytest.mark.parametrize(
    ("conf", "hh", "mm", "weekday", "expected"),
    [
        ({"condition": "time", "after": time(7), "before": time(22)}, 8, 0, 0, PASS),
        ({"condition": "time", "after": time(7), "before": time(22)}, 23, 0, 0, FAIL),
        ({"condition": "time", "after": time(22), "before": time(6)}, 23, 30, 0, PASS),  # spans midnight
        ({"condition": "time", "after": time(22), "before": time(6)}, 12, 0, 0, FAIL),
        ({"condition": "time", "after": "07:00:00"}, 8, 0, 0, PASS),  # raw config strings
        ({"condition": "time", "weekday": ["sat", "sun"]}, 8, 0, 0, FAIL),
        ({"condition": "time", "weekday": ["sat", "sun"]}, 8, 0, 5, PASS),
        ({"condition": "time", "weekday": "mon"}, 8, 0, 0, PASS),
        ({"condition": "time", "after": "input_datetime.wake"}, 6, 0, 0, FAIL),  # entity from baseline
        ({"condition": "time", "after": "input_datetime.wake"}, 7, 0, 0, PASS),
    ],
)
async def test_time(hass, conf, hh, mm, weekday, expected):
    assert _one(hass, conf, _at(hass, hh, mm, weekday))[0] == expected


async def test_time_without_a_moment_is_unknown(hass):
    assert _one(hass, {"condition": "time", "after": time(7)}, None)[0] == UNKNOWN


async def test_sun(hass):
    day = dt_util.as_local(_at(hass, 12, 0)).date()
    sunset = get_astral_event_date(hass, "sunset", day)
    after_sunset = {"condition": "sun", "options": {"after": "sunset", "after_offset": timedelta(minutes=-30)}}
    assert _one(hass, after_sunset, sunset - timedelta(minutes=10))[0] == PASS
    assert _one(hass, after_sunset, sunset - timedelta(minutes=40))[0] == FAIL
    night = {"condition": "sun", "after": "sunset", "before": "sunrise"}  # top-level fields work too
    assert _one(hass, night, sunset + timedelta(hours=2))[0] == PASS
    assert _one(hass, night, _at(hass, 12, 0))[0] == FAIL


# ---------------------------------------------------------------- state-ish


async def test_state_from_baseline(hass):
    vac_off = {"condition": "state", "entity_id": ["input_boolean.vac"], "state": "off"}
    result, step = _one(hass, vac_off)
    assert (result, step["basis"]) == (PASS, "baseline")
    result, step = _one(hass, {**vac_off, "state": "on"})
    assert result == FAIL and "input_boolean.vac was 'off'" in step["why"]
    assert _one(hass, {**vac_off, "entity_id": ["input_boolean.missing"]})[0] == UNKNOWN
    assert _one(hass, {**vac_off, "state": ["on", "off"]})[0] == PASS
    # match: any
    both = {"condition": "state", "entity_id": ["input_boolean.vac", "input_boolean.recent"], "state": "on"}
    assert _one(hass, both)[0] == FAIL
    assert _one(hass, {**both, "match": "any"})[0] == PASS
    # state compared to an input_* entity
    assert _one(hass, {"condition": "state", "entity_id": ["input_select.mode"], "state": "input_text.wanted"})[0] == PASS


async def test_state_for_and_attributes(hass):
    when = dt_util.utcnow()
    held = {"condition": "state", "entity_id": ["input_boolean.vac"], "state": "off", "for": timedelta(minutes=5)}
    assert _one(hass, held, when)[0] == PASS  # off for 10 hours
    recent = {"condition": "state", "entity_id": ["input_boolean.recent"], "state": "on", "for": timedelta(minutes=5)}
    assert _one(hass, recent, when)[0] == FAIL  # on for only 2 minutes
    assert _one(hass, recent, None)[0] == UNKNOWN
    attr = {"condition": "state", "entity_id": ["sensor.temp"], "attribute": "unit", "state": "C"}
    assert _one(hass, attr)[0] == PASS
    # An attribute the baseline didn't record is unknown, not "missing"
    assert _one(hass, {**attr, "attribute": "friendly_name"})[0] == UNKNOWN


async def test_numeric_state(hass):
    conf = {"condition": "numeric_state", "entity_id": ["sensor.temp"], "above": 20.0}
    assert _one(hass, conf)[0] == PASS
    assert _one(hass, {**conf, "above": 25})[0] == FAIL
    assert _one(hass, {**conf, "above": "sensor.limit"})[0] == PASS  # threshold from an entity
    assert _one(hass, {**conf, "above": "sensor.nope"})[0] == UNKNOWN
    assert _one(hass, {**conf, "value_template": "{{ 1 }}"})[0] == UNKNOWN


async def test_zone_by_name(hass):
    hass.states.async_set("zone.work", "0", {"friendly_name": "Work"})
    assert _one(hass, {"condition": "zone", "options": {"entity_id": ["person.anne"], "zone": ["zone.work"]}})[0] == PASS
    assert _one(hass, {"condition": "zone", "options": {"entity_id": ["person.anne"], "zone": ["zone.home"]}})[0] == FAIL


async def test_template(hass):
    hass.states.async_set("input_boolean.vac", "off")
    same = {"condition": "template", "value_template": "{{ is_state('input_boolean.vac', 'off') }}"}
    assert _one(hass, same)[0] == PASS
    assert _one(hass, {**same, "value_template": "{{ is_state('input_boolean.vac', 'on') }}"})[0] == FAIL
    # The value changed since the baseline: rendering now would be wrong.
    hass.states.async_set("input_boolean.vac", "on")
    assert _one(hass, same)[0] == UNKNOWN
    for src in ("{{ now().hour > 6 }}", "{{ trigger.id == 'x' }}", "{{ states.light | count > 0 }}"):
        assert _one(hass, {"condition": "template", "value_template": src})[0] == UNKNOWN, src


# ---------------------------------------------------------------- logic and others


async def test_three_valued_logic(hass):
    p = {"condition": "state", "entity_id": ["input_boolean.vac"], "state": "off"}
    f = {"condition": "state", "entity_id": ["input_boolean.vac"], "state": "on"}
    u = {"condition": "device", "device_id": "x"}
    assert _one(hass, {"condition": "and", "conditions": [p, u]})[0] == UNKNOWN
    assert _one(hass, {"condition": "and", "conditions": [f, u]})[0] == FAIL
    assert _one(hass, {"condition": "or", "conditions": [f, u]})[0] == UNKNOWN
    assert _one(hass, {"condition": "or", "conditions": [p, u]})[0] == PASS
    assert _one(hass, {"or": [f, f]})[0] == FAIL  # shorthand
    assert _one(hass, {"condition": "not", "conditions": [f]})[0] == PASS
    assert _one(hass, {"condition": "not", "conditions": [f, p]})[0] == FAIL
    assert _one(hass, {"condition": "not", "conditions": [f, u]})[0] == UNKNOWN
    # disabled conditions are skipped
    assert _one(hass, {"condition": "and", "conditions": [p, {**f, "enabled": False}]})[0] == PASS


async def test_trigger_condition(hass):
    conf = {"condition": "trigger", "id": ["morning", "1"]}
    assert _one(hass, conf, trigger_id="morning")[0] == PASS
    assert _one(hass, conf, trigger_id="1")[0] == PASS  # unnamed triggers use their index
    assert _one(hass, conf, trigger_id="evening")[0] == FAIL


async def test_evaluate_finding_across_occurrences(hass):
    confs = [{"condition": "time", "after": time(7), "before": time(9)}]
    occ = [_at(hass, 6, 0).isoformat(), _at(hass, 8, 0).isoformat()]
    assert evaluate_finding(hass, confs, BASE, None, occ)["result"] == PASS  # any occurrence passing
    res = evaluate_finding(hass, confs, BASE, None, occ[:1])
    assert res["result"] == FAIL and res["why"].startswith("time: at")
    # Baseline values are estimates: a fail is only "likely", so it doesn't hide the finding.
    vac_on = [{"condition": "state", "entity_id": ["input_boolean.vac"], "state": "on"}]
    res = evaluate_finding(hass, vac_on, BASE, None, [])
    assert (res["result"], res["likely"]) == (UNKNOWN, FAIL)
    assert res["why"] == "would have failed with pre-downtime values (state: input_boolean.vac was 'off' (wanted on))"
    vac_off = [{"condition": "state", "entity_id": ["input_boolean.vac"], "state": "off"}]
    assert evaluate_finding(hass, vac_off, BASE, None, [])["result"] == PASS
    assert evaluate_finding(hass, vac_on, NoSource(), None, [])["result"] == UNKNOWN
    # Exact sources (the clock, recorder history) do fail.
    from homeassistant.core import State

    t0 = dt_util.utcnow() - timedelta(hours=1)
    hist = HistorySource({"input_boolean.vac": [State("input_boolean.vac", "off", last_changed=t0, last_updated=t0)]})
    res = evaluate_finding(hass, vac_on, hist, None, [dt_util.utcnow().isoformat()])
    assert res["result"] == FAIL and "likely" not in res


async def test_exact_fail_beats_estimates(hass):
    """After a real outage, a fail decided by the clock alone is still a fail."""
    too_early = {"condition": "time", "after": time(7)}
    vac_on = {"condition": "state", "entity_id": ["input_boolean.vac"], "state": "on"}  # baseline: off
    vac_off = {**vac_on, "state": "off"}
    at_six = [_at(hass, 6, 0).isoformat()]
    at_eight = [_at(hass, 8, 0).isoformat()]

    res = evaluate_finding(hass, [too_early, vac_on], BASE, None, at_six)
    assert res["result"] == FAIL and "likely" not in res and res["why"].startswith("time: at")
    res = evaluate_finding(hass, [vac_on, too_early], BASE, None, at_six)  # order doesn't matter
    assert res["result"] == FAIL and res["why"].startswith("time: at")
    assert evaluate_finding(hass, [too_early, vac_off], BASE, None, at_six)["result"] == FAIL
    res = evaluate_finding(hass, [too_early, vac_on], BASE, None, at_eight)
    assert (res["result"], res["likely"]) == (UNKNOWN, FAIL)
    # A NOT decided by a baseline value is an estimate too.
    res = evaluate_finding(hass, [{"condition": "not", "conditions": [vac_off]}], BASE, None, at_eight)
    assert (res["result"], res["likely"]) == (UNKNOWN, FAIL)
    assert res["why"].startswith("would have failed with pre-downtime values (not (state:")


async def test_history_source_picks_state_in_effect(hass):
    from homeassistant.core import State

    t0 = dt_util.utcnow() - timedelta(hours=3)
    states = [
        State("input_boolean.vac", "on", last_changed=t0, last_updated=t0),
        State("input_boolean.vac", "off", last_changed=t0 + timedelta(hours=2), last_updated=t0 + timedelta(hours=2)),
    ]
    src = HistorySource({"input_boolean.vac": states})
    assert src.get("input_boolean.vac", t0 + timedelta(hours=1)).state == "on"
    assert src.get("input_boolean.vac", t0 + timedelta(hours=2, minutes=1)).state == "off"
    assert src.get("input_boolean.vac", t0 - timedelta(minutes=1)) is None
    assert src.get("input_boolean.vac", None) is None


# ---------------------------------------------------------------- end to end


async def _setup_autos(hass, t_missed):
    await async_setup_component(hass, "input_boolean", {"input_boolean": {"vac": {}}})
    autos = [
        {"id": "home", "alias": "Home only", "triggers": [{"trigger": "time", "at": t_missed}],
         "conditions": [{"condition": "state", "entity_id": "input_boolean.vac", "state": "off"}], "actions": []},
        {"id": "away", "alias": "Away only", "triggers": [{"trigger": "time", "at": t_missed}],
         "conditions": [{"condition": "state", "entity_id": "input_boolean.vac", "state": "on"}], "actions": []},
    ]
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()


async def test_real_outage_conditions(hass, enable_custom_integrations):
    """Baseline holds the condition entity; a likely-failing condition is flagged but not hidden."""
    await _setup_autos(hass, (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S"))
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False, "repairs_min_severity": "low"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()  # vac is off in the baseline
    saved = copy.deepcopy(auditor.data)
    assert "input_boolean.vac" in saved["baseline"]["entities"]  # captured for conditions
    # Changed since the shutdown: the pre-downtime value is only an estimate.
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.vac"}, blocking=True)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    rep = await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()

    by = {f["entity_id"]: f for f in rep["findings"]}
    home, away = by["automation.home_only"], by["automation.away_only"]
    assert (home["conditions"], home["severity"]) == ("pass", "medium")
    assert home["details"]["conditions"]["basis"] == "baseline"
    # vac might have been switched on while HA was down, so this stays visible at Medium.
    assert (away["conditions"], away["severity"]) == ("unknown", "medium")
    assert away["details"]["conditions"]["likely"] == "fail"
    assert away["severity_reason"] == (
        "default for unrated automations; conditions would probably have failed (pre-downtime values)"
    )
    issues = {i.translation_placeholders["name"]: i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN}
    assert set(issues) == {"Home only", "Away only"}
    assert "Conditions: probably failed — would have failed with pre-downtime values" in (
        issues["Away only"].translation_placeholders["details"]
    )
    assert hass.states.get("sensor.downtime_auditor_missed_triggers").state == "2"
    assert rep["counts_by_severity"]["none"] == 0


async def test_what_if_uses_recorder_history(recorder_mock, hass, enable_custom_integrations, freezer):
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    now = dt_util.utcnow()
    freezer.move_to(now - timedelta(hours=3))
    await _setup_autos(hass, dt_util.as_local(now - timedelta(hours=1)).strftime("%H:%M:%S"))
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.vac"}, blocking=True)
    await async_wait_recording_done(hass)
    freezer.move_to(now - timedelta(minutes=30))  # vac turned off after the missed time
    await hass.services.async_call("input_boolean", "turn_off", {"entity_id": "input_boolean.vac"}, blocking=True)
    await async_wait_recording_done(hass)
    freezer.move_to(now)

    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rep = await hass.data[DOMAIN].async_analyze_window(now - timedelta(hours=2), now, notify=False)
    by = {f["entity_id"]: f for f in rep["findings"]}
    # At the missed time vac was ON (history), even though it is OFF now.
    assert by["automation.away_only"]["conditions"] == "pass"
    assert by["automation.home_only"]["conditions"] == "fail"
    assert by["automation.home_only"]["severity"] == "none"
    assert by["automation.home_only"]["details"]["conditions"]["basis"] == "history"
    assert rep["meta"]["conditions_basis"] == "history"


async def test_what_if_without_recorder_is_unknown(hass, enable_custom_integrations):
    now = dt_util.utcnow()
    await _setup_autos(hass, dt_util.as_local(now - timedelta(hours=1)).strftime("%H:%M:%S"))
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    rep = await hass.data[DOMAIN].async_analyze_window(now - timedelta(hours=2), now, notify=False)
    assert {f["conditions"] for f in rep["findings"]} == {"unknown"}
    assert {f["severity"] for f in rep["findings"]} == {"medium"}
    assert rep["meta"]["conditions_basis"] == "none"
