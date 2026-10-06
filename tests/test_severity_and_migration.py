"""v0.5: severity rules, thresholds, config entry migration and v0.4 history upgrade."""

import copy
from datetime import timedelta
import json
import shutil

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.const import DOMAIN, Severity
from custom_components.downtime_auditor.report import upgrade_legacy
from custom_components.downtime_auditor.severity import at_least, effective_severity, highest

# ---------------------------------------------------------------- effective severity


@pytest.mark.parametrize(
    ("base", "ftype", "confidence", "conditions", "expected"),
    [
        # Critical is never capped; only failed conditions bring it down.
        ("critical", "fired_at_startup", "confirmed", None, "critical"),
        ("critical", "missed", "unknown", None, "critical"),
        ("critical", "missed", "confirmed", "fail", "none"),
        # Everything else is capped at Low for startup firing or Unknown confidence.
        ("high", "missed", "unknown", None, "low"),
        ("medium", "fired_at_startup", "confirmed", None, "low"),
        ("low", "missed", "unknown", None, "low"),
        ("none", "missed", "unknown", None, "none"),
        ("none", "fired_at_startup", "confirmed", None, "none"),
        # Otherwise the base stands, whatever the confidence.
        ("high", "missed", "possible", None, "high"),
        ("medium", "interrupted", "probable", None, "medium"),
        ("high", "missed", "confirmed", "pass", "high"),
        ("high", "missed", "confirmed", "unknown", "high"),
        # Failed conditions win over everything.
        ("high", "fired_at_startup", "unknown", "fail", "none"),
    ],
)
def test_effective_severity_matrix(base, ftype, confidence, conditions, expected):
    sev, reason = effective_severity(Severity(base), "label", ftype, confidence, conditions)
    assert sev == expected
    assert reason


def test_severity_reasons_and_helpers():
    assert effective_severity(Severity.MEDIUM, "default", "missed", "confirmed", None)[1] == (
        "default for unrated automations"
    )
    assert effective_severity(Severity.HIGH, "label", "missed", "confirmed", None)[1] == "set by label"
    assert "capped at Low" in effective_severity(Severity.HIGH, "label", "missed", "unknown", None)[1]
    assert at_least("high", "high") and at_least("critical", "high") and not at_least("medium", "high")
    assert highest(["low", "critical", "medium"]) == "critical"
    assert highest([]) is None


# ---------------------------------------------------------------- v0.4 history / report upgrade

V04_HISTORY = {
    "generated_at": "2026-09-01T10:00:00+00:00",
    "window": {"start": "x", "end": "y", "clean_shutdown": True},
    "counts": {
        "interrupted": 1,
        "missed": 2,
        "possibly_missed": 3,
        "unverifiable": 4,
        "fired_during_startup": 5,
        "skipped": 6,
    },
    "actionable": 6,
    "file": "report-20260901-030000.json",
}


def _v04_report(clean=True):
    return {
        "schema": 1,
        "generated_at": "2026-09-01T10:00:00+00:00",
        "window": {"start": "x", "end": "y", "clean_shutdown": clean},
        "counts": {"missed": 1, "possibly_missed": 1, "unverifiable": 1, "fired_during_startup": 1, "skipped": 1},
        "actionable": 2,
        "meta": {},
        "findings": [
            {"category": "missed", "confidence": "high", "entity_id": "automation.a", "name": "A", "summary": "s"},
            {"category": "possibly_missed", "confidence": "medium", "entity_id": "automation.b", "name": "B", "summary": "s"},
            {"category": "unverifiable", "confidence": "low", "entity_id": "automation.c", "name": "C", "summary": "s"},
            {"category": "fired_during_startup", "confidence": "low", "entity_id": "automation.d", "name": "D", "summary": "s"},
            {"category": "skipped", "confidence": "high", "entity_id": "automation.e", "name": "E", "summary": "s"},
        ],
    }


def test_upgrade_legacy_history_line():
    line = upgrade_legacy(copy.deepcopy(V04_HISTORY))
    assert line["counts"] == {"interrupted": 1, "missed": 9, "fired_at_startup": 5}
    assert line["skipped"] == 6
    assert line["actionable"] == 6  # kept with its old meaning
    assert line["needs_attention"] is None
    assert line["counts_by_severity"] is None and line["highest_severity"] is None
    assert line["legacy"] is True and line["schema"] == 2
    assert line["file"] == V04_HISTORY["file"]
    assert upgrade_legacy(copy.deepcopy(line)) == line  # idempotent


@pytest.mark.parametrize(("clean", "first_confidence"), [(True, "confirmed"), (False, "probable")])
def test_upgrade_legacy_report_file(clean, first_confidence):
    rep = upgrade_legacy(_v04_report(clean))
    by = {f["entity_id"]: f for f in rep["findings"]}
    assert "automation.e" not in by  # skipped findings are dropped ...
    assert rep["skipped"] == 1  # ... and counted
    assert all("category" not in f for f in rep["findings"])
    assert (by["automation.a"]["type"], by["automation.a"]["confidence"]) == ("missed", first_confidence)
    assert (by["automation.b"]["type"], by["automation.b"]["confidence"]) == ("missed", "probable")
    assert (by["automation.c"]["type"], by["automation.c"]["confidence"]) == ("missed", "unknown")
    assert (by["automation.d"]["type"], by["automation.d"]["confidence"]) == ("fired_at_startup", "confirmed")
    assert by["automation.a"]["severity"] == "medium"
    assert by["automation.c"]["severity"] == by["automation.d"]["severity"] == "low"  # capped
    assert all(f["severity_base"] == "medium" for f in rep["findings"])
    assert all(f["severity_reason"] == "Recorded before severity ratings existed" for f in rep["findings"])
    assert rep["counts"] == {"interrupted": 0, "missed": 3, "fired_at_startup": 1}
    assert rep["counts_by_severity"]["medium"] == 2 and rep["counts_by_severity"]["low"] == 2
    assert rep["highest_severity"] == "medium"
    assert rep["needs_attention"] is None and rep["actionable"] == 2
    assert rep["legacy"] is True
    assert upgrade_legacy(copy.deepcopy(rep)) == rep


def test_upgrade_legacy_passes_v05_through():
    new = {"schema": 2, "counts": {"missed": 1}, "findings": []}
    assert upgrade_legacy(new) is new
    assert upgrade_legacy(None) is None


async def test_history_tab_reads_v04_files(hass, enable_custom_integrations, hass_ws_client):
    base = hass.config.path("downtime_auditor")
    shutil.rmtree(base, ignore_errors=True)
    from pathlib import Path

    (Path(base) / "reports").mkdir(parents=True)
    hist = Path(base) / "history.jsonl"
    hist.write_text(json.dumps(V04_HISTORY) + "\n", encoding="utf-8")
    report_file = Path(base) / "reports" / V04_HISTORY["file"]
    report_file.write_text(json.dumps(_v04_report()), encoding="utf-8")
    before = (hist.read_text(), report_file.read_text())

    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "downtime_auditor/history"})
    msg = await client.receive_json()
    assert msg["success"]
    (item,) = msg["result"]
    assert item["counts"] == {"interrupted": 1, "missed": 9, "fired_at_startup": 5}
    assert item["legacy"] is True and item["available"] is True

    await client.send_json({"id": 2, "type": "downtime_auditor/report", "file": item["file"]})
    msg = await client.receive_json()
    assert msg["success"]
    assert {f["type"] for f in msg["result"]["findings"]} == {"missed", "fired_at_startup"}

    assert (hist.read_text(), report_file.read_text()) == before  # files on disk untouched


async def test_last_report_from_v04_store_is_upgraded(hass, enable_custom_integrations, hass_storage):
    from custom_components.downtime_auditor.const import STORAGE_KEY

    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"session": None, "last_report": _v04_report()},
    }
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN].last_report["legacy"] is True
    assert hass.states.get("sensor.downtime_auditor_missed_triggers").state == "3"
    assert hass.states.get("sensor.downtime_auditor_fired_at_startup").state == "1"
    assert hass.states.get("sensor.downtime_auditor_highest_severity").state == "medium"


# ---------------------------------------------------------------- config entry migration


async def _migrate(hass, fired_object_id):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        options={
            "startup_delay": 0,
            "write_json": False,
            "repairs_possible": True,
            "include_unverifiable": False,
            "retention": 100,
            "report_retention_days": 30,
        },
        data={"retention": 100},
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    ids = {}
    for key, object_id in (
        ("possibly_missed", "downtime_auditor_possibly_missed_triggers"),
        ("unverifiable", "downtime_auditor_unverifiable_triggers"),
        ("fired_during_startup", fired_object_id),
        ("missed", "downtime_auditor_missed_triggers"),
    ):
        ids[key] = reg.async_get_or_create(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}", suggested_object_id=object_id, config_entry=entry
        ).entity_id
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, reg, ids


async def test_migrate_v1_entry(hass, enable_custom_integrations):
    entry, reg, ids = await _migrate(hass, "downtime_auditor_fired_during_startup")
    assert entry.version == 2
    assert entry.options == {
        "startup_delay": 0,
        "write_json": False,
        "report_retention_days": 30,
        # Upgraded installs keep v0.4 behaviour: unrated (Medium) findings still raise Repairs/push.
        "repairs_min_severity": "medium",
        "push_min_severity": "medium",
    }
    assert entry.data == {}
    assert reg.async_get(ids["possibly_missed"]) is None
    assert reg.async_get(ids["unverifiable"]) is None
    assert reg.async_get("sensor.downtime_auditor_fired_during_startup") is None
    moved = reg.async_get("sensor.downtime_auditor_fired_at_startup")
    assert moved and moved.unique_id == f"{entry.entry_id}_fired_at_startup"
    assert reg.async_get(ids["missed"]).unique_id == f"{entry.entry_id}_missed"  # untouched
    assert hass.states.get("sensor.downtime_auditor_fired_at_startup") is not None


async def test_migrate_keeps_customised_entity_id(hass, enable_custom_integrations):
    entry, reg, _ids = await _migrate(hass, "my_startup_sensor")
    kept = reg.async_get("sensor.my_startup_sensor")
    assert kept and kept.unique_id == f"{entry.entry_id}_fired_at_startup"
    assert reg.async_get("sensor.downtime_auditor_fired_at_startup") is None


async def test_newer_entry_version_is_refused(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain=DOMAIN, version=3, options={"startup_delay": 0})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)


# ---------------------------------------------------------------- default thresholds


async def test_default_thresholds_skip_medium(hass, enable_custom_integrations):
    """With defaults (High), unrated (Medium) findings raise no Repairs and no push."""
    await async_setup_component(hass, "input_boolean", {"input_boolean": {"flag": {}}})
    t_missed = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    await async_setup_component(
        hass,
        "automation",
        {"automation": [{"id": "m", "alias": "Morning", "triggers": [{"trigger": "time", "at": t_missed}], "actions": []}]},
    )
    await hass.async_block_till_done()
    push = async_mock_service(hass, "notify", "test_push")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        options={"startup_delay": 0, "write_json": False, "notify_service": "notify.test_push"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    rep = await auditor._async_analyse_previous_downtime()
    await hass.async_block_till_done()

    assert [f["severity"] for f in rep["findings"]] == ["medium"]
    assert rep["needs_attention"] == 0
    assert not [1 for (d, _) in ir.async_get(hass).issues if d == DOMAIN]
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "off"
    assert hass.states.get("sensor.downtime_auditor_missed_triggers").state == "1"
    assert not push

    # Lowering the Repairs threshold turns needs_attention on for the same report.
    hass.config_entries.async_update_entry(entry, options={**entry.options, "repairs_min_severity": "medium"})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "on"


async def test_fired_at_startup_is_low(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 7, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    auditor.startup_triggered = {"automation.x": [{"time": "t", "source": "state of binary_sensor.y", "name": "X"}]}
    (f,) = auditor._startup_fired_findings()
    auditor._rate([f])
    assert (f.type, f.confidence, f.severity) == ("fired_at_startup", "confirmed", "low")
    assert "fired at startup" in f.severity_reason


# ---------------------------------------------------------------- the whole v0.4 → v0.5 upgrade


async def test_upgrade_from_v04_end_to_end(hass, enable_custom_integrations, hass_storage, hass_ws_client):
    """Boot v0.5 on top of a v0.4 install: entry, entities, store, Repairs and files."""
    from pathlib import Path

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from homeassistant.helpers import label_registry as lr

    from custom_components.downtime_auditor.const import STORAGE_KEY

    hass.set_state(CoreState.not_running)
    base = Path(hass.config.path("downtime_auditor"))
    shutil.rmtree(base, ignore_errors=True)
    (base / "reports").mkdir(parents=True)
    (base / "history.jsonl").write_text(json.dumps(V04_HISTORY) + "\n", encoding="utf-8")
    (base / "reports" / V04_HISTORY["file"]).write_text(json.dumps(_v04_report()), encoding="utf-8")

    t_missed = (dt_util.now() - timedelta(minutes=30)).strftime("%H:%M:%S")
    await async_setup_component(
        hass,
        "automation",
        {"automation": [{"id": "m", "alias": "Morning", "triggers": [{"trigger": "time", "at": t_missed}], "actions": []}]},
    )
    push = async_mock_service(hass, "notify", "test_push")

    # v0.4 config entry + its entities
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        options={
            "startup_delay": 0,
            "write_json": True,
            "notify_service": "notify.test_push",
            "repairs_possible": True,
            "include_unverifiable": True,
        },
    )
    entry.add_to_hass(hass)
    reg = er.async_get(hass)
    for key, object_id in (
        ("possibly_missed", "downtime_auditor_possibly_missed_triggers"),
        ("unverifiable", "downtime_auditor_unverifiable_triggers"),
        ("fired_during_startup", "downtime_auditor_fired_during_startup"),
    ):
        reg.async_get_or_create("sensor", DOMAIN, f"{entry.entry_id}_{key}", suggested_object_id=object_id, config_entry=entry)

    # v0.4 store: clean shutdown 2h ago, v0.4 last report, one open v0.4 Repairs issue
    old_issue = "possibly_missed_automation_x_abc123"
    ir.async_create_issue(
        hass, DOMAIN, old_issue, is_fixable=False, is_persistent=True,
        severity=ir.IssueSeverity.WARNING, translation_key="possibly_missed",
    )
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "session": {
                "id": "v04",
                "setup_at": (dt_util.utcnow() - timedelta(days=1)).isoformat(),
                "last_heartbeat": (dt_util.utcnow() - timedelta(hours=2)).isoformat(),
                "clean_shutdown": True,
                "shutdown_at": (dt_util.utcnow() - timedelta(hours=2)).isoformat(),
                "ha_version": "2026.1.0",
            },
            "baseline": {"captured_at": "x", "entities": {}, "templates": {}, "automations": {}},
            "running": None,
            "last_report": _v04_report(),
            "repair_issues": [old_issue],
        },
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]

    # Before the analysis: migrated entry, the old report reads in new terms, labels exist,
    # and the previous session is still stored for the analysis.
    assert entry.version == 2
    assert "repairs_possible" not in entry.options and "include_unverifiable" not in entry.options
    assert entry.options["repairs_min_severity"] == entry.options["push_min_severity"] == "medium"
    assert reg.async_get("sensor.downtime_auditor_possibly_missed_triggers") is None
    assert reg.async_get("sensor.downtime_auditor_fired_at_startup") is not None
    assert hass.states.get("sensor.downtime_auditor_highest_severity").state == "medium"
    assert {f"downtime_auditor_sev: {s}" for s in ("critical", "high", "medium", "low", "none")} <= {
        label.name for label in lr.async_get(hass).async_list_labels()
    }
    assert hass_storage[STORAGE_KEY]["data"]["session"]["id"] == "v04"

    # HA finishes starting → first v0.5 report
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()

    rep = auditor.last_report
    assert rep["schema"] == 2 and "legacy" not in rep
    assert [f["entity_id"] for f in rep["findings"]] == ["automation.morning"]
    issues = {iid: i for (d, iid), i in ir.async_get(hass).issues.items() if d == DOMAIN}
    assert old_issue not in issues  # the v0.4 issue is cleared by the first report
    assert [i.translation_key for i in issues.values()] == ["missed"]  # Medium ≥ migrated threshold
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "on"
    assert len(push) == 1
    stored = hass_storage[STORAGE_KEY]["data"]
    assert stored["labels_created"] is True and stored["session"]["id"] != "v04"

    # History: the new line plus the v0.4 line, both in v0.5 terms
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "downtime_auditor/history"})
    items = (await client.receive_json())["result"]
    assert [i.get("legacy", False) for i in items] == [False, True]
    assert all(set(i["counts"]) == {"interrupted", "missed", "fired_at_startup"} for i in items)
