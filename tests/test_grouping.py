"""One finding per automation: its missed triggers are listed under it, not reported separately."""

import copy
from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.const import DOMAIN
from custom_components.downtime_auditor.report import group_findings, to_markdown, upgrade_legacy


def _f(entity, summary, severity="medium", confidence="confirmed", ftype="missed", **kw):
    return {"type": ftype, "entity_id": entity, "name": entity.split(".")[1].title(), "summary": summary,
            "severity": severity, "confidence": confidence, "severity_source": "default",
            "severity_base": "medium", "severity_reason": "r", "details": {}, **kw}


def test_group_findings_merges_per_automation():
    flat = [
        _f("automation.pool", "pump unknown", severity="medium", confidence="possible", platform="state", count=None),
        _f("automation.pool", "event lost", severity="low", confidence="unknown", platform="event"),
        _f("automation.pool", "due at 07:00", severity="high", confidence="confirmed", platform="time",
           occurrences=["2026-10-06 07:00:00"], occurrences_iso=["2026-10-06T14:00:00+00:00"], count=1),
        _f("automation.other", "door opened", severity="critical"),
        _f("automation.pool", "was running", ftype="interrupted"),  # a different type stays separate
    ]
    groups = group_findings(flat)
    assert [(g["entity_id"], g["type"]) for g in groups] == [
        ("automation.other", "missed"), ("automation.pool", "missed"), ("automation.pool", "interrupted"),
    ]
    pool = groups[1]
    assert len(pool["triggers"]) == 3
    assert [t["summary"] for t in pool["triggers"]] == ["due at 07:00", "pump unknown", "event lost"]  # most severe first
    assert (pool["severity"], pool["confidence"]) == ("high", "confirmed")
    assert pool["summary"].startswith("3 triggers: due at 07:00; pump unknown")
    assert pool["platform"] == "event, state, time"
    assert pool["trigger_index"] is None and pool["details"] == {}
    assert pool["occurrences_iso"] == ["2026-10-06T14:00:00+00:00"]
    single = groups[0]
    assert single["summary"] == "door opened" and len(single["triggers"]) == 1
    assert group_findings(copy.deepcopy(groups)) == groups  # regrouping is a no-op


def test_v050_report_is_grouped_on_read():
    v050 = {
        "schema": 2,
        "counts": {"interrupted": 0, "missed": 3, "fired_at_startup": 0},
        "needs_attention": 3,
        "findings": [_f("automation.pool", f"sensor {i} unknown") for i in range(3)],
    }
    rep = upgrade_legacy(copy.deepcopy(v050))
    assert rep["schema"] == 3 and "legacy" not in rep
    (g,) = rep["findings"]
    assert len(g["triggers"]) == 3
    assert rep["counts"]["missed"] == 1 and rep["counts_by_severity"]["medium"] == 1
    assert rep["needs_attention"] == 3  # the recorded value is kept (threshold unknown here)
    assert upgrade_legacy(copy.deepcopy(rep)) == rep


async def test_multi_trigger_automation_is_one_finding_and_one_repair(hass, enable_custom_integrations):
    """Like an OPNpool schedule: one state trigger over several entities, plus a time trigger."""
    for eid in ("sensor.pump", "switch.pool", "switch.spa"):
        hass.states.async_set(eid, "off")
    t_missed = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    autos = [{
        "id": "pool", "alias": "Pool schedule",
        "triggers": [
            {"trigger": "state", "entity_id": ["sensor.pump", "switch.pool", "switch.spa"], "to": "on"},
            {"trigger": "time", "at": t_missed},
        ],
        "actions": [],
    }]
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={
        "startup_delay": 0, "write_json": False, "repairs_min_severity": "medium", "persistent_notification": True,
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    for eid in ("sensor.pump", "switch.pool", "switch.spa"):
        hass.states.async_set(eid, "on")  # all three changed "during the downtime"
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    rep = await auditor._async_analyse_previous_downtime()
    await hass.async_block_till_done()

    (finding,) = [f for f in rep["findings"] if f["entity_id"] == "automation.pool_schedule"]
    assert len(finding["triggers"]) == 4  # three entities of the state trigger + the time trigger
    assert finding["summary"].startswith("4 triggers: ")
    assert rep["counts"]["missed"] == 1
    assert hass.states.get("sensor.downtime_auditor_missed_triggers").state == "1"
    attrs = hass.states.get("sensor.downtime_auditor_missed_triggers").attributes["findings"]
    assert attrs[0]["triggers"] == 4

    issues = [i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN]
    assert len(issues) == 1  # one Repair for the automation, not one per trigger
    details = issues[0].translation_placeholders["details"]
    assert details.count("\n- ") == 3 and "sensor.pump" in details and "Time trigger" in details

    md = to_markdown(rep, None)
    assert md.count("Pool schedule") == 1 and "4 triggers" in md

    # Re-rating keeps it one finding and one Repair.
    await auditor.async_rerate_last_report()
    assert len([f for f in auditor.last_report["findings"] if f["entity_id"] == "automation.pool_schedule"]) == 1
    assert len([1 for (d, _) in ir.async_get(hass).issues if d == DOMAIN]) == 1
