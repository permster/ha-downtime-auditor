"""Dashboard smoke test: render frontend/panel.js with data from a real backend run.

The fixture is generated here (never committed), so the dashboard is always
checked against the report format the backend currently produces. Needs Node.js;
skipped when it isn't installed.
"""

import copy
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er, label_registry as lr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.const import DOMAIN

from .test_severity_and_migration import V04_HISTORY

HARNESS = Path(__file__).parent / "frontend" / "panel_smoke.js"
PANEL = Path(__file__).parent.parent / "custom_components" / "downtime_auditor" / "frontend" / "panel.js"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
async def test_panel_renders_real_reports(hass, enable_custom_integrations, hass_ws_client, tmp_path):
    shutil.rmtree(hass.config.path("downtime_auditor"), ignore_errors=True)
    await async_setup_component(hass, "input_boolean", {"input_boolean": {"vac": {}}})
    t = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    autos = [
        # Conditions that "probably failed" (vac is off in the baseline; the time part fails exactly)
        {"id": "a", "alias": "Morning", "triggers": [{"trigger": "time", "at": t}],
         "conditions": [{"or": [{"condition": "state", "entity_id": "input_boolean.vac", "state": "on"},
                                {"condition": "time", "after": "23:00"}]}], "actions": []},
        # Unknown confidence, capped at Low
        {"id": "b", "alias": "Evt", "triggers": [{"trigger": "event", "event_type": "x"}], "actions": []},
        # Rated High by label
        {"id": "c", "alias": "Pattern", "triggers": [{"trigger": "time_pattern", "minutes": "/30"}], "actions": []},
    ]
    await async_setup_component(hass, "automation", {"automation": autos})
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    high = lr.async_get(hass).async_get_label_by_name("downtime_auditor_sev: high").label_id
    er.async_get(hass).async_update_entity("automation.pattern", labels={high})

    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    report = await auditor._async_analyse_previous_downtime()
    whatif = await auditor.async_analyze_window(dt_util.utcnow() - timedelta(hours=3), dt_util.utcnow(), False)

    # Add a v0.4 history line so the History tab shows a legacy entry too.
    hist = Path(hass.config.path("downtime_auditor")) / "history.jsonl"
    hist.write_text(json.dumps(V04_HISTORY) + "\n" + hist.read_text(encoding="utf-8"), encoding="utf-8")
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "downtime_auditor/history"})
    history = (await client.receive_json())["result"]
    await client.send_json({"id": 2, "type": "downtime_auditor/status"})
    status = (await client.receive_json())["result"]

    # What the harness relies on
    by = {f["name"]: f for f in report["findings"]}
    assert (by["Pattern"]["severity"], by["Morning"]["details"]["conditions"].get("likely")) == ("high", "fail")
    assert by["Evt"]["confidence"] == "unknown"
    assert [h.get("legacy", False) for h in history] == [False, True]

    fixture = tmp_path / "panel_fixture.json"
    fixture.write_text(
        json.dumps({"report": report, "whatif": whatif, "history": history, "status": status}, default=str),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, str(HARNESS), str(fixture), str(PANEL)], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PANEL SMOKE OK" in proc.stdout
