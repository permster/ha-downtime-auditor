"""The dashboard's Rules tab describes what effective_severity actually does."""

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.downtime_auditor.const import DOMAIN, Severity
from custom_components.downtime_auditor.severity import effective_severity, severity_rules

# One example per rule: (effective_severity kwargs, expected severity) with the rule applying.
EXAMPLES = {
    "conditions": (dict(base=Severity.CRITICAL, source="label", confidence="confirmed", conditions="fail"), "none"),
    "already_back": (dict(base=Severity.CRITICAL, source="label", confidence="confirmed", finding_type="reverted",
                          already_back=True), "none"),
    "trigger": (dict(base=Severity.HIGH, source="trigger", confidence="unknown"), "high"),
    "catch_up": (dict(base=Severity.CRITICAL, source="label", confidence="confirmed", caught_up_in=5), "none"),
    "critical": (dict(base=Severity.CRITICAL, source="label", confidence="confirmed", finding_type="fired_at_startup"),
                 "critical"),
    "startup": (dict(base=Severity.HIGH, source="label", confidence="confirmed", finding_type="fired_at_startup"), "low"),
}


def _apply(unconfirmable, limit, base, source, confidence, conditions=None, finding_type="missed", caught_up_in=None,
           already_back=False):
    return effective_severity(
        base, source, finding_type, confidence, conditions, unconfirmable,
        caught_up_in=caught_up_in, catch_up_limit=limit, already_back=already_back,
    )[0]


@pytest.mark.parametrize("unconfirmable", ["low", "none"])
@pytest.mark.parametrize("limit", [0, 60])
def test_rules_match_effective_severity(unconfirmable, limit):
    rules = severity_rules(unconfirmable, limit)["adjust"]
    keys = [r["key"] for r in rules]
    # Order matters: the unconfirmable option set to None comes before Critical, the Low cap after.
    if unconfirmable == "none":
        assert keys.index("unconfirmable") < keys.index("critical")
        assert next(r for r in rules if r["key"] == "unconfirmable")["then"] == "None"
    else:
        assert keys.index("unconfirmable") > keys.index("critical")
        assert next(r for r in rules if r["key"] == "unconfirmable")["then"] == "capped at Low"
    assert _apply(unconfirmable, limit, Severity.CRITICAL, "label", "unknown") == (
        "none" if unconfirmable == "none" else "critical"
    )
    assert _apply(unconfirmable, limit, Severity.HIGH, "label", "unknown") == ("none" if unconfirmable == "none" else "low")

    for rule in rules:
        if rule["key"] not in EXAMPLES:
            continue
        kwargs, expected = EXAMPLES[rule["key"]]
        got = _apply(unconfirmable, limit, **kwargs)
        if rule["on"]:
            assert got == expected, rule
        else:  # a rule shown as off doesn't apply
            assert got != expected, rule

    catch_up = next(r for r in rules if r["key"] == "catch_up")
    assert catch_up["on"] is (limit > 0)
    assert catch_up["value"] == ("off" if limit == 0 else "60 min")


def test_starting_rating_is_described():
    start = severity_rules()["start"]
    assert [s["key"] for s in start] == ["trigger", "label", "default"]
    assert start[-1]["text"] == "otherwise Medium (unrated)"


async def test_status_carries_the_rules(hass, enable_custom_integrations, hass_ws_client):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2,
        options={"startup_delay": 0, "write_json": False, "unconfirmable_severity": "none",
                 "time_pattern_catch_up_minutes": 30},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "downtime_auditor/status"})
    rules = (await client.receive_json())["result"]["severity_rules"]
    assert rules == severity_rules("none", 30)
    assert next(r for r in rules["adjust"] if r["key"] == "catch_up")["when"].endswith("within 30 min of startup")
