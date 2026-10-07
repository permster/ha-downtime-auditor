"""Running-run detail must work with every HA trace-store shape.

The test suite runs an older HA, so the newer shape is simulated here.
"""

from types import SimpleNamespace

import pytest

from custom_components.downtime_auditor.snapshot import _running_traces


class FakeTrace:
    """Just enough of homeassistant.components.trace.models.ActionTrace."""

    def __init__(self, item_id: str, state: str = "running") -> None:
        self.short = {
            "domain": "automation",
            "item_id": item_id,
            "run_id": f"run-{item_id}",
            "state": state,
            "last_step": "action/1",
            "timestamp": {"start": "2026-10-06T09:00:00+00:00", "finish": None},
        }

    def as_short_dict(self) -> dict:
        return self.short

    def as_extended_dict(self) -> dict:
        return {
            **self.short,
            "trace": {"action/1": [{"result": {"delay": 1200.0, "done": False}, "timestamp": "2026-10-06T09:00:01+00:00"}]},
            "config": {"actions": [{"action": "light.turn_on"}, {"delay": "00:20:00"}]},
            "context": {"id": "ctx1"},
        }


def _old_shape(*traces):  # HA ≤ 2026.8: hass.data["trace"][key] is a dict of run_id -> trace
    return {f"automation.{t.short['item_id']}": {t.short["run_id"]: t for t in traces} for t in traces}


def _new_shape(*traces):  # HA 2026.9+: TraceData(runs=..., not_triggered=...)
    return {
        f"automation.{t.short['item_id']}": SimpleNamespace(
            runs={t.short["run_id"]: t for t in traces}, not_triggered={}
        )
        for t in traces
    }


@pytest.mark.parametrize("shape", [_old_shape, _new_shape], ids=["dict-of-runs", "TraceData"])
def test_running_trace_detail(shape):
    hass = SimpleNamespace(data={"trace": shape(FakeTrace("wake_up"))})
    (detail,) = _running_traces(hass)[("automation", "wake_up")]
    assert detail["last_step"] == "action/1"
    assert detail["last_step_result"] == {"delay": 1200.0, "done": False}
    assert detail["last_step_config"] == {"delay": "00:20:00"}
    assert detail["context_id"] == "ctx1"


@pytest.mark.parametrize("shape", [_old_shape, _new_shape], ids=["dict-of-runs", "TraceData"])
def test_finished_runs_are_ignored(shape):
    hass = SimpleNamespace(data={"trace": shape(FakeTrace("done", state="stopped"))})
    assert _running_traces(hass) == {}


def test_unknown_shape_degrades_to_no_detail():
    hass = SimpleNamespace(data={"trace": {"automation.x": object()}})
    assert _running_traces(hass) == {}
