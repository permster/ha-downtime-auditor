"""What the actions that didn't run would have changed (shown with live states on the dashboard)."""

import copy
from datetime import timedelta

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.analyzer import Finding
from custom_components.downtime_auditor.const import DOMAIN
from custom_components.downtime_auditor.effects import (
    extract_effects,
    remaining_effects,
    step_effects,
    uses_trigger_ids,
)


def _act(action, entity=None, **data):
    step = {"action": action}
    if entity:
        step["target"] = {"entity_id": entity}
    if data:
        step["data"] = data
    return step


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (_act("light.turn_off", "light.porch", transition=5), [("light.porch", "off")]),
        (_act("input_boolean.turn_on", ["input_boolean.a", "input_boolean.b"]),
         [("input_boolean.a", "on"), ("input_boolean.b", "on")]),
        (_act("cover.close_cover", "cover.garage"), [("cover.garage", "closed")]),
        (_act("lock.unlock", "lock.front"), [("lock.front", "unlocked")]),
        (_act("input_select.select_option", "input_select.mode", option="Night"), [("input_select.mode", "Night")]),
        (_act("input_number.set_value", "input_number.t", value="{{ 3 }}"), [("input_number.t", None)]),
        (_act("homeassistant.turn_off", "switch.pump"), [("switch.pump", "off")]),
        (_act("script.turn_on", "script.resync"), [("script.resync", None)]),
        (_act("light.toggle", "light.desk"), [("light.desk", None)]),
        (_act("notify.phone"), []),
        ({"delay": 5}, []),
    ],
)
def test_step_effects(step, expected):
    assert [(e["entity_id"], e["expected"]) for e in step_effects(step)] == expected


def test_extract_effects_skips_branches_and_disabled_steps():
    seq = [
        _act("light.turn_on", "light.a"),
        {"choose": [{"conditions": [], "sequence": [_act("light.turn_on", "light.b")]}]},  # which option: unknown
        {**_act("light.turn_on", "light.c"), "enabled": False},
        {"sequence": [_act("switch.turn_off", "switch.d")]},
        {"parallel": [_act("light.turn_off", "light.e"), {"sequence": [_act("light.turn_on", "light.a")]}]},
    ]
    assert [e["entity_id"] for e in extract_effects(seq)] == ["light.a", "switch.d", "light.e"]


def test_remaining_effects_after_an_interrupted_step():
    """Stopped at action/0/choose/1/sequence/1: that step, the rest of its sequence, then the outer rest."""
    seq = [
        {"choose": [
            {"conditions": [], "sequence": [_act("light.turn_on", "light.x")]},
            {"conditions": [], "sequence": [_act("light.turn_on", "light.a"), _act("light.turn_off", "light.b"),
                                            _act("light.turn_off", "light.c")]},
        ]},
        _act("switch.turn_off", "switch.d"),
    ]
    got = [e["entity_id"] for e in remaining_effects(seq, "action/0/choose/1/sequence/1")]
    assert got == ["light.b", "light.c", "switch.d"]
    # Scripts' paths start with "sequence"; bad paths give nothing.
    assert [e["entity_id"] for e in remaining_effects(seq, "sequence/1")] == ["switch.d"]
    assert remaining_effects(seq, "action/9") == []
    assert remaining_effects(seq, None) == []


def test_uses_trigger_ids():
    assert uses_trigger_ids([{"choose": [{"conditions": [{"condition": "trigger", "id": "night"}], "sequence": []}]}])
    assert uses_trigger_ids([_act("notify.phone", message="{{ trigger.to_state.state }}")])
    assert not uses_trigger_ids([_act("light.turn_on", "light.a")])


async def _outage(hass, autos):
    await async_setup_component(hass, "input_boolean", {"input_boolean": {"day": {}, "night": {}}})
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    rep = await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    return auditor, {f["entity_id"]: f for f in rep["findings"]}


async def test_missed_findings_say_what_would_have_changed(hass, enable_custom_integrations):
    t = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    later = (dt_util.now() + timedelta(hours=3)).strftime("%H:%M:%S")
    autos = [
        # Day/Night style: the option is picked by trigger id
        {"id": "dn", "alias": "Day night", "triggers": [{"trigger": "time", "at": t, "id": "night"},
                                                       {"trigger": "time", "at": later, "id": "day"}],
         "actions": [{"choose": [
             {"conditions": [{"condition": "trigger", "id": "night"}],
              "sequence": [_act("input_boolean.turn_on", "input_boolean.night"),
                           _act("input_boolean.turn_off", "input_boolean.day")]},
             {"conditions": [{"condition": "trigger", "id": "day"}],
              "sequence": [_act("input_boolean.turn_on", "input_boolean.day")]},
         ]}]},
        {"id": "pl", "alias": "Plain", "triggers": [{"trigger": "time", "at": t}],
         "actions": [_act("input_boolean.turn_on", "input_boolean.day")]},
    ]
    _auditor, by = await _outage(hass, autos)
    dn = by["automation.day_night"]["details"]["effects"]
    assert [(e["entity_id"], e["expected"]) for e in dn["items"]] == [
        ("input_boolean.night", "on"), ("input_boolean.day", "off")]
    assert dn["certain"] is True and dn["by_trigger"] is True
    plain = by["automation.plain"]["details"]["effects"]
    assert plain == {"items": [{"entity_id": "input_boolean.day", "action": "input_boolean.turn_on", "expected": "on"}],
                     "certain": True, "by_trigger": False}


async def test_interrupted_findings_say_what_didnt_run(hass, enable_custom_integrations):
    autos = [{"id": "lf", "alias": "Lights front", "triggers": [{"trigger": "event", "event_type": "x"}],
              "actions": [{"choose": [{"conditions": [],
                                       "sequence": [_act("input_boolean.turn_off", "input_boolean.day")]}]},
                          _act("input_boolean.turn_on", "input_boolean.night")]}]
    auditor, _by = await _outage(hass, autos)
    f = Finding(type="interrupted", confidence="confirmed", entity_id="automation.lights_front", name="Lights front",
                summary="", details={"runs": [{"last_step": "action/0/choose/0/sequence/0"}]})
    auditor._add_effects([f])
    assert [(e["entity_id"], e["expected"]) for e in f.details["effects"]["items"]] == [
        ("input_boolean.day", "off"), ("input_boolean.night", "on")]
