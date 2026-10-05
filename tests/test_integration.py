"""End-to-end: run HA, snapshot a clean shutdown, fake a 2h outage, analyse."""

import asyncio
import copy
from datetime import timedelta
import json
from pathlib import Path

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.auditor import DowntimeAuditor
from custom_components.downtime_auditor.const import DOMAIN


async def _setup(hass, t_missed):
    await async_setup_component(hass, "input_boolean", {"input_boolean": {"flag": {}}})
    hass.states.async_set("binary_sensor.door", "off")
    hass.states.async_set("binary_sensor.window", "off")
    hass.states.async_set("binary_sensor.garage", "off")
    hass.states.async_set("sensor.temp", "20")
    autos = [
        {"id": "a_time", "alias": "Morning", "triggers": [{"trigger": "time", "at": t_missed}], "actions": []},
        {"id": "b_state", "alias": "Door", "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"}], "actions": []},
        {"id": "b2_for", "alias": "Window", "triggers": [{"trigger": "state", "entity_id": "binary_sensor.window", "to": "on", "for": "00:05:00"}], "actions": []},
        {"id": "b3_from", "alias": "Garage closed", "triggers": [{"trigger": "state", "entity_id": "binary_sensor.garage", "from": "on", "to": "off"}], "actions": []},
        {"id": "c_tpl", "alias": "Flag", "triggers": [{"trigger": "template", "value_template": "{{ is_state('input_boolean.flag','on') }}"}], "actions": []},
        {"id": "d_num", "alias": "Hot", "triggers": [{"trigger": "numeric_state", "entity_id": "sensor.temp", "above": 30}], "actions": []},
        {"id": "e_long", "alias": "Long runner", "mode": "parallel", "triggers": [{"trigger": "event", "event_type": "start_long"}],
         "actions": [{"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.flag"}}, {"delay": "00:10:00"}, {"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.flag"}}]},
        {"id": "f_evt", "alias": "Evt", "triggers": [{"trigger": "event", "event_type": "foo"}], "actions": []},
        {"id": "h_pat", "alias": "Every15", "triggers": [{"trigger": "time_pattern", "minutes": "/15"}], "actions": []},
        {"id": "i_off", "alias": "Disabled", "initial_state": False, "triggers": [{"trigger": "time", "at": t_missed}], "actions": []},
        {"id": "j_start", "alias": "On start", "triggers": [{"trigger": "homeassistant", "event": "start"}], "actions": []},
    ]
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()


async def test_full_restart_cycle(hass, enable_custom_integrations):
    now = dt_util.now()
    t_missed = (now - timedelta(hours=1)).strftime("%H:%M:%S")
    await _setup(hass, t_missed)
    push = async_mock_service(hass, "notify", "test_push")

    entry = MockConfigEntry(
        domain=DOMAIN,
        options={"startup_delay": 0, "notify_service": "notify.test_push", "write_json": True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor: DowntimeAuditor = hass.data[DOMAIN]
    assert auditor.active  # loaded while running => no analysis, tracking only

    # Start a run that will be sitting in a 10 minute delay.
    hass.states.async_set("binary_sensor.garage", "on")
    await async_setup_component(hass, "script", {"script": {"slow": {"sequence": [{"wait_template": "{{ false }}"}]}}})
    hass.bus.async_fire("start_long")
    await hass.services.async_call("script", "turn_on", {"entity_id": "script.slow"})
    for _ in range(20):  # don't block_till_done: the run sits in a 10 min delay
        await asyncio.sleep(0)
    assert hass.states.get("automation.long_runner").attributes["current"] == 1

    # Clean shutdown snapshot (this is what the shutdown job does).
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    await hass.services.async_call("script", "turn_off", {"entity_id": "script.slow"}, blocking=True)
    assert saved["session"]["clean_shutdown"] is True
    runs = saved["running"]["runs"]
    assert [r["entity_id"] for r in runs] == ["automation.long_runner", "script.slow"]
    assert runs[0]["runs"][0]["last_step"].startswith("action/1")
    assert runs[1]["runs"][0]["last_step"] == "sequence/0"
    assert saved["baseline"]["templates"]  # template baseline captured

    # Pretend HA died 2h ago and things changed while it was down.
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all", "stop_actions": True}, blocking=True)
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("binary_sensor.window", "on")
    hass.states.async_set("binary_sensor.garage", "off")
    hass.states.async_set("sensor.temp", "35")
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": "input_boolean.flag"}, blocking=True)
    for eid in hass.states.async_entity_ids("automation"):
        if eid != "automation.disabled":
            await hass.services.async_call("automation", "turn_on", {"entity_id": eid}, blocking=True)
    await hass.async_block_till_done()

    new = DowntimeAuditor(hass, entry)
    new.prev = saved
    new.started_at = dt_util.utcnow()
    report = await new._async_analyse_previous_downtime()
    await hass.async_block_till_done()

    by = {}
    for f in report["findings"]:
        by.setdefault(f["entity_id"], []).append(f)

    def cat(eid):
        return [f["category"] for f in by.get(eid, [])]

    assert cat("automation.long_runner")[0] == "interrupted"
    interrupted = by["automation.long_runner"][0]["summary"]
    assert "delay" in interrupted and "elapsed" in interrupted
    assert "(waiting)" in by["script.slow"][0]["summary"]
    assert cat("automation.morning") == ["missed"]
    assert cat("automation.door") == ["missed"]
    assert cat("automation.window") == ["possibly_missed"]  # has for:
    assert cat("automation.garage_closed") == ["missed"]  # from on -> off
    assert cat("automation.flag") == ["missed"]  # template false -> true
    assert cat("automation.hot") == ["missed"]  # 20 -> 35 above 30
    assert cat("automation.evt") == ["unverifiable"]
    pat = by["automation.every15"][0]
    assert pat["category"] == "missed" and pat["count"] == 8
    assert cat("automation.disabled") == ["skipped"]
    assert "automation.on_start" not in by

    assert report["window"]["clean_shutdown"] is True
    path = report["meta"]["json_path"]
    assert path and Path(path).exists()
    assert json.loads(Path(path).read_text())["counts"]["missed"] >= 6
    assert (Path(path).parent.parent / "history.jsonl").exists()

    notif = hass.states.get("persistent_notification.downtime_auditor_report")
    assert len(push) == 1 and "missed" in push[0].data["message"]
    print("\n==== PUSH ====\n", push[0].data["title"], "\n", push[0].data["message"])
    from custom_components.downtime_auditor.report import to_markdown
    print("\n==== NOTIFICATION ====\n" + to_markdown(report, path))


async def test_unclean_uses_heartbeat_and_covered(hass, enable_custom_integrations):
    now = dt_util.now()
    t_missed = (now - timedelta(minutes=30)).strftime("%H:%M:%S")
    await _setup(hass, t_missed)
    entry = MockConfigEntry(domain=DOMAIN, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_heartbeat(dt_util.utcnow())
    saved = copy.deepcopy(auditor.data)
    assert saved["session"]["clean_shutdown"] is False
    saved["session"]["last_heartbeat"] = (dt_util.utcnow() - timedelta(hours=1)).isoformat()

    # Automation actually ran after the last heartbeat (HA died later than we know).
    await hass.services.async_call("automation", "trigger", {"entity_id": "automation.morning"}, blocking=True)
    await hass.async_block_till_done()

    new = DowntimeAuditor(hass, entry)
    new.prev = saved
    new.started_at = dt_util.utcnow() + timedelta(seconds=1)
    report = await new._async_analyse_previous_downtime()
    assert report["window"]["clean_shutdown"] is False
    cats = [f["category"] for f in report["findings"] if f["entity_id"] == "automation.morning"]
    assert cats == []  # covered by last_triggered


async def test_what_if_service(hass, enable_custom_integrations):
    now = dt_util.now()
    await _setup(hass, (now - timedelta(hours=1)).strftime("%H:%M:%S"))
    entry = MockConfigEntry(domain=DOMAIN, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    resp = await hass.services.async_call(
        DOMAIN,
        "analyze_window",
        {"start": (now - timedelta(hours=2)).replace(tzinfo=None), "end": now.replace(tzinfo=None), "notify": True},
        blocking=True,
        return_response=True,
    )
    ids = {f["entity_id"] for f in resp["findings"]}
    assert "automation.morning" in ids and "automation.every15" in ids
    assert "automation.door" not in ids


async def test_config_flow(hass, enable_custom_integrations):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "persistent_notification": True,
            "notify_service": "bad",
            "push_only_on_findings": True,
            "write_json": True,
            "report_retention_days": 30,
            "history_retention_days": 365,
            "heartbeat_interval": 60,
            "startup_delay": 30,
            "include_scripts": True,
            "include_unverifiable": True,
            "max_window_days": 14,
        },
    )
    assert result["errors"] == {"notify_service": "invalid_service"}


async def test_real_boot_path_and_shutdown_job(hass, enable_custom_integrations, hass_storage):
    """Boot with stored previous session -> report after HA 'started'; then real stop saves snapshot."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from custom_components.downtime_auditor.const import STORAGE_KEY

    now = dt_util.now()
    t_missed = (now - timedelta(minutes=20)).strftime("%H:%M:%S")
    hass.set_state(CoreState.not_running)
    await _setup(hass, t_missed)
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "session": {
                "id": "prev",
                "last_heartbeat": (dt_util.utcnow() - timedelta(minutes=45)).isoformat(),
                "clean_shutdown": False,
                "shutdown_at": None,
                "ha_version": "2026.1.0",
            },
            "baseline": {"captured_at": "x", "entities": {"binary_sensor.door": {"state": "off", "attributes": {}}}, "templates": {}, "automations": {}},
            "running": None,
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    assert not auditor.active

    hass.states.async_set("binary_sensor.door", "on")  # changed "during downtime"
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    from pytest_homeassistant_custom_component.common import async_fire_time_changed
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert auditor.active
    rep = auditor.last_report
    assert rep and rep["window"]["clean_shutdown"] is False
    ids = {f["entity_id"]: f["category"] for f in rep["findings"]}
    assert ids.get("automation.morning") == "missed"
    assert ids.get("automation.door") == "missed"
    assert rep["meta"]["ha_version_before"] == "2026.1.0"

    # Now a real HA stop with a run in progress: the shutdown job must persist a
    # clean snapshot, and HA cancelling the run afterwards must not erase it.
    hass.bus.async_fire("start_long")
    for _ in range(20):
        await asyncio.sleep(0)
    await hass.async_stop(force=True)
    stored = hass_storage[STORAGE_KEY]["data"]
    assert stored["session"]["clean_shutdown"] is True
    assert stored["session"]["shutdown_at"]
    assert [r["entity_id"] for r in stored["running"]["runs"]] == ["automation.long_runner"]


async def test_entities_repairs_and_websocket(hass, enable_custom_integrations, hass_ws_client):
    import shutil

    from homeassistant.helpers import issue_registry as ir

    shutil.rmtree(hass.config.path("downtime_auditor"), ignore_errors=True)

    now = dt_util.now()
    await _setup(hass, (now - timedelta(hours=1)).strftime("%H:%M:%S"))
    entry = MockConfigEntry(domain=DOMAIN, options={"startup_delay": 0, "write_json": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]

    # Before any report, category sensors are unknown
    assert hass.states.get("sensor.downtime_auditor_missed_triggers").state == "unknown"

    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)
    hass.states.async_set("binary_sensor.door", "on")
    await hass.services.async_call("automation", "turn_on", {"entity_id": "all"}, blocking=True)

    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    await auditor._async_analyse_previous_downtime()
    await hass.async_block_till_done()

    missed = hass.states.get("sensor.downtime_auditor_missed_triggers")
    assert int(missed.state) >= 3
    names = {f["entity_id"] for f in missed.attributes["findings"]}
    assert {"automation.morning", "automation.door", "automation.every15"} <= names
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "on"
    assert hass.states.get("binary_sensor.downtime_auditor_last_shutdown_unclean").state == "off"
    dur = hass.states.get("sensor.downtime_auditor_last_downtime_duration")
    assert dur.attributes["unit_of_measurement"] in ("min", "s")

    reg = ir.async_get(hass)
    ours = [i for (d, _), i in reg.issues.items() if d == DOMAIN]
    assert len(ours) == int(missed.state)
    assert all(i.translation_key == "missed" for i in ours)
    first_ids = set(auditor.data["repair_issues"])

    # A second report replaces the previous issues
    auditor.prev = copy.deepcopy(auditor.data)
    auditor.prev["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(seconds=5)).isoformat()
    auditor.prev["session"]["clean_shutdown"] = True
    auditor.started_at = dt_util.utcnow()
    await auditor._async_analyse_previous_downtime()
    await hass.async_block_till_done()
    remaining = {iid for (d, iid) in reg.issues if d == DOMAIN}
    assert not (first_ids & remaining)

    # Dismiss service clears everything
    resp = await hass.services.async_call(DOMAIN, "dismiss_repairs", {}, blocking=True, return_response=True)
    assert not [1 for (d, _) in reg.issues if d == DOMAIN]
    assert "dismissed" in resp

    # Websocket API used by the dashboard
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "downtime_auditor/report"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["window"]
    await client.send_json({"id": 2, "type": "downtime_auditor/history"})
    msg = await client.receive_json()
    # Newest first. A downtime only gets a saved report if something was found.
    assert msg["success"] and len(msg["result"]) == 2
    for item in msg["result"]:
        found = any(item["counts"].get(c) for c in ("interrupted", "missed", "possibly_missed", "fired_during_startup"))
        assert item["available"] == found == (item["file"] is not None)
    assert msg["result"][1]["available"]
    first_file = msg["result"][1]["file"]
    await client.send_json({"id": 3, "type": "downtime_auditor/report", "file": first_file})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["counts"]["missed"] >= 3
    await client.send_json({"id": 4, "type": "downtime_auditor/report", "file": "../../secrets.yaml"})
    msg = await client.receive_json()
    assert not msg["success"]
    await client.send_json({"id": 5, "type": "downtime_auditor/status"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["tracking"] is True
    await client.send_json({
        "id": 6, "type": "downtime_auditor/what_if",
        "start": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        "end": now.strftime("%Y-%m-%d %H:%M:%S"),
    })
    msg = await client.receive_json()
    assert msg["success"] and any(f["entity_id"] == "automation.morning" for f in msg["result"]["findings"])


async def test_sidebar_panel_registered(hass, enable_custom_integrations):
    # The frontend component needs the (large) home-assistant-frontend package.
    pytest.importorskip("hass_frontend")
    from homeassistant.components.frontend import DATA_PANELS

    await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "frontend", {})
    entry = MockConfigEntry(domain=DOMAIN, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    panel = hass.data[DATA_PANELS]["downtime-auditor"]
    assert panel.config["_panel_custom"]["name"] == "downtime-auditor-panel"
    assert panel.require_admin is True

    # Options toggle removes it
    hass.config_entries.async_update_entry(entry, options={**entry.options, "sidebar_panel": False})
    await hass.async_block_till_done()
    assert "downtime-auditor" not in hass.data[DATA_PANELS]


def test_retention_by_age_and_empty_reports(tmp_path):
    """Full reports only when something was found; prune reports and history by age."""
    from homeassistant.util import dt as dt_util

    from custom_components.downtime_auditor import report as rep

    base = tmp_path
    reports = base / "reports"
    reports.mkdir()
    now = dt_util.utcnow()

    def local_stamp(dt):
        return dt_util.as_local(dt).strftime("%Y%m%d-%H%M%S")

    # Pre-existing files: one 40 days old (expired), one 5 days old (kept)
    old = reports / f"report-{local_stamp(now - timedelta(days=40))}.json"
    recent = reports / f"report-{local_stamp(now - timedelta(days=5))}.json"
    old.write_text("{}")
    recent.write_text("{}")
    # History: one line 400 days old (dropped), one 10 days old (kept)
    hist = base / "history.jsonl"
    hist.write_text(
        json.dumps({"generated_at": (now - timedelta(days=400)).isoformat(), "file": None}) + "\n"
        + json.dumps({"generated_at": (now - timedelta(days=10)).isoformat(), "file": None}) + "\n"
    )

    def make(counts):
        return {
            "generated_at": now.isoformat(),
            "window": {"start": now.isoformat(), "end": now.isoformat()},
            "counts": counts,
            "actionable": sum(v for k, v in counts.items() if k != "unverifiable"),
            "findings": [],
        }

    # Clean restart with only unverifiable noise -> no report file
    assert rep._write_files(base, make({"unverifiable": 3}), 30, 365) is None
    # Restart with a missed trigger -> report file
    path = rep._write_files(base, make({"missed": 1}), 30, 365)
    assert path and Path(path).exists()

    names = {p.name for p in reports.glob("report-*.json")}
    assert old.name not in names and recent.name in names and Path(path).name in names
    lines = [json.loads(x) for x in hist.read_text().splitlines()]
    assert len(lines) == 3  # 10-day-old line + the two new ones; 400-day line dropped
    assert lines[1]["file"] is None and lines[2]["file"] == Path(path).name
    assert (base / "last_report.json").exists()


def test_report_file_cap(tmp_path, monkeypatch):
    """The hard cap still applies within the retention period."""
    from homeassistant.util import dt as dt_util

    from custom_components.downtime_auditor import report as rep

    monkeypatch.setattr(rep, "MAX_REPORT_FILES", 3)
    reports = tmp_path / "reports"
    reports.mkdir()
    now = dt_util.utcnow()
    for i in range(5):
        stamp = dt_util.as_local(now - timedelta(hours=i + 1)).strftime("%Y%m%d-%H%M%S")
        (reports / f"report-{stamp}.json").write_text("{}")
    rep._prune_reports(reports, 30, now)
    left = sorted(p.name for p in reports.glob("report-*.json"))
    assert len(left) == 3
    newest = dt_util.as_local(now - timedelta(hours=1)).strftime("%Y%m%d-%H%M%S")
    assert f"report-{newest}.json" in left
