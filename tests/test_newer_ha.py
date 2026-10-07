"""Newer HA trigger layouts, and triggers we can't analyze.

HA 2026.9 moved the fields of integration-provided triggers (sun, zone, ...) under
`options`. CI runs an older HA, so the new layout is fed in directly here.
"""

from datetime import timedelta
import logging
from types import SimpleNamespace

import pytest

from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.analyzer import Analyzer, Window
from custom_components.downtime_auditor.report import to_markdown
from custom_components.downtime_auditor.snapshot import trigger_configs

SUN_OLD = {"platform": "sun", "event": "sunset", "offset": timedelta(minutes=-30), "id": "dusk"}
SUN_NEW = {"platform": "sun", "id": "dusk", "options": {"event": "sunset", "offset": timedelta(minutes=-30)}}
ZONE_OLD = {"platform": "zone", "entity_id": ["person.anne"], "zone": "zone.home", "event": "enter"}
ZONE_NEW = {"platform": "zone", "options": {"entity_id": ["person.anne"], "zone": "zone.home", "event": "enter"}}


def _automation(*triggers):
    return SimpleNamespace(entity_id="automation.probe", unique_id="probe", _trigger_config=list(triggers))


def test_options_are_lifted_to_the_top_level():
    assert trigger_configs(_automation(SUN_NEW)) == trigger_configs(_automation(SUN_OLD))
    assert trigger_configs(_automation(ZONE_NEW)) == trigger_configs(_automation(ZONE_OLD))
    assert "options" not in trigger_configs(_automation(SUN_NEW))[0]


def _sunset_window(hass):
    day = dt_util.now().date() - timedelta(days=1)
    sunset = get_astral_event_date(hass, "sunset", day)
    return Window(start=sunset - timedelta(hours=2), end=sunset + timedelta(hours=1), clean=True), sunset


@pytest.mark.parametrize("trigger", [SUN_OLD, SUN_NEW], ids=["flat", "options"])
async def test_sun_trigger_both_layouts(hass, trigger):
    hass.states.async_set("automation.probe", "on")
    window, sunset = _sunset_window(hass)
    analyzer = Analyzer(hass, window, {})
    (finding,) = analyzer._analyze_automation(_automation(trigger), "automation.probe", {})
    assert finding.type == "missed" and finding.confidence == "confirmed"
    assert finding.occurrences_iso == [(sunset - timedelta(minutes=30)).isoformat()]
    assert analyzer.errors == []


@pytest.mark.parametrize("trigger", [ZONE_OLD, ZONE_NEW], ids=["flat", "options"])
async def test_zone_trigger_both_layouts(hass, trigger):
    hass.states.async_set("automation.probe", "on")
    hass.states.async_set("person.anne", "home")
    window = Window(start=dt_util.utcnow() - timedelta(hours=1), end=dt_util.utcnow(), clean=True)
    baseline = {"entities": {"person.anne": {"state": "not_home", "attributes": {}}}}
    (finding,) = Analyzer(hass, window, baseline)._analyze_automation(_automation(trigger), "automation.probe", {})
    assert "transition matches the trigger" in finding.summary  # not "could not be evaluated"


async def test_unanalyzable_trigger_is_an_error_not_a_finding(hass, caplog, monkeypatch):
    hass.states.async_set("automation.probe", "on")
    window, _ = _sunset_window(hass)
    analyzer = Analyzer(hass, window, {})

    def broken(self, ctx):
        raise TypeError("attribute name must be string, not 'NoneType'")

    monkeypatch.setattr(Analyzer, "_t_sun", broken)
    with caplog.at_level(logging.WARNING):
        findings = analyzer._analyze_automation(_automation(SUN_OLD), "automation.probe", {})
    assert findings == []
    (err,) = analyzer.errors
    assert (err["automation"], err["trigger_index"], err["platform"]) == ("automation.probe", 0, "sun")
    assert "Couldn't analyze automation.probe trigger #0 (sun)" in caplog.text

    report = {
        "window": {"start_local": "a", "end_local": "b", "duration": "1h", "clean_shutdown": True},
        "meta": {"analysis_errors": analyzer.errors},
        "findings": [],
    }
    assert "Couldn't analyze 1 trigger(s)" in to_markdown(report, None)
