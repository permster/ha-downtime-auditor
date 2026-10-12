"""`for:` triggers: decided from what the entity does after startup."""

import copy
from datetime import timedelta

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import State
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor import holds
from custom_components.downtime_auditor.const import DOMAIN

T0 = dt_util.parse_datetime("2026-10-11T22:55:03+00:00")  # downtime start
END = T0 + timedelta(seconds=90)  # HA started
HOLD = 300.0  # for: 5 min


def _st(value, at, **attrs):
    return State("sensor.clock", value, attrs, last_changed=at, last_updated=at)


@pytest.mark.parametrize(
    ("value", "None_", "expected"),
    [
        (timedelta(minutes=5), None, 300), ("00:05:00", None, 300), ({"minutes": 5}, None, 300),
        (300, None, 300), ("{{ states('input_number.x') }}", None, None), (None, None, None),
    ],
)
def test_for_seconds(value, None_, expected):
    assert holds.for_seconds(value) == expected


def test_changed_again_too_soon_is_ruled_out():
    """The clock ticks again 3 min after the downtime began: 5 min can't have passed."""
    changes = [(_st("22:59", END - timedelta(seconds=10)), _st("23:00", T0 + timedelta(minutes=3)))]
    result, at = holds.verdict(_st("23:00", T0 + timedelta(minutes=3)), None, "23:00", HOLD, T0, END, END, changes)
    assert (result, at) == (holds.RULED_OUT, T0 + timedelta(minutes=3))


def test_changed_again_later_can_not_be_told():
    changes = [(_st("22:59", END), _st("23:10", T0 + timedelta(minutes=8)))]
    assert holds.verdict(None, None, "22:59", HOLD, T0, END, END, changes)[0] == holds.CHANGED


def test_late_integration_is_not_a_change():
    """unavailable → value after startup is the integration reporting, not the value changing."""
    changes = [(State("sensor.clock", "unavailable"), _st("22:59", END + timedelta(seconds=20)))]
    result, deadline = holds.verdict(
        _st("22:59", END + timedelta(seconds=20)), None, "22:59", HOLD, T0, END, END + timedelta(seconds=60), changes
    )
    assert (result, deadline) == (holds.WATCH, END + timedelta(seconds=HOLD))


@pytest.mark.parametrize(("now", "expected"), [(END + timedelta(seconds=60), holds.WATCH),
                                               (END + timedelta(seconds=300), holds.HELD)])
def test_still_holding(now, expected):
    assert holds.verdict(_st("22:59", END - timedelta(seconds=30)), None, "22:59", HOLD, T0, END, now)[0] == expected


def test_value_only_changed_after_startup():
    """A clock that stopped while HA was down and ticks after startup: HA saw that change itself."""
    changes = [(_st("22:56", T0 - timedelta(minutes=1)), _st("23:30", END + timedelta(seconds=20)))]
    result, _ = holds.verdict(_st("23:30", END + timedelta(seconds=20)), None, "23:30", HOLD, T0, END,
                              END + timedelta(seconds=60), changes, before="22:56")
    assert result == holds.RULED_OUT


def test_attribute_changes_count():
    old = _st("on", END, brightness=100)
    new = State("sensor.clock", "on", {"brightness": 50}, last_changed=END, last_updated=T0 + timedelta(minutes=2))
    assert holds.first_real_change([(old, new)], "brightness") == T0 + timedelta(minutes=2)
    assert holds.first_real_change([(old, new)], None) is None  # the state itself didn't change


# ---------------------------------------------------------------- end to end


async def _setup(hass, hold="00:05:00", downtime=60):
    hass.states.async_set("sensor.clock", "22:56")
    autos = [{"id": "stale", "alias": "Clock stale", "triggers": [
        {"trigger": "state", "entity_id": "sensor.clock", "for": hold, "id": "time_stale"}], "actions": []}]
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, version=2,
                            options={"startup_delay": 0, "write_json": False, "repairs_min_severity": "low"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()  # baseline: 22:56
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(seconds=downtime)).isoformat()
    auditor.prev = saved
    hass.states.async_set("sensor.clock", "22:59")  # changed while "down"
    await hass.async_block_till_done()
    auditor.started_at = dt_util.utcnow()
    auditor._record_post_start_changes()
    return auditor


def _finding(auditor):
    return next((f for f in auditor.last_report["findings"] if f["entity_id"] == "automation.clock_stale"), None)


def _issues(hass):
    return [i for (d, _), i in ir.async_get(hass).issues.items() if d == DOMAIN]


async def test_clock_ticks_again_before_the_analysis(hass, enable_custom_integrations):
    """The field case: the clock ticks again during the settle delay → dropped right away."""
    auditor = await _setup(hass)
    hass.states.async_set("sensor.clock", "23:00")
    await hass.async_block_till_done()
    await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    assert _finding(auditor) is None
    assert auditor._holds is None and auditor._post_start_unsub is None


async def test_clock_ticks_again_while_watched(hass, enable_custom_integrations):
    auditor = await _setup(hass)
    await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    f = _finding(auditor)
    assert f["confidence"] == "probable" and f["details"]["hold_check"]["result"] == "watch"
    assert "watching: if it's still '22:59' at" in f["details"]["hold_check"]["why"]
    assert len(_issues(hass)) == 1

    hass.states.async_set("sensor.clock", "23:00")
    await hass.async_block_till_done()
    assert _finding(auditor) is None  # couldn't have held 5 min
    assert _issues(hass) == []  # and its Repair is gone


async def test_value_held_until_the_deadline(hass, enable_custom_integrations, freezer):
    auditor = await _setup(hass)
    await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=301))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    f = _finding(auditor)
    assert f["confidence"] == "confirmed" and f["details"]["hold_check"]["result"] == "held"
    assert "so it would have fired" in f["details"]["hold_check"]["why"]


async def test_long_for_stops_watching_after_the_limit(hass, enable_custom_integrations, freezer):
    auditor = await _setup(hass, hold="01:00:00")
    await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=601))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    f = _finding(auditor)
    assert f["confidence"] == "probable" and f["details"]["hold_check"]["result"] == "timeout"
    assert auditor._holds is None


async def test_repairs_text_says_what_happened(hass, enable_custom_integrations):
    auditor = await _setup(hass)
    await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    (issue,) = _issues(hass)
    details = issue.translation_placeholders["details"]
    assert "Trigger: State: sensor.clock changes, then holds for 5 min" in details
    assert "sensor.clock: `22:56` before the downtime → `22:59` after startup" in details
    assert "Held long enough? watching:" in details
    assert "`for:`" not in details


async def test_changes_are_recorded_from_a_real_boot(hass, enable_custom_integrations, hass_storage):
    """Through the real startup path: changes after 'started' are recorded until the analysis."""
    from homeassistant.core import CoreState

    from tests.common import fake_boot

    hass.set_state(CoreState.not_running)
    hass.states.async_set("sensor.clock", "22:56")
    autos = [{"id": "stale", "alias": "Clock stale", "triggers": [
        {"trigger": "state", "entity_id": "sensor.clock", "for": "00:05:00"}], "actions": []}]
    await async_setup_component(hass, "automation", {"automation": autos})
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 60, "write_json": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await fake_boot(hass)
    auditor = hass.data[DOMAIN]
    assert auditor._post_start_unsub is not None
    hass.states.async_set("sensor.clock", "22:57")
    await hass.async_block_till_done()
    assert [(o.state, n.state) for o, n in auditor._post_start["sensor.clock"]] == [("22:56", "22:57")]
