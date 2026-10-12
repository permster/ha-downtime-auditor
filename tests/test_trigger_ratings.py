"""Per-trigger ratings and the "can't be confirmed" option."""

import copy
from datetime import timedelta

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er, label_registry as lr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.downtime_auditor.const import DOMAIN, STORAGE_KEY, Severity
from custom_components.downtime_auditor.labels import async_create_labels
from custom_components.downtime_auditor.severity import effective_severity
from custom_components.downtime_auditor.snapshot import describe_trigger


@pytest.mark.parametrize(
    ("base", "source", "confidence", "conditions", "unconfirmable", "expected"),
    [
        # The option at None hides unconfirmable triggers, even in a Critical automation.
        ("critical", "label", "unknown", None, "none", "none"),
        ("high", "default", "unknown", None, "none", "none"),
        ("high", "label", "unknown", None, "low", "low"),  # today's behavior
        ("critical", "label", "unknown", None, "low", "critical"),
        # A rating set for one trigger is taken as is: no caps, and it beats the option ...
        ("high", "trigger", "unknown", None, "none", "high"),
        ("medium", "trigger", "unknown", None, "low", "medium"),
        # ... but failed conditions still win.
        ("high", "trigger", "confirmed", "fail", "low", "none"),
        # The option only concerns unconfirmable triggers.
        ("high", "label", "confirmed", None, "none", "high"),
    ],
)
def test_precedence(base, source, confidence, conditions, unconfirmable, expected):
    sev, reason = effective_severity(Severity(base), source, "missed", confidence, conditions, unconfirmable)
    assert sev == expected and reason
    if source == "trigger" and conditions is None:
        assert reason == "set for this trigger"


async def _setup(hass, options):
    t_missed = (dt_util.now() - timedelta(hours=1)).strftime("%H:%M:%S")
    autos = [{"id": "door", "alias": "Doorbell", "triggers": [
        {"trigger": "time", "at": t_missed, "id": "chime_test"},
        {"trigger": "event", "event_type": "doorbell_pressed"},
    ], "actions": []}]
    await async_setup_component(hass, "automation", {"automation": autos})
    await hass.async_block_till_done()
    entry = MockConfigEntry(domain=DOMAIN, version=2, options={"startup_delay": 0, "write_json": False, **options})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    async_create_labels(hass)
    high = lr.async_get(hass).async_get_label_by_name("downtime_auditor_sev: high").label_id
    er.async_get(hass).async_update_entity("automation.doorbell", labels={high})
    auditor = hass.data[DOMAIN]
    await auditor._async_on_shutdown()
    saved = copy.deepcopy(auditor.data)
    saved["session"]["shutdown_at"] = (dt_util.utcnow() - timedelta(hours=2)).isoformat()
    auditor.prev = saved
    auditor.started_at = dt_util.utcnow()
    await auditor._async_analyze_previous_downtime()
    await hass.async_block_till_done()
    return entry, auditor


def _triggers(auditor):
    (group,) = [f for f in auditor.last_report["findings"] if f["entity_id"] == "automation.doorbell"]
    return group, {t["platform"]: t for t in group["triggers"]}


async def test_option_none_hides_unconfirmable_triggers(hass, enable_custom_integrations):
    _entry, auditor = await _setup(hass, {"unconfirmable_severity": "none"})
    group, by = _triggers(auditor)
    assert by["event"]["severity"] == "none" and "options set those to None" in by["event"]["severity_reason"]
    assert by["time"]["severity"] == "high"  # the automation's label, untouched
    assert group["severity"] == "high"


async def test_rate_one_trigger(hass, enable_custom_integrations, hass_ws_client, hass_storage):
    _entry, auditor = await _setup(hass, {})
    group, by = _triggers(auditor)
    assert (by["event"]["severity"], by["time"]["severity"]) == ("low", "high")  # default: capped at Low

    client = await hass_ws_client(hass)
    event = by["event"]
    await client.send_json_auto_id({
        "type": "downtime_auditor/set_trigger_severity", "entity_id": "automation.doorbell",
        "item_id": event["item_id"], "trigger_id": event["trigger_id"],
        "trigger_index": event["trigger_index"], "platform": "event", "severity": "none",
    })
    msg = await client.receive_json()
    assert msg["success"], msg
    _group, by = _triggers(auditor)
    assert (by["event"]["severity"], by["event"]["severity_source"]) == ("none", "trigger")
    assert by["time"]["severity"] == "high"  # only that trigger
    assert hass_storage[STORAGE_KEY]["data"]["trigger_ratings"] == {"door|#1:event": "none"}

    # A trigger with an id is keyed by it (survives reordering); null clears a rating.
    time_t = by["time"]
    await client.send_json_auto_id({
        "type": "downtime_auditor/set_trigger_severity", "entity_id": "automation.doorbell",
        "item_id": time_t["item_id"], "trigger_id": "chime_test", "trigger_index": 0, "platform": "time",
        "severity": "low",
    })
    assert (await client.receive_json())["success"]
    _group, by = _triggers(auditor)
    assert by["time"]["severity"] == "low"
    assert "door|id:chime_test" in hass_storage[STORAGE_KEY]["data"]["trigger_ratings"]
    await client.send_json_auto_id({
        "type": "downtime_auditor/set_trigger_severity", "entity_id": "automation.doorbell",
        "item_id": time_t["item_id"], "trigger_id": "chime_test", "trigger_index": 0, "platform": "time",
        "severity": None,
    })
    assert (await client.receive_json())["success"]
    _group, by = _triggers(auditor)
    assert by["time"]["severity"] == "high"  # back to the automation's label

    # Ratings are kept across restarts (the store's next session carries them).
    auditor.prev = copy.deepcopy(auditor.data)
    await auditor.async_start()
    assert auditor.data["trigger_ratings"] == {"door|#1:event": "none"}


async def test_changing_the_option_rerates_the_report(hass, enable_custom_integrations):
    entry, _auditor = await _setup(hass, {})
    hass.config_entries.async_update_entry(entry, options={**entry.options, "unconfirmable_severity": "none"})
    await hass.async_block_till_done()  # the entry reloads and re-rates
    _group, by = _triggers(hass.data[DOMAIN])
    assert by["event"]["severity"] == "none"


# ---------------------------------------------------------------- which trigger (for rating it)


@pytest.mark.parametrize(
    ("conf", "expected"),
    [
        ({"trigger": "time_pattern", "seconds": "/5"}, "Time pattern: every 5 seconds"),
        ({"trigger": "time_pattern", "minutes": "/15"}, "Time pattern: every 15 minutes"),
        ({"trigger": "time_pattern", "minutes": "/1"}, "Time pattern: every minute"),
        ({"trigger": "time_pattern", "hours": "/2"}, "Time pattern: every 2 hours"),
        ({"trigger": "time_pattern", "hours": "/2", "minutes": 30}, "Time pattern: hours=/2 minutes=30"),
        ({"trigger": "time", "at": "07:00:00"}, "Time: 07:00"),
        ({"trigger": "time", "at": {"entity_id": "sensor.alarm", "offset": "-00:05:00"}}, "Time: sensor.alarm −5 min"),
        ({"trigger": "event", "event_type": "doorbell_pressed"}, "Event: doorbell_pressed"),
        ({"trigger": "state", "entity_id": "cover.garage", "to": "open"}, "State: cover.garage changes to open"),
        ({"trigger": "state", "entity_id": ["a.a", "b.b", "c.c"], "from": "off"}, "State: a.a, b.b +1 changes from off"),
        # The field case: a clock sensor watched for going stale.
        ({"trigger": "state", "entity_id": "sensor.opnpool_time", "for": timedelta(minutes=5)},
         "State: sensor.opnpool_time changes, then holds for 5 min"),
        ({"trigger": "state", "entity_id": "binary_sensor.door", "to": "on", "for": {"hours": 1, "minutes": 30}},
         "State: binary_sensor.door changes to on, then holds for 1 h 30 min"),
        ({"trigger": "numeric_state", "entity_id": "sensor.t", "above": -10}, "Numeric state: sensor.t goes above -10"),
        ({"trigger": "numeric_state", "entity_id": "sensor.t", "above": 1, "below": 5, "for": "00:00:30"},
         "Numeric state: sensor.t goes above 1 and below 5, then stays for 30 s"),
        ({"platform": "sun", "event": "sunset", "offset": timedelta(minutes=-15)}, "Sun: sunset −15 min"),
        ({"trigger": "sun", "event": "sunrise", "offset": "01:30:00"}, "Sun: sunrise +1 h 30 min"),
        ({"trigger": "mqtt", "topic": "home/door"}, "MQTT: home/door"),
        ({"trigger": "device", "domain": "zha", "type": "remote_button_short_press", "subtype": "turn_on"},
         "Device: zha remote_button_short_press turn_on"),
        ({"trigger": "something_new"}, "something_new"),
    ],
)
def test_describe_trigger(conf, expected):
    assert describe_trigger(conf) == expected


async def test_findings_say_which_trigger(hass, enable_custom_integrations):
    _entry, auditor = await _setup(hass, {})
    _group, by = _triggers(auditor)
    assert (by["event"]["details"]["trigger"], by["event"]["details"]["trigger_of"]) == ("Event: doorbell_pressed", 2)
    assert by["time"]["details"]["trigger"].startswith("Time: ")
