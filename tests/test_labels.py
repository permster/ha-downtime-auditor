"""Severity labels: created once, read per automation, set from the dashboard."""

import copy
from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er, label_registry as lr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.const import DOMAIN, STORAGE_KEY
from custom_components.downtime_auditor.labels import async_create_labels

SEV_NAMES = {f"downtime_auditor_sev: {s}" for s in ("critical", "high", "medium", "low", "none")}


def test_label_names_match_case_and_spacing():
    from custom_components.downtime_auditor.labels import _severity_of

    assert _severity_of("DOWNTIME_AUDITOR_SEV:HIGH") == "high"
    assert _severity_of("downtime_auditor_sev:   critical") == "critical"
    assert _severity_of("Downtime_Auditor_Sev: None") == "none"
    assert _severity_of("downtime_auditor_sev: urgent") is None
    assert _severity_of("my downtime_auditor_sev: high") is None


def _names(hass):
    return {label.name for label in lr.async_get(hass).async_list_labels()}


def _label_id(hass, sev):
    async_create_labels(hass)  # labels exist only once something is rated (or the service ran)
    return lr.async_get(hass).async_get_label_by_name(f"downtime_auditor_sev: {sev}").label_id


async def _setup(hass, t_missed):
    autos = [
        {"id": "a", "alias": "Morning", "triggers": [{"trigger": "time", "at": t_missed}], "actions": []},
        {"id": "b", "alias": "Evt", "triggers": [{"trigger": "event", "event_type": "foo"}], "actions": []},
    ]
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN]


async def _analyze(hass, auditor):
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    rep = await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    return {f["entity_id"]: f for f in rep["findings"]}


async def test_labels_created_on_demand(hass, enable_custom_integrations, hass_ws_client):
    """No labels until something is rated (unused labels are flagged by tools like Spook)."""
    entry, auditor = await _setup(hass, "03:00:00")
    assert not SEV_NAMES & _names(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not SEV_NAMES & _names(hass)

    # Rating from the dashboard creates just that label.
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "downtime_auditor/set_severity",
                            "entity_id": "automation.morning", "severity": "high"})
    assert (await client.receive_json())["success"]
    assert SEV_NAMES & _names(hass) == {"downtime_auditor_sev: high"}
    label = lr.async_get(hass).async_get_label_by_name("downtime_auditor_sev: high")
    assert (label.color, label.icon) == ("orange", "mdi:timeline-check-outline")

    # The service creates only what is missing.
    resp = await hass.services.async_call(
        DOMAIN, "create_severity_labels", {}, blocking=True, return_response=True
    )
    assert sorted(resp["created"]) == sorted(SEV_NAMES - {"downtime_auditor_sev: high"})
    assert SEV_NAMES <= _names(hass)
    resp = await hass.services.async_call(
        DOMAIN, "create_severity_labels", {}, blocking=True, return_response=True
    )
    assert resp == {"created": []}


async def test_first_start_keeps_previous_session(hass, enable_custom_integrations, hass_storage):
    """Setup must not overwrite the session the startup analysis needs."""
    from homeassistant.core import CoreState

    hass.set_state(CoreState.not_running)
    prev = {"session": {"id": "prev", "last_heartbeat": dt_util.utcnow().isoformat(), "clean_shutdown": False}}
    hass_storage[STORAGE_KEY] = {"version": 1, "minor_version": 1, "key": STORAGE_KEY, "data": prev}
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[STORAGE_KEY]["data"]
    assert stored["session"]["id"] == "prev"
    assert not SEV_NAMES & _names(hass)


async def test_labels_drive_severity(hass, enable_custom_integrations):
    t_missed = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    _entry, auditor = await _setup(hass, t_missed)
    ents = er.async_get(hass)
    # Several labels: the highest wins.
    ents.async_update_entity(
        "automation.morning", labels={_label_id(hass, "low"), _label_id(hass, "high")}
    )
    ents.async_update_entity("automation.evt", labels={_label_id(hass, "critical")})

    by = await _analyze(hass, auditor)
    morning = by["automation.morning"]
    assert (morning["severity"], morning["severity_source"], morning["severity_reason"]) == (
        "high", "label", "set by label",
    )
    evt = by["automation.evt"]
    assert (evt["confidence"], evt["severity"]) == ("unknown", "critical")  # Critical is never capped
    assert morning["severity_base"] == "high" and evt["severity_base"] == "critical"
    assert hass.states.get("sensor.downtime_auditor_highest_severity").state == "critical"
    # Default Repairs threshold is High: both raise an issue now.
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "on"


async def test_set_severity_websocket(hass, enable_custom_integrations, hass_ws_client):
    t_missed = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    _entry, auditor = await _setup(hass, t_missed)
    ents = er.async_get(hass)
    keep = lr.async_get(hass).async_create("Kitchen").label_id
    ents.async_update_entity("automation.morning", labels={keep, _label_id(hass, "low")})
    by = await _analyze(hass, auditor)
    assert by["automation.morning"]["severity"] == "low"

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "downtime_auditor/set_severity", "entity_id": "automation.morning", "severity": "critical"})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert ents.async_get("automation.morning").labels == {keep, _label_id(hass, "critical")}
    # The current report is re-rated straight away.
    (f,) = [f for f in auditor.last_report["findings"] if f["entity_id"] == "automation.morning"]
    assert (f["severity"], f["severity_source"]) == ("critical", "label")
    assert auditor.last_report["highest_severity"] == "critical"
    assert hass.states.get("sensor.downtime_auditor_highest_severity").state == "critical"

    # null = back to unrated (Medium); unrelated labels stay.
    await client.send_json({"id": 2, "type": "downtime_auditor/set_severity", "entity_id": "automation.morning", "severity": None})
    msg = await client.receive_json()
    assert msg["success"]
    assert ents.async_get("automation.morning").labels == {keep}
    (f,) = [f for f in auditor.last_report["findings"] if f["entity_id"] == "automation.morning"]
    assert (f["severity"], f["severity_source"]) == ("medium", "default")
    assert f["severity_base"] == "medium"

    # Choosing a severity whose label was deleted recreates just that label.
    lr.async_get(hass).async_delete(_label_id(hass, "high"))
    await client.send_json({"id": 3, "type": "downtime_auditor/set_severity", "entity_id": "automation.morning", "severity": "high"})
    assert (await client.receive_json())["success"]
    assert ents.async_get("automation.morning").labels == {keep, _label_id(hass, "high")}

    # Only automations and scripts can be rated.
    await client.send_json({"id": 4, "type": "downtime_auditor/set_severity", "entity_id": "light.x", "severity": "high"})
    assert not (await client.receive_json())["success"]
    await client.send_json({"id": 5, "type": "downtime_auditor/set_severity", "entity_id": "automation.nope", "severity": "high"})
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_found"

    lr.async_get(hass).async_delete(_label_id(hass, "none"))
    await client.send_json({"id": 6, "type": "downtime_auditor/create_severity_labels"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"] == {"created": ["downtime_auditor_sev: none"]}


async def test_label_changed_in_ha_rerates_report(hass, enable_custom_integrations):
    """Rating in HA's own label editor (not the dashboard) updates the report and Repairs."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.helpers import issue_registry as ir

    t_missed = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    _entry, auditor = await _setup(hass, t_missed)
    ents = er.async_get(hass)
    ents.async_update_entity("automation.morning", labels={_label_id(hass, "high")})
    await hass.async_block_till_done()
    by = await _analyze(hass, auditor)
    assert by["automation.morning"]["severity"] == "high"
    ours = lambda: [i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN]  # noqa: E731
    assert len(ours()) == 1  # High ≥ the default Repairs threshold

    # Like HA's label editor: change the entity's labels directly.
    ents.async_update_entity("automation.morning", labels={_label_id(hass, "none")})
    ents.async_update_entity("automation.morning", labels={_label_id(hass, "none")})  # burst → one re-rate
    await hass.async_block_till_done()
    assert auditor.last_report["highest_severity"] == "high"  # debounced, not yet
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    (f,) = [f for f in auditor.last_report["findings"] if f["entity_id"] == "automation.morning"]
    assert (f["severity"], f["severity_source"]) == ("none", "label")
    assert ours() == []  # the Repair is gone
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "off"

    # Renaming a label so it no longer names a severity also re-rates (back to the default).
    lr.async_get(hass).async_update(_label_id(hass, "none"), name="Not a rating")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    (f,) = [f for f in auditor.last_report["findings"] if f["entity_id"] == "automation.morning"]
    assert (f["severity"], f["severity_source"]) == ("medium", "default")


async def test_status_reports_pending_analysis_and_version(hass, enable_custom_integrations, hass_ws_client, hass_storage):
    """The dashboard can say 'checking what was missed in N s' after a restart."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.core import CoreState

    hass.set_state(CoreState.not_running)
    hass_storage[STORAGE_KEY] = {
        "version": 1, "minor_version": 1, "key": STORAGE_KEY,
        "data": {"session": {"id": "prev", "clean_shutdown": True,
                             "shutdown_at": (dt_util.utcnow() - timedelta(minutes=5)).isoformat()},
                 "labels_created": True},
    }
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 30, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    async def status():
        await client.send_json_auto_id({"type": "downtime_auditor/status"})
        return (await client.receive_json())["result"]

    st = await status()
    assert st["pending"] == {"state": "starting", "due_at": None, "startup_delay": 30}
    assert st["version"]  # the integration version, for the dashboard's reload check

    from .common import fake_boot

    await fake_boot(hass)  # start -> startup jobs -> running -> started, as a real boot
    st = await status()
    assert st["pending"]["state"] == "settling"
    due = dt_util.parse_datetime(st["pending"]["due_at"])
    assert 25 <= (due - dt_util.utcnow()).total_seconds() <= 31

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    st = await status()
    assert st["pending"] is None and st["tracking"] is True
