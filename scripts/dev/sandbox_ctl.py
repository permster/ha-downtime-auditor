"""Drive the throwaway sandbox HA (run by scripts/dev/sandbox.sh, inside WSL).

Commands:
  automations          write demo automations.yaml (times relative to now)
  onboard              create the owner account through HA's onboarding API
  install              add Downtime Auditor through its config flow
  rate                 give the demo automations severity labels
  prepare              set pre-outage states and start long-running runs
  backdate H [--unclean]   (HA stopped) pretend the last downtime began H hours ago
  offline-changes      (HA stopped) change states "while HA was down"
  wait-report SINCE    wait for a report generated after SINCE (ISO time)
  call DOMAIN.SERVICE [JSON]   call a service
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys

import aiohttp

SANDBOX = Path(os.environ.get("DA_SANDBOX", Path.home() / "da-sandbox"))
CONFIG = SANDBOX / "config"
PORT = int(os.environ.get("DA_SANDBOX_PORT", "8124"))
BASE = f"http://127.0.0.1:{PORT}"
CLIENT_ID = f"http://localhost:{PORT}/"  # also what the browser uses
AUTH_FILE = SANDBOX / "auth.json"
USER, PASSWORD = "demo", "demo-sandbox"

# entity_id → severity label for the demo
RATINGS = {
    "automation.water_the_garden": "high",
    "automation.garage_left_open_alert": "critical",
    "automation.freezer_too_warm": "critical",
    "automation.wake_up_routine": "high",
    "automation.doorbell_notification": "low",
    "automation.house_check": "high",
    "automation.sensor_sync": "low",
    "script.bedtime_lights": "medium",
}


def _hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def cmd_automations() -> None:
    """Demo automations; time triggers fall inside the outage that `demo` simulates."""
    now = datetime.now(tz=_ha_tz())  # HA's time zone, not WSL's
    garden, alarm = now - timedelta(minutes=80), now - timedelta(minutes=50)
    autos = [
        {"id": "water_garden", "alias": "Water the garden",
         "triggers": [{"trigger": "time", "at": _hhmm(garden)}], "actions": []},
        {"id": "heating_schedule", "alias": "Heating schedule",
         "triggers": [{"trigger": "time_pattern", "minutes": "/30"}],
         "conditions": [{"condition": "state", "entity_id": "input_boolean.vacation_mode", "state": "off"}],
         "actions": []},
        {"id": "night_alarm", "alias": "Night alarm arm",
         "triggers": [{"trigger": "time", "at": _hhmm(alarm)}],
         "conditions": [{"condition": "state", "entity_id": "input_boolean.vacation_mode", "state": "on"}],
         "actions": []},
        # Its time window never contains its trigger time: an exact fail → severity None.
        {"id": "pool_pump", "alias": "Pool pump (off-peak only)",
         "triggers": [{"trigger": "time", "at": _hhmm(garden)}],
         "conditions": [{"condition": "time", "after": _hhmm(garden + timedelta(hours=2)),
                         "before": _hhmm(garden + timedelta(hours=6))}],
         "actions": []},
        {"id": "garage_alert", "alias": "Garage left open alert",
         "triggers": [{"trigger": "state", "entity_id": "input_boolean.garage_door", "to": "on"}], "actions": []},
        {"id": "freezer_warm", "alias": "Freezer too warm",
         "triggers": [{"trigger": "numeric_state", "entity_id": "input_number.freezer_temp", "above": -10}],
         "actions": []},
        # Several triggers in one automation: one finding (and one Repair) listing all three.
        {"id": "house_check", "alias": "House check",
         "triggers": [{"trigger": "time", "at": _hhmm(alarm)},
                      {"trigger": "state", "entity_id": ["input_boolean.garage_door", "input_number.freezer_temp"]},
                      {"trigger": "event", "event_type": "house_check"}],
         "actions": []},
        # Sun trigger: HA 2026.9+ stores its fields under `options` (see the what-if over 24 h).
        {"id": "porch_sunset", "alias": "Porch light at sunset",
         "triggers": [{"trigger": "sun", "event": "sunset", "offset": "-00:15:00"}], "actions": []},
        # A "slow integration": sensor.pool_pump only exists while ctl sets it through the REST
        # API, so after a restart it's missing until `ctl late-report` brings it back.
        {"id": "pool_on", "alias": "Pool pump started",
         "triggers": [{"trigger": "state", "entity_id": "sensor.pool_pump", "to": "on"}], "actions": []},
        {"id": "pool_from_off", "alias": "Pool pump off to on",
         "triggers": [{"trigger": "state", "entity_id": "sensor.pool_pump", "from": "off", "to": "on"}], "actions": []},
        {"id": "doorbell", "alias": "Doorbell notification",
         "triggers": [{"trigger": "event", "event_type": "doorbell_pressed"}], "actions": []},
        {"id": "wake_up", "alias": "Wake-up routine", "mode": "parallel",
         "triggers": [{"trigger": "event", "event_type": "start_wakeup"}],
         "actions": [{"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.guest_mode"}},
                     {"delay": "00:20:00"},
                     {"action": "input_boolean.turn_off", "target": {"entity_id": "input_boolean.guest_mode"}}]},
        {"id": "sensor_sync", "alias": "Sensor sync",
         "triggers": [{"trigger": "time_pattern", "seconds": "/15"}], "actions": []},
        {"id": "hallway_motion", "alias": "Hallway motion light", "initial_state": False,
         "triggers": [{"trigger": "time", "at": _hhmm(garden)}], "actions": []},
    ]
    import yaml  # noqa: PLC0415  (bundled with HA)

    (CONFIG / "automations.yaml").write_text(yaml.safe_dump(autos, sort_keys=False), encoding="utf-8")
    print(f"wrote {len(autos)} automations (garden at {_hhmm(garden)}, alarm at {_hhmm(alarm)})")


def _ha_tz():
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    class _L(yaml.SafeLoader):
        pass

    _L.add_constructor("!include", lambda loader, node: None)
    cfg = yaml.load((CONFIG / "configuration.yaml").read_text(encoding="utf-8"), Loader=_L)
    return ZoneInfo(cfg["homeassistant"]["time_zone"])


# ---------------------------------------------------------------- auth


async def _token(session: aiohttp.ClientSession) -> str:
    auth = json.loads(AUTH_FILE.read_text())
    if auth.get("expires_at", 0) > datetime.now(UTC).timestamp() + 60:
        return auth["access_token"]
    async with session.post(
        f"{BASE}/auth/token",
        data={"grant_type": "refresh_token", "refresh_token": auth["refresh_token"], "client_id": CLIENT_ID},
    ) as r:
        r.raise_for_status()
        tok = await r.json()
    auth.update(access_token=tok["access_token"], expires_at=datetime.now(UTC).timestamp() + tok["expires_in"])
    AUTH_FILE.write_text(json.dumps(auth, indent=2))
    return auth["access_token"]


async def _hdr(session) -> dict:
    return {"Authorization": f"Bearer {await _token(session)}"}


async def cmd_onboard() -> None:
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE}/api/onboarding/users", json={
            "client_id": CLIENT_ID, "name": "Demo", "username": USER, "password": PASSWORD, "language": "en",
        }) as r:
            r.raise_for_status()
            code = (await r.json())["auth_code"]
        async with s.post(f"{BASE}/auth/token", data={
            "grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID,
        }) as r:
            r.raise_for_status()
            tok = await r.json()
        AUTH_FILE.write_text(json.dumps({
            "hass_url": f"http://localhost:{PORT}", "client_id": CLIENT_ID,
            "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
            "expires_at": datetime.now(UTC).timestamp() + tok["expires_in"],
        }, indent=2))
        hdr = await _hdr(s)
        for step, body in (
            ("core_config", {}),
            ("analytics", {}),
            ("integration", {"client_id": CLIENT_ID, "redirect_uri": f"{CLIENT_ID}?auth_callback=1"}),
        ):
            async with s.post(f"{BASE}/api/onboarding/{step}", json=body, headers=hdr) as r:
                if r.status not in (200, 403):  # 403 = step already done
                    raise RuntimeError(f"onboarding {step}: {r.status} {await r.text()}")
    print(f"onboarded; login {USER} / {PASSWORD}")


async def cmd_install() -> None:
    async with aiohttp.ClientSession() as s:
        hdr = await _hdr(s)
        async with s.post(f"{BASE}/api/config/config_entries/flow", json={"handler": "downtime_auditor"}, headers=hdr) as r:
            r.raise_for_status()
            flow = await r.json()
        if flow.get("type") == "abort":
            print("already installed")
            return
        async with s.post(f"{BASE}/api/config/config_entries/flow/{flow['flow_id']}", json={
            "startup_delay": 20, "heartbeat_interval": 30, "repairs_min_severity": "high", "push_min_severity": "high",
        }, headers=hdr) as r:
            r.raise_for_status()
            print("installed:", (await r.json()).get("type"))


async def _ws(session, *messages: dict) -> list:
    out = []
    async with session.ws_connect(f"{BASE}/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": await _token(session)})
        assert (await ws.receive_json())["type"] == "auth_ok"
        for i, msg in enumerate(messages, 1):
            await ws.send_json({"id": i, **msg})
            while True:
                res = await ws.receive_json()
                if res.get("id") == i and res.get("type") == "result":
                    break
            if not res["success"]:
                raise RuntimeError(f"{msg['type']}: {res['error']}")
            out.append(res["result"])
    return out


async def cmd_rate() -> None:
    async with aiohttp.ClientSession() as s:
        await _ws(s, *({"type": "downtime_auditor/set_severity", "entity_id": e, "severity": v} for e, v in RATINGS.items()))
    print(f"rated {len(RATINGS)} automations/scripts")


async def _call(session, service: str, data: dict | None = None) -> None:
    domain, name = service.split(".", 1)
    async with session.post(f"{BASE}/api/services/{domain}/{name}", json=data or {}, headers=await _hdr(session)) as r:
        if r.status >= 400:
            raise RuntimeError(f"{service}: {r.status} {await r.text()}")


async def _set_state(session, entity_id: str, state: str) -> None:
    async with session.post(f"{BASE}/api/states/{entity_id}", json={"state": state}, headers=await _hdr(session)) as r:
        r.raise_for_status()


async def cmd_late_report(state: str = "on") -> None:
    """The slow 'integration' finally reports sensor.pool_pump (after a restart it's missing)."""
    async with aiohttp.ClientSession() as s:
        await _set_state(s, "sensor.pool_pump", state)
    print(f"sensor.pool_pump reported '{state}'")


async def cmd_call(service: str, data: str = "{}") -> None:
    async with aiohttp.ClientSession() as s:
        await _call(s, service, json.loads(data))


async def cmd_prepare() -> None:
    """States before the outage, plus runs that will be interrupted."""
    async with aiohttp.ClientSession() as s:
        await _call(s, "input_boolean.turn_off", {"entity_id": ["input_boolean.vacation_mode", "input_boolean.garage_door"]})
        await _call(s, "input_number.set_value", {"entity_id": "input_number.freezer_temp", "value": -18})
        async with s.post(f"{BASE}/api/events/start_wakeup", json={}, headers=await _hdr(s)) as r:
            r.raise_for_status()
        await _call(s, "script.turn_on", {"entity_id": "script.bedtime_lights"})
        await _set_state(s, "sensor.pool_pump", "off")
        await asyncio.sleep(2)
        await _call(s, "downtime_auditor.snapshot_now")
    print("prepared: vacation off, garage closed, freezer -18 °C, wake-up routine and bedtime script running")


# ---------------------------------------------------------------- offline edits (HA must be stopped)


def _store(name: str) -> tuple[Path, dict]:
    path = CONFIG / ".storage" / name
    return path, json.loads(path.read_text(encoding="utf-8"))


def cmd_backdate(hours: str, *flags: str) -> None:
    path, store = _store("downtime_auditor.state")
    sess = store["data"]["session"]
    when = (datetime.now(UTC) - timedelta(hours=float(hours))).isoformat()
    if "--unclean" in flags:
        sess.update(clean_shutdown=False, shutdown_at=None, last_heartbeat=when)
    else:
        if not sess.get("clean_shutdown"):
            raise SystemExit("last stop wasn't clean; use --unclean")
        sess.update(shutdown_at=when)
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    print(f"downtime now starts {when} ({'unclean' if '--unclean' in flags else 'clean'})")


def cmd_offline_changes() -> None:
    """What 'happened' while HA was down: garage opened, freezer warmed up."""
    path, store = _store("core.restore_state")
    changes = {"input_boolean.garage_door": "on", "input_number.freezer_temp": "-5.0"}
    found = set()
    for item in store["data"]:
        st = item.get("state") or {}
        if st.get("entity_id") in changes:
            st["state"] = changes[st["entity_id"]]
            found.add(st["entity_id"])
    missing = set(changes) - found
    if missing:
        raise SystemExit(f"not in restore_state yet: {missing}")
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    print("offline changes: garage door opened, freezer at -5 °C")


async def cmd_report_id() -> None:
    """generated_at of the current report (to wait for the next one exactly, not by the clock)."""
    async with aiohttp.ClientSession() as s:
        try:
            (rep,) = await _ws(s, {"type": "downtime_auditor/report"})
        except (aiohttp.ClientError, RuntimeError, AssertionError):
            rep = None
    print((rep or {}).get("generated_at") or "1970-01-01T00:00:00+00:00")


async def cmd_wait_report(since: str, timeout: str = "180") -> None:
    since_dt = datetime.fromisoformat(since)
    deadline = asyncio.get_running_loop().time() + float(timeout)
    async with aiohttp.ClientSession() as s:
        while asyncio.get_running_loop().time() < deadline:
            try:
                (rep,) = await _ws(s, {"type": "downtime_auditor/report"})
            except (aiohttp.ClientError, RuntimeError, AssertionError):
                rep = None
            if rep and datetime.fromisoformat(rep["generated_at"]) > since_dt:
                counts = rep["counts"]
                print(f"report: {rep['window']['duration']} downtime, {counts}, highest {rep['highest_severity']}")
                return
            await asyncio.sleep(3)
    raise SystemExit("no new report in time")


async def cmd_options(*pairs: str) -> None:
    """Change Downtime Auditor options, e.g. `options startup_delay=90` (via the options flow)."""
    updates = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        try:
            updates[key] = json.loads(value)  # numbers, true/false, lists
        except ValueError:
            updates[key] = value  # plain text, e.g. "none" or "high"
    async with aiohttp.ClientSession() as s:
        hdr = await _hdr(s)
        async with s.get(f"{BASE}/api/config/config_entries/entry", headers=hdr) as r:
            entry = next(e for e in await r.json() if e["domain"] == "downtime_auditor")
        async with s.post(f"{BASE}/api/config/config_entries/options/flow", json={"handler": entry["entry_id"]}, headers=hdr) as r:
            r.raise_for_status()
            flow = await r.json()
        async with s.post(f"{BASE}/api/config/config_entries/options/flow/{flow['flow_id']}", json=updates, headers=hdr) as r:
            if r.status >= 400:
                raise SystemExit(f"options: {r.status} {await r.text()}")
    print(f"options updated: {updates}")


async def cmd_confirm_http() -> None:
    """HA 2026.9+ applies the YAML http: settings (our port) as *pending* until an admin
    confirms them, and shows a blocking dialog meanwhile. Confirm them like the dialog does."""
    async with aiohttp.ClientSession() as s:
        try:
            await _ws(s, {"type": "http/config/promote"})
            print("confirmed the HTTP config")
        except RuntimeError as err:  # older HA: no such command / nothing pending
            print(f"no HTTP config to confirm ({err})")


async def cmd_tidy() -> None:
    """Ignore Repairs that only exist because this is a sandbox (e.g. HA's install-method notice)."""
    async with aiohttp.ClientSession() as s:
        (issues,) = await _ws(s, {"type": "repairs/list_issues"})
        other = [i for i in issues["issues"] if i["domain"] != "downtime_auditor" and not i.get("ignored")]
        if other:
            await _ws(s, *({"type": "repairs/ignore_issue", "domain": i["domain"], "issue_id": i["issue_id"], "ignore": True}
                           for i in other))
    print(f"ignored {len(other)} sandbox-only repair(s): {[i['issue_id'] for i in other]}")


async def _poll(check, what: str, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    async with aiohttp.ClientSession() as s:
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await check(s):
                    return
            except (aiohttp.ClientError, RuntimeError, AssertionError, OSError):
                pass
            await asyncio.sleep(1)
    raise SystemExit(f"timed out waiting for {what}")


async def cmd_wait_running(timeout: str = "300") -> None:
    """The web server answers before HA has finished starting; wait for RUNNING."""

    async def check(s):
        (cfg,) = await _ws(s, {"type": "get_config"})
        return cfg["state"] == "RUNNING"

    await _poll(check, "HA to finish starting", float(timeout))


async def cmd_wait_tracking(timeout: str = "180") -> None:
    """Downtime Auditor is recording (so a stop now produces a report on the next start)."""

    async def check(s):
        (st,) = await _ws(s, {"type": "downtime_auditor/status"})
        return st["tracking"]

    await _poll(check, "Downtime Auditor to start tracking", float(timeout))


def cmd_base_requirements() -> None:
    """Print the pip requirements of the components HA's frontend service list imports.

    homeassistant.helpers.service._base_components() imports these even when they
    aren't set up; a minimal venv without their packages makes the UI hang on
    "Loading data". Follows `dependencies` and `after_dependencies` recursively.
    """
    import homeassistant.components as comps  # noqa: PLC0415
    from homeassistant.helpers import service  # noqa: PLC0415
    import inspect  # noqa: PLC0415
    import re  # noqa: PLC0415

    src = inspect.getsource(service._base_components)
    imported = src.split("import (", 1)[1].split(")", 1)[0]
    todo = re.findall(r"^\s+([a-z_]+),?$", imported, re.M)
    if not todo:
        raise SystemExit("could not read _base_components(); HA changed it")
    root = Path(comps.__file__).parent
    seen, reqs = set(), set()
    while todo:
        name = todo.pop()
        if name in seen or not (root / name / "manifest.json").exists():
            continue
        seen.add(name)
        manifest = json.loads((root / name / "manifest.json").read_text(encoding="utf-8"))
        reqs.update(manifest.get("requirements", []))
        todo.extend(manifest.get("dependencies", []) + manifest.get("after_dependencies", []))
    print("\n".join(sorted(reqs)))


COMMANDS = {
    "base-requirements": cmd_base_requirements,
    "late-report": cmd_late_report,
    "report-id": cmd_report_id,
    "wait-running": cmd_wait_running,
    "wait-tracking": cmd_wait_tracking,
    "tidy": cmd_tidy,
    "confirm-http": cmd_confirm_http,
    "options": cmd_options,
    "automations": cmd_automations, "onboard": cmd_onboard, "install": cmd_install, "rate": cmd_rate,
    "prepare": cmd_prepare, "backdate": cmd_backdate, "offline-changes": cmd_offline_changes,
    "wait-report": cmd_wait_report, "call": cmd_call,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit(__doc__)
    result = COMMANDS[sys.argv[1]](*sys.argv[2:])
    if asyncio.iscoroutine(result):
        asyncio.run(result)
