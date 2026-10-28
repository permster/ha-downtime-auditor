"""Restored states that came back older after an unclean stop (reverted findings)."""

import copy
from datetime import timedelta

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import State
from homeassistant.helpers import restore_state
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor import reverts
from custom_components.downtime_auditor.const import DOMAIN

AUTOS = [{"id": "op", "alias": "Off peak", "triggers": [{"trigger": "state", "entity_id": "input_boolean.off_peak"}],
          "actions": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.night"}}]}]


def _was(state, changed="2026-10-27T12:00:00+00:00"):
    return {"state": state, "changed": changed}


def test_compare():
    before = {"a": _was("on"), "b": _was("off"), "c": _was("5"), "d": _was("on"), "e": _was("on")}
    restored = {"a": _was("off"), "b": _was("off"), "c": _was("0"), "d": _was("off")}
    saved = {"a": {"state": "off", "saved_at": "x"}, "c": {"state": "3", "saved_at": "y"}}
    got = {i["entity_id"]: i for i in reverts.compare(before, restored, saved)}
    # a: came back with HA's older copy; b: unchanged; c: didn't come back with HA's copy (an initial value,
    # or it resets by design); d: no saved copy known; e: gone after the restart
    assert set(got) == {"a", "d"}
    assert (got["a"]["before"], got["a"]["after"], got["a"]["saved_at"]) == ("on", "off", "x")
    assert got["d"]["saved_at"] is None


@pytest.mark.parametrize(
    ("entity_id", "value", "expected"),
    [
        ("input_boolean.x", "on", {"action": "input_boolean.turn_on", "data": {"entity_id": "input_boolean.x"}}),
        ("input_boolean.x", "weird", None),
        ("input_number.t", "21.5", {"action": "input_number.set_value", "data": {"entity_id": "input_number.t", "value": "21.5"}}),
        ("input_select.m", "Night", {"action": "input_select.select_option", "data": {"entity_id": "input_select.m", "option": "Night"}}),
        ("input_datetime.d", "2026-10-27 07:00:00", {"action": "input_datetime.set_datetime",
                                                     "data": {"entity_id": "input_datetime.d", "datetime": "2026-10-27 07:00:00"}}),
        ("input_datetime.t", "07:00:00", {"action": "input_datetime.set_datetime", "data": {"entity_id": "input_datetime.t", "time": "07:00:00"}}),
        ("sensor.energy", "12.3", None),
    ],
)
def test_set_back(entity_id, value, expected):
    assert reverts.set_back(entity_id, value) == expected


async def _setup(hass, options=None):
    await async_setup_component(hass, "input_boolean", {"input_boolean": {"off_peak": {}, "night": {}, "unused": {}}})
    await async_setup_component(hass, "automation", {"automation": AUTOS})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, version=2,
                            options={"startup_delay": 0, "write_json": False, **(options or {})})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN]


async def test_scope(hass, enable_custom_integrations):
    auditor = await _setup(hass)
    # input_boolean.night is used too: an automation's action targets it
    assert set(auditor.data["restored"]) == {"input_boolean.off_peak", "input_boolean.night"}
    assert reverts.tracked(hass, "all") >= {"input_boolean.off_peak", "input_boolean.night", "input_boolean.unused"}
    assert reverts.tracked(hass, "off") == set()
    assert "automation.off_peak" not in reverts.tracked(hass, "all")  # runs are covered by other finding types


async def test_changes_are_kept_within_seconds(hass, enable_custom_integrations):
    auditor = await _setup(hass)
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.off_peak"}, blocking=True)
    assert auditor.data["restored"]["input_boolean.off_peak"]["state"] == "on"
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.unused"}, blocking=True)
    assert "input_boolean.unused" not in auditor.data["restored"]  # not used by anything


async def _restart(hass, auditor, clean=False, saved=None):
    """Simulate a restart where off_peak came back 'off' (HA's older saved copy)."""
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.off_peak"}, blocking=True)
    prev = copy.deepcopy(auditor.data)
    prev["session"]["clean_shutdown"] = clean
    prev["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(minutes=5)).isoformat() if clean else None
    prev["session"]["last_heartbeat"] = (dt_util.utcnow() - timedelta(minutes=5)).isoformat()
    auditor.prev = prev
    hass.states.async_set("input_boolean.off_peak", "off")  # what came back
    data = restore_state.async_get(hass)
    for eid, value in (saved or {}).items():
        data.last_states[eid] = restore_state.StoredState(State(eid, value), None, dt_util.utcnow() - timedelta(minutes=8))
    auditor.started_at = dt_util.utcnow()
    auditor._capture_restored_at_start()
    rep = await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    return {f["entity_id"]: f for f in rep["findings"] if f["type"] == "reverted"}


async def test_reverted_after_an_unclean_stop(hass, enable_custom_integrations):
    auditor = await _setup(hass)
    found = await _restart(hass, auditor, saved={"input_boolean.off_peak": "off"})
    f = found["input_boolean.off_peak"]
    assert (f["confidence"], f["severity"]) == ("confirmed", "medium")
    assert f["summary"].startswith("Was 'on' (set at ") and "came back 'off' from Home Assistant's copy saved at" in f["summary"]
    d = f["details"]
    assert (d["before"], d["after"], d["already_back"]) == ("on", "off", False)
    assert [u["entity_id"] for u in d["used_by"]] == ["automation.off_peak"]
    assert d["set_back"] == {"action": "input_boolean.turn_on", "data": {"entity_id": "input_boolean.off_peak"}}
    assert d["effects"]["items"] == [{"entity_id": "input_boolean.off_peak", "action": "was on", "expected": "on"}]
    assert auditor.last_report["counts"]["reverted"] == 1
    assert hass.states.get("sensor.downtime_auditor_reverted_states").state == "1"


async def test_not_after_a_clean_stop(hass, enable_custom_integrations):
    auditor = await _setup(hass)
    assert await _restart(hass, auditor, clean=True, saved={"input_boolean.off_peak": "off"}) == {}


async def test_initial_values_are_not_reverts(hass, enable_custom_integrations):
    """Came back with neither our value nor HA's saved copy: it doesn't restore (initial:), so nothing was lost."""
    auditor = await _setup(hass)
    assert await _restart(hass, auditor, saved={"input_boolean.off_peak": "on"}) == {}


async def test_already_back_is_none(hass, enable_custom_integrations):
    auditor = await _setup(hass)
    prev_analyze = auditor._async_analyze_previous_downtime

    async def _fixed_first():
        # an automation put it back after startup, before the analysis
        hass.states.async_set("input_boolean.off_peak", "on")
        return await prev_analyze()

    auditor._async_analyze_previous_downtime = _fixed_first
    f = (await _restart(hass, auditor))["input_boolean.off_peak"]
    assert f["confidence"] == "probable"  # no saved copy known
    assert f["severity"] == "none" and "already back" in f["severity_reason"]


async def test_off_scope_tracks_nothing(hass, enable_custom_integrations):
    auditor = await _setup(hass, {"reverted_scope": "off"})
    assert auditor.data["restored"] == {}
    assert await _restart(hass, auditor, saved={"input_boolean.off_peak": "off"}) == {}
