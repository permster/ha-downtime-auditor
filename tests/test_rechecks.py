"""Entities still unknown after startup are re-checked when they report, not reported as findings."""

import copy
from datetime import timedelta
import json
from pathlib import Path
import shutil

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.auditor import RECHECK_SETTLE, RECHECK_TIMEOUT
from custom_components.downtime_auditor.const import DOMAIN

ENTITIES = ("sensor.pump", "switch.spa", "switch.aux")


async def _setup(hass):
    shutil.rmtree(hass.config.path("downtime_auditor"), ignore_errors=True)
    for eid in ENTITIES:
        hass.states.async_set(eid, "off")
    autos = [
        # HA fires this itself when the pump goes unknown -> on: nothing was missed.
        {"id": "a", "alias": "Pump on", "triggers": [{"trigger": "state", "entity_id": "sensor.pump", "to": "on"}], "actions": []},
        # HA won't fire this on unknown -> on (from: off): a real miss once the pump reports.
        {"id": "b", "alias": "Pump off to on", "triggers": [{"trigger": "state", "entity_id": "sensor.pump", "from": "off", "to": "on"}], "actions": []},
        # Comes back with the same value as before: nothing changed.
        {"id": "c", "alias": "Spa on", "triggers": [{"trigger": "state", "entity_id": "switch.spa", "to": "on"}], "actions": []},
        # Never reports within the time limit.
        {"id": "d", "alias": "Aux on", "triggers": [{"trigger": "state", "entity_id": "switch.aux", "to": "on"}], "actions": []},
    ]
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    push = async_mock_service(hass, "notify", "test_push")
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={
        "startup_delay": 0, "write_json": True, "notify_service": "notify.test_push",
        "repairs_min_severity": "medium", "push_min_severity": "medium",
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()  # baseline: all three "off"
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
    auditor.prev = saved
    for eid in ENTITIES:  # their integration hasn't come back yet
        hass.states.async_set(eid, "unknown")
    await hass.async_block_till_done()
    auditor.started_at = dt_util.utcnow()
    rep = await auditor._async_analyse_previous_downtime()
    await hass.async_block_till_done()
    return auditor, rep, push


async def test_unknown_entities_are_rechecked_not_reported(hass, enable_custom_integrations):
    updates = async_capture_events(hass, "downtime_auditor_report_updated")
    auditor, rep, push = await _setup(hass)

    # Nothing is reported for entities that hadn't reported yet; they're pending.
    assert [f for f in rep["findings"] if f["type"] == "missed"] == []
    assert sorted((p["automation"], p["entity"]) for p in rep["pending_checks"]) == [
        ("automation.aux_on", "switch.aux"),
        ("automation.pump_off_to_on", "sensor.pump"),
        ("automation.pump_on", "sensor.pump"),
        ("automation.spa_on", "switch.spa"),
    ]
    assert not [1 for (d, _) in ir.async_get(hass).issues if d == DOMAIN]
    push_before = len(push)

    # The integration comes back: pump turned on while HA was down, spa didn't change.
    hass.states.async_set("sensor.pump", "on")
    hass.states.async_set("switch.spa", "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECHECK_SETTLE + 1))
    await hass.async_block_till_done()

    rep = auditor.last_report
    missed = {f["name"]: f for f in rep["findings"] if f["type"] == "missed"}
    assert set(missed) == {"Pump off to on"}  # "Pump on" was run by HA itself; the spa didn't change
    f = missed["Pump off to on"]
    assert f["triggers"][0]["details"]["rechecked"] is True
    assert (f["triggers"][0]["details"]["before"], f["triggers"][0]["details"]["after"]) == ("off", "on")
    assert f["confidence"] == "probable" and f["severity"] == "medium"
    assert [p["entity"] for p in rep["pending_checks"]] == ["switch.aux"]
    assert rep["counts"]["missed"] == 1

    # Everything that shows the report follows: sensors, Repairs, push, event, files.
    assert hass.states.get("sensor.downtime_auditor_missed_triggers").state == "1"
    issues = [i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN]
    assert [i.translation_placeholders["name"] for i in issues] == ["Pump off to on"]
    assert len(push) == push_before + 1 and "Pump off to on" in push[-1].data["message"]
    assert updates[-1].data["added"] == 1 and updates[-1].data["pending_checks"] == 1
    base = Path(hass.config.path("downtime_auditor"))
    saved = json.loads((base / "last_report.json").read_text())
    assert saved["counts"]["missed"] == 1
    history = [json.loads(x) for x in (base / "history.jsonl").read_text().splitlines()]
    assert len(history) == 1  # the line was updated in place, not appended
    assert history[0]["counts"]["missed"] == 1 and history[0]["file"]
    assert (base / "reports" / history[0]["file"]).exists()

    # The aux switch never reports: listed as unchecked, not as a finding.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECHECK_TIMEOUT + 10))
    await hass.async_block_till_done()
    rep = auditor.last_report
    assert rep["pending_checks"] == []
    assert [(u["automation"], u["entity"]) for u in rep["unchecked"]] == [("automation.aux_on", "switch.aux")]
    assert {f["name"] for f in rep["findings"] if f["type"] == "missed"} == {"Pump off to on"}
    assert updates[-1].data["unchecked"] == 1

    # Watching has stopped: a late change no longer touches the report.
    hass.states.async_set("switch.aux", "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECHECK_TIMEOUT + 30))
    await hass.async_block_till_done()
    assert auditor.last_report["unchecked"] and auditor._rechecks is None


async def test_entity_flapping_back_to_unknown_stays_pending(hass, enable_custom_integrations):
    auditor, rep, _push = await _setup(hass)
    hass.states.async_set("switch.spa", "on")
    hass.states.async_set("switch.spa", "unavailable")  # reported once, then dropped again
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=RECHECK_SETTLE + 1))
    await hass.async_block_till_done()
    assert "switch.spa" in {p["entity"] for p in auditor.last_report["pending_checks"]}
    assert not [f for f in auditor.last_report["findings"] if f["type"] == "missed"]


async def test_new_report_stops_old_rechecks(hass, enable_custom_integrations):
    auditor, _rep, _push = await _setup(hass)
    assert auditor._rechecks is not None
    await auditor._async_on_shutdown()  # HA stops again before anything reported
    assert auditor._rechecks is None
