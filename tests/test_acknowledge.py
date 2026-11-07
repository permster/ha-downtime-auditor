"""Acknowledging a finding: only for the report it's in (its Repairs issue clears), never a rating."""

from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.downtime_auditor.const import DOMAIN

from .test_reverted import _restart, _setup

OPTIONS = {"repairs_min_severity": "medium"}  # the reverted finding is Medium
EID = "input_boolean.off_peak"


def _issues(hass):
    return [i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN]


def _finding(auditor):
    (f,) = [f for f in auditor.last_report["findings"] if f["entity_id"] == EID]
    return f


async def test_acknowledge_clears_the_repair(hass, enable_custom_integrations):
    auditor = await _setup(hass, OPTIONS)
    await _restart(hass, auditor, saved={EID: "off"})
    assert len(_issues(hass)) == 1
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "on"

    assert await auditor.async_acknowledge("reverted", EID)
    await hass.async_block_till_done()
    assert _finding(auditor)["acknowledged"]
    assert _issues(hass) == []
    assert auditor.last_report["needs_attention"] == 0
    assert hass.states.get("binary_sensor.downtime_auditor_needs_attention").state == "off"
    # still counted: the counts say what happened
    assert auditor.last_report["counts"]["reverted"] == 1
    sensor = hass.states.get("sensor.downtime_auditor_reverted_states")
    assert sensor.state == "1" and sensor.attributes["findings"][0]["acknowledged"]
    assert auditor.data["last_report"]["acknowledged"]  # kept across a restart, until the next report

    # Re-rating (a label or option changed) doesn't bring the issue back.
    await auditor.async_rerate_last_report()
    assert _finding(auditor)["acknowledged"] and _issues(hass) == []

    # Undo.
    assert await auditor.async_acknowledge("reverted", EID, False)
    assert "acknowledged" not in _finding(auditor)
    assert len(_issues(hass)) == 1 and auditor.last_report["needs_attention"] == 1


async def test_only_findings_of_the_latest_report(hass, enable_custom_integrations):
    auditor = await _setup(hass, OPTIONS)
    await _restart(hass, auditor, saved={EID: "off"})
    assert not await auditor.async_acknowledge("missed", EID)  # no such finding
    assert not await auditor.async_acknowledge("reverted", EID, report_id="an older report")
    assert "acknowledged" not in _finding(auditor)


async def test_the_next_report_raises_it_again(hass, enable_custom_integrations):
    auditor = await _setup(hass, OPTIONS)
    await _restart(hass, auditor, saved={EID: "off"})
    await auditor.async_acknowledge("reverted", EID)
    assert _issues(hass) == []
    found = await _restart(hass, auditor, saved={EID: "off"})
    assert "acknowledged" not in found[EID] and not auditor.last_report.get("acknowledged")
    assert len(_issues(hass)) == 1


async def test_service(hass, enable_custom_integrations):
    auditor = await _setup(hass, OPTIONS)
    await _restart(hass, auditor, saved={EID: "off"})
    await hass.services.async_call(DOMAIN, "acknowledge", {"entity_id": EID, "type": "reverted"}, blocking=True)
    assert _finding(auditor)["acknowledged"] and _issues(hass) == []


async def test_repairs_fix_flow_acknowledges(hass, hass_client, enable_custom_integrations):
    auditor = await _setup(hass, OPTIONS)
    assert await async_setup_component(hass, "repairs", {})
    await _restart(hass, auditor, saved={EID: "off"})
    (issue,) = _issues(hass)
    assert issue.is_fixable
    client = await hass_client()
    resp = await client.post("/api/repairs/issues/fix", json={"handler": DOMAIN, "issue_id": issue.issue_id})
    assert resp.status == 200
    flow = await resp.json()
    assert flow["step_id"] == "confirm" and flow["description_placeholders"]["entity_id"] == EID
    resp = await client.post(f"/api/repairs/issues/fix/{flow['flow_id']}", json={})
    assert (await resp.json())["type"] == "create_entry"
    await hass.async_block_till_done()
    assert _finding(auditor)["acknowledged"] and _issues(hass) == []


async def test_websocket(hass, hass_ws_client, enable_custom_integrations):
    auditor = await _setup(hass, OPTIONS)
    await _restart(hass, auditor, saved={EID: "off"})
    ws = await hass_ws_client(hass)
    report = auditor.last_report["generated_at"]
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/acknowledge", "finding_type": "reverted", "entity_id": EID, "report": report})
    assert (await ws.receive_json())["success"]
    assert _finding(auditor)["acknowledged"]
    await ws.send_json({"id": 2, "type": f"{DOMAIN}/acknowledge", "finding_type": "reverted", "entity_id": EID, "report": "old"})
    msg = await ws.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "not_found"
