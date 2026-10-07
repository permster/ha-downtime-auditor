"""Lifecycle: heartbeat, run tracking, shutdown snapshot, post-restart analysis."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, __version__ as HA_VERSION
from homeassistant.core import CoreState, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, label_registry as lr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .analyzer import Analyzer, Finding, Window, _parse_dt, interrupted_findings
from .const import (
    CONF_HEARTBEAT_INTERVAL,
    CONF_INCLUDE_SCRIPTS,
    CONF_MAX_WINDOW_DAYS,
    CONF_NOTIFY_SERVICE,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_PUSH_MIN_SEVERITY,
    CONF_PUSH_ONLY_ON_FINDINGS,
    CONF_REPAIRS,
    CONF_REPAIRS_MIN_SEVERITY,
    CONF_SHOW_SEVERITY_NONE,
    CONF_UNCONFIRMABLE_SEVERITY,
    CONF_HISTORY_DAYS,
    CONF_REPORT_DAYS,
    CONF_SIDEBAR_PANEL,
    CONF_STARTUP_DELAY,
    CONF_WRITE_JSON,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_INCLUDE_SCRIPTS,
    DEFAULT_MAX_WINDOW_DAYS,
    DEFAULT_NOTIFY_SERVICE,
    DEFAULT_PERSISTENT_NOTIFICATION,
    DEFAULT_PUSH_MIN_SEVERITY,
    DEFAULT_PUSH_ONLY_ON_FINDINGS,
    DEFAULT_REPAIRS,
    DEFAULT_REPAIRS_MIN_SEVERITY,
    DEFAULT_SEVERITY,
    DEFAULT_SHOW_SEVERITY_NONE,
    DEFAULT_UNCONFIRMABLE_SEVERITY,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_REPORT_DAYS,
    DEFAULT_SIDEBAR_PANEL,
    DEFAULT_STARTUP_DELAY,
    DEFAULT_WRITE_JSON,
    EVENT_REPORT,
    EVENT_REPORT_UPDATED,
    SEVERITY_LABELS,
    UNKNOWN_STATES,
    PERSISTENT_NOTIFICATION_ID,
    SIGNAL_REPORT_UPDATED,
    SEVERITY_SOURCE_DEFAULT,
    SEVERITY_SOURCE_LABEL,
    SEVERITY_SOURCE_TRIGGER,
    STORAGE_KEY,
    STORAGE_VERSION,
    Confidence,
    FindingType,
    Severity,
)
from .conditions import (
    BaselineSource,
    StateSource,
    async_history_source,
    condition_configs,
    condition_entities,
    evaluate_finding,
)
from .labels import SeverityLookup, async_create_labels
from .repairs import async_sync_issues
from .report import (
    async_deliver,
    async_update_json,
    async_write_json,
    build_report,
    refresh_summary,
    upgrade_legacy,
)
from .severity import at_least, effective_severity
from .snapshot import (
    automation_entities,
    capture_baseline,
    capture_running,
    trigger_configs,
)

_LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    CONF_HEARTBEAT_INTERVAL: DEFAULT_HEARTBEAT_INTERVAL,
    CONF_STARTUP_DELAY: DEFAULT_STARTUP_DELAY,
    CONF_PERSISTENT_NOTIFICATION: DEFAULT_PERSISTENT_NOTIFICATION,
    CONF_NOTIFY_SERVICE: DEFAULT_NOTIFY_SERVICE,
    CONF_PUSH_ONLY_ON_FINDINGS: DEFAULT_PUSH_ONLY_ON_FINDINGS,
    CONF_WRITE_JSON: DEFAULT_WRITE_JSON,
    CONF_REPORT_DAYS: DEFAULT_REPORT_DAYS,
    CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
    CONF_INCLUDE_SCRIPTS: DEFAULT_INCLUDE_SCRIPTS,
    CONF_MAX_WINDOW_DAYS: DEFAULT_MAX_WINDOW_DAYS,
    CONF_SIDEBAR_PANEL: DEFAULT_SIDEBAR_PANEL,
    CONF_REPAIRS: DEFAULT_REPAIRS,
    CONF_REPAIRS_MIN_SEVERITY: DEFAULT_REPAIRS_MIN_SEVERITY,
    CONF_PUSH_MIN_SEVERITY: DEFAULT_PUSH_MIN_SEVERITY,
    CONF_SHOW_SEVERITY_NONE: DEFAULT_SHOW_SEVERITY_NONE,
    CONF_UNCONFIRMABLE_SEVERITY: DEFAULT_UNCONFIRMABLE_SEVERITY,
}

SCHEDULE_PLATFORMS = {"time", "time_pattern", "sun", "calendar"}

# Triggers whose entity hadn't reported when the report was built are re-checked
# once it reports (after RECHECK_SETTLE s, so HA's own trigger has run), for up to
# RECHECK_TIMEOUT s; whatever is still unknown then is listed as unchecked.
RECHECK_SETTLE = 5
RECHECK_TIMEOUT = 600


class DowntimeAuditor:
    """Owns all state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.prev: dict[str, Any] = {}
        self.data: dict[str, Any] = {}
        self.active = False  # True once the post-restart analysis is done
        self.started_at: datetime | None = None
        self.startup_triggered: dict[str, list[dict]] = {}
        self.last_report: dict[str, Any] | None = None
        self._unsubs: list = []
        self._analysis_unsub = None
        self._analysis_due: datetime | None = None
        self.version: str | None = None  # integration version (set by async_setup_entry)
        self._rerate_unsub = None
        self._rechecks: dict[str, Any] | None = None
        self._frozen = False  # set once the shutdown snapshot is written

    # ------------------------------------------------------------------ options

    def opt(self, key: str) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, DEFAULTS[key]))

    # ------------------------------------------------------------------ setup

    async def async_start(self) -> None:
        self.prev = await self.store.async_load() or {}
        self.last_report = upgrade_legacy(self.prev.get("last_report"))
        now = dt_util.utcnow()
        self.data = {
            "session": {
                "id": uuid.uuid4().hex,
                "setup_at": now.isoformat(),
                "last_heartbeat": now.isoformat(),
                "clean_shutdown": False,
                "shutdown_at": None,
                "ha_version": HA_VERSION,
            },
            "baseline": None,
            "running": None,
            "last_report": self.last_report,
            "repair_issues": list(self.prev.get("repair_issues") or []),
            "labels_created": bool(self.prev.get("labels_created")),
            "trigger_ratings": dict(self.prev.get("trigger_ratings") or {}),
        }
        await self._async_create_labels_once()

        # Shutdown jobs run *before* integrations (and running scripts) are stopped.
        remove = self.hass.async_add_shutdown_job(HassJob(self._async_on_shutdown, cancel_on_shutdown=False))
        self._unsubs.append(remove)

        self._unsubs.append(self.hass.bus.async_listen("automation_triggered", self._on_run_started))
        self._unsubs.append(self.hass.bus.async_listen("script_started", self._on_run_started))
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._on_state_changed, event_filter=self._state_filter
            )
        )
        # Ratings changed outside the dashboard (HA's label editor) re-rate the current report.
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_ratings_changed, event_filter=self._labels_filter
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(lr.EVENT_LABEL_REGISTRY_UPDATED, self._on_ratings_changed)
        )

        if self.hass.state is CoreState.running:
            # Loaded after startup (fresh install or config-entry reload):
            # automations never stopped, so there is no downtime to analyze.
            _LOGGER.debug("Loaded while HA running; starting tracking without analysis")
            await self._async_activate(report=None)
        else:
            self._unsubs.append(async_at_started(self.hass, self._on_started))

    async def async_stop(self) -> None:
        """Config entry unload (not HA shutdown)."""
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()
        if self._analysis_unsub:
            self._analysis_unsub()
        if self._rerate_unsub:
            self._rerate_unsub()
            self._rerate_unsub = None
        self._stop_rechecks()
        if self.active:
            # Mark as clean so a later reload doesn't look like a crash.
            self._refresh(clean=True)
            await self.store.async_save(self.data)

    # ------------------------------------------------------------------ events

    @callback
    def _labels_filter(self, event_data: Any) -> bool:
        eid = str(event_data.get("entity_id", ""))
        return (
            event_data.get("action") == "update"
            and "labels" in (event_data.get("changes") or {})
            and (eid.startswith("automation.") or eid.startswith("script."))
        )

    @callback
    def _on_ratings_changed(self, _event: Event) -> None:
        """Debounced: a burst of label edits re-rates once."""
        if self._rerate_unsub:
            self._rerate_unsub()
        self._rerate_unsub = async_call_later(
            self.hass, 1, HassJob(self._async_rerate_job, cancel_on_shutdown=True)
        )

    async def _async_rerate_job(self, _now: datetime) -> None:
        self._rerate_unsub = None
        await self.async_rerate_last_report()

    @callback
    def _state_filter(self, event_data: Any) -> bool:
        eid = event_data.get("entity_id", "")
        return eid.startswith("automation.") or eid.startswith("script.")

    @callback
    def _on_state_changed(self, event: Event) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        old_cur = old.attributes.get("current") if old else None
        new_cur = new.attributes.get("current") if new else None
        if old_cur != new_cur:
            self._schedule_running_save()

    @callback
    def _on_run_started(self, event: Event) -> None:
        if not self.active and self.started_at is not None:
            source = str(event.data.get("source") or "")
            if event.event_type == "automation_triggered" and "Home Assistant start" not in source:
                self.startup_triggered.setdefault(event.data.get("entity_id"), []).append(
                    {
                        "time": dt_util.utcnow().isoformat(),
                        "source": source or None,
                        "name": event.data.get("name"),
                    }
                )
        self._schedule_running_save()

    @callback
    def _schedule_running_save(self) -> None:
        if not self.active or self._frozen:
            return
        # Debounced: the capture itself happens when the store writes (≤2s later),
        # so bursts of automation activity cost one capture, not one per event.
        self.store.async_delay_save(self._data_with_running, 2)

    @callback
    def _data_with_running(self) -> dict[str, Any]:
        if self._frozen:
            return self.data
        try:
            self.data["running"] = {
                "captured_at": dt_util.utcnow().isoformat(),
                "runs": capture_running(self.hass, self.opt(CONF_INCLUDE_SCRIPTS)),
            }
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Running capture failed")
        return self.data

    @callback
    def _on_started(self, hass: HomeAssistant) -> None:
        self.started_at = dt_util.utcnow()
        delay = max(0, int(self.opt(CONF_STARTUP_DELAY)))
        _LOGGER.debug("HA started; analyzing in %ss", delay)
        self._analysis_due = self.started_at + timedelta(seconds=delay)
        self._analysis_unsub = async_call_later(
            self.hass, delay, HassJob(self._async_run_startup_analysis, cancel_on_shutdown=True)
        )

    def pending(self) -> dict | None:
        """While a restart is being analyzed: what we're waiting for (for the dashboard)."""
        if self.active:
            return None
        if self.started_at is None:
            return {"state": "starting", "due_at": None, "startup_delay": int(self.opt(CONF_STARTUP_DELAY))}
        return {
            "state": "settling",
            "due_at": self._analysis_due.isoformat() if self._analysis_due else None,
            "startup_delay": int(self.opt(CONF_STARTUP_DELAY)),
        }

    async def _async_run_startup_analysis(self, _now: datetime | None = None) -> None:
        self._analysis_unsub = None
        report = None
        try:
            report = await self._async_analyze_previous_downtime()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Downtime analysis failed")
        await self._async_activate(report)

    # ------------------------------------------------------------------ heartbeat

    async def _async_activate(self, report: dict | None) -> None:
        """Start tracking the current session."""
        if report is not None:
            self.last_report = report
            self.data["last_report"] = report
        self.active = True
        self._refresh(clean=False)
        await self.store.async_save(self.data)
        interval = timedelta(seconds=max(10, int(self.opt(CONF_HEARTBEAT_INTERVAL))))
        self._unsubs.append(async_track_time_interval(self.hass, self._async_heartbeat, interval))

    @callback
    def _refresh(self, clean: bool) -> None:
        now = dt_util.utcnow().isoformat()
        sess = self.data["session"]
        sess["last_heartbeat"] = now
        sess["clean_shutdown"] = clean
        sess["shutdown_at"] = now if clean else None
        try:
            self.data["baseline"] = capture_baseline(self.hass)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Baseline capture failed")
        try:
            self.data["running"] = {
                "captured_at": now,
                "runs": capture_running(self.hass, self.opt(CONF_INCLUDE_SCRIPTS)),
            }
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Running capture failed")

    async def _async_heartbeat(self, _now: datetime) -> None:
        if not self.active or self._frozen:
            return
        self._refresh(clean=False)
        await self.store.async_save(self.data)
        async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)

    async def _async_on_shutdown(self) -> None:
        if not self.active:
            # Shut down again before the startup analysis ran. Leave the previous
            # session untouched so the next boot reports the combined window.
            _LOGGER.debug("Shutdown before analysis; keeping previous session")
            return
        self._refresh(clean=True)
        # Freeze: HA is about to stop running scripts, and those state changes
        # must not overwrite the snapshot of what was interrupted.
        self._frozen = True
        self._stop_rechecks()
        await self.store.async_save(self.data)
        _LOGGER.debug(
            "Shutdown snapshot saved (%s runs in progress)",
            len((self.data.get("running") or {}).get("runs", [])),
        )

    async def async_snapshot_now(self) -> dict:
        self._refresh(clean=False)
        await self.store.async_save(self.data)
        return {
            "running": self.data.get("running"),
            "baseline_entities": len((self.data.get("baseline") or {}).get("entities", {})),
            "baseline_templates": len((self.data.get("baseline") or {}).get("templates", {})),
        }

    # ------------------------------------------------------------------ analysis

    def _counts(self) -> tuple[int, int]:
        ents = automation_entities(self.hass)
        return len(ents), sum(len(trigger_configs(e)) for e in ents)

    async def _async_analyze_previous_downtime(self) -> dict | None:
        prev_sess = self.prev.get("session")
        if not prev_sess:
            _LOGGER.info("No previous session recorded (first run); nothing to analyze")
            return None
        clean = bool(prev_sess.get("clean_shutdown"))
        start = _parse_dt(prev_sess.get("shutdown_at") if clean else prev_sess.get("last_heartbeat"))
        end = self.started_at or dt_util.utcnow()
        if start is None:
            return None
        truncated = False
        max_days = int(self.opt(CONF_MAX_WINDOW_DAYS))
        if end - start > timedelta(days=max_days):
            start = end - timedelta(days=max_days)
            truncated = True
        window = Window(start=start, end=end, clean=clean)

        analyzer = Analyzer(self.hass, window, self.prev.get("baseline"))
        findings: list[Finding] = []
        findings += interrupted_findings(self.prev.get("running"), window, clean)
        findings += analyzer.analyze(self.startup_triggered)
        findings += await analyzer.async_analyze_calendars()
        findings += self._startup_fired_findings()
        self._check_conditions(findings, BaselineSource((self.prev.get("baseline") or {}).get("entities") or {}))
        self._rate(findings)

        n_auto, n_trig = self._counts()
        meta = {
            "previous_session": prev_sess.get("id"),
            "ha_version_before": prev_sess.get("ha_version"),
            "ha_version_after": HA_VERSION,
            "ha_started_at": end.isoformat(),
            "analyzed_at": dt_util.utcnow().isoformat(),
            "startup_delay_seconds": int(self.opt(CONF_STARTUP_DELAY)),
            "automations_checked": n_auto,
            "automations_skipped": len(analyzer.skipped),
            "triggers_checked": n_trig,
            "baseline_available": bool(self.prev.get("baseline")),
            "baseline_captured_at": (self.prev.get("baseline") or {}).get("captured_at"),
            "window_truncated_to_days": max_days if truncated else None,
            "analysis_errors": analyzer.errors,
        }
        report = build_report(window, findings, meta, self.opt(CONF_REPAIRS_MIN_SEVERITY))
        if analyzer.deferred:
            report["pending_checks"] = [dict(d) for d in analyzer.deferred]
            report["recheck_until"] = (dt_util.utcnow() + timedelta(seconds=RECHECK_TIMEOUT)).isoformat()
        await self._async_publish(report)
        self._start_rechecks(analyzer, report)
        return report

    @staticmethod
    def trigger_key(f: Any) -> str | None:
        """Stable key for one trigger of one automation (for per-trigger ratings).

        The trigger's `id:` if it has one, else its position and type. Works on Finding
        objects and finding dicts alike.
        """
        get = f.get if isinstance(f, dict) else lambda k: getattr(f, k, None)
        if get("type") != FindingType.MISSED or (get("trigger_id") is None and get("trigger_index") is None):
            return None
        which = f"id:{get('trigger_id')}" if get("trigger_id") is not None else f"#{get('trigger_index')}:{get('platform')}"
        return f"{get('item_id') or get('entity_id')}|{which}"

    def _base_severity(self, lookup: SeverityLookup, f: Any) -> tuple[Severity, str]:
        """How much it matters before any capping: this trigger's rating, the automation's label, else Medium."""
        if (key := self.trigger_key(f)) and (rated := (self.data.get("trigger_ratings") or {}).get(key)):
            return Severity(rated), SEVERITY_SOURCE_TRIGGER
        entity_id = f["entity_id"] if isinstance(f, dict) else f.entity_id
        if (sev := lookup.get(entity_id)) is not None:
            return sev, SEVERITY_SOURCE_LABEL
        return DEFAULT_SEVERITY, SEVERITY_SOURCE_DEFAULT

    def _rate(self, findings: list[Finding]) -> None:
        """Fill in each finding's effective severity."""
        lookup = SeverityLookup(self.hass)
        for f in findings:
            base, source = self._base_severity(lookup, f)
            f.severity, f.severity_reason = self._severity(base, source, f.type, f.confidence, f.details)
            f.severity_source = source
            f.severity_base = base.value

    async def async_set_trigger_severity(self, finding: dict, severity: Severity | None) -> str:
        """Rate one trigger (None clears it); re-rates the current report."""
        key = self.trigger_key(finding)
        if key is None:
            raise ValueError("only missed triggers can be rated individually")
        ratings = self.data.setdefault("trigger_ratings", {})
        if severity is None:
            ratings.pop(key, None)
        else:
            ratings[key] = Severity(severity).value
        await self.async_rerate_last_report()
        if self.active:
            await self.store.async_save(self.data)
        return key

    def _severity(self, base: Severity, source: str, ftype: str, confidence: str, details: dict) -> tuple[Severity, str]:
        cond = (details or {}).get("conditions") or {}
        sev, reason = effective_severity(
            base, source, ftype, confidence, cond.get("result"), self.opt(CONF_UNCONFIRMABLE_SEVERITY)
        )
        if cond.get("result") == "fail" and cond.get("why"):
            reason = f"{reason}: {cond['why']}"
        elif cond.get("likely") == "fail":
            reason = f"{reason}; conditions would probably have failed (pre-downtime values)"
        return sev, reason

    # Conditions are checked only where the trigger very likely did fire.
    CONDITION_CONFIDENCE = (Confidence.CONFIRMED, Confidence.PROBABLE)

    def _condition_targets(self, findings: list[Finding]) -> list[tuple[Finding, list[dict]]]:
        autos = {getattr(e, "entity_id", None): e for e in automation_entities(self.hass)}
        out = []
        for f in findings:
            if f.type != FindingType.MISSED or f.confidence not in self.CONDITION_CONFIDENCE:
                continue
            if (ent := autos.get(f.entity_id)) is None or not (confs := condition_configs(ent)):
                continue
            out.append((f, confs))
        return out

    def _check_conditions(self, findings: list[Finding], source: StateSource, targets=None) -> None:
        """Would the automation's conditions have passed when it should have fired?"""
        for f, confs in targets if targets is not None else self._condition_targets(findings):
            trigger_id = f.trigger_id if f.trigger_id is not None else (
                str(f.trigger_index) if f.trigger_index is not None else None
            )
            try:
                result = evaluate_finding(self.hass, confs, source, trigger_id, f.occurrences_iso)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Condition check failed for %s", f.entity_id, exc_info=True)
                continue
            f.conditions = result["result"]
            f.details["conditions"] = result

    async def async_rerate_last_report(self) -> None:
        """Re-apply labels to the last report after a rating changed (dashboard selector)."""
        rep = self.last_report
        if not rep or rep.get("legacy"):
            return  # old reports keep "Recorded before severity ratings existed"
        lookup = SeverityLookup(self.hass)
        for group in rep.get("findings") or []:
            for f in group.get("triggers") or [group]:  # rate each trigger; groups are rebuilt below
                base, source = self._base_severity(lookup, f)
                sev, reason = self._severity(base, source, f["type"], f["confidence"], f.get("details") or {})
                f.update(severity=sev.value, severity_source=source, severity_base=base.value, severity_reason=reason)
        refresh_summary(rep, self.opt(CONF_REPAIRS_MIN_SEVERITY))
        if self.opt(CONF_REPAIRS):
            self.data["repair_issues"] = async_sync_issues(
                self.hass, rep, self.data.get("repair_issues") or [], self.opt(CONF_REPAIRS_MIN_SEVERITY)
            )
        if self.active:
            await self.store.async_save(self.data)
        async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)

    async def _async_create_labels_once(self) -> None:
        """Create the severity labels on first setup only; never again automatically."""
        if self.data.get("labels_created"):
            return
        created = async_create_labels(self.hass)
        if created:
            _LOGGER.info("Created severity labels: %s", ", ".join(created))
        self.data["labels_created"] = True
        # Persist the flag without replacing the previous session, which the
        # startup analysis still needs.
        await self.store.async_save({**self.prev, "labels_created": True})

    # ------------------------------------------------------------------ late re-checks

    @callback
    def _start_rechecks(self, analyzer: Analyzer, report: dict) -> None:
        """Watch entities that hadn't reported yet; re-check their triggers when they do."""
        self._stop_rechecks()
        pending = list(analyzer.deferred)
        if not pending:
            return
        entities = sorted({p["entity"] for p in pending})
        self._rechecks = {
            "analyzer": analyzer,
            "report_id": report["generated_at"],
            "pending": pending,
            "timers": {},
            "unsubs": [
                async_track_state_change_event(self.hass, entities, self._on_pending_entity),
                async_call_later(
                    self.hass, RECHECK_TIMEOUT, HassJob(self._async_recheck_timeout, cancel_on_shutdown=True)
                ),
            ],
        }
        _LOGGER.debug("Re-checking %s trigger(s) once %s report", len(pending), entities)
        for entity_id in entities:  # some may have reported between the analysis and now
            if (st := self.hass.states.get(entity_id)) is not None and st.state not in UNKNOWN_STATES:
                self._schedule_recheck(entity_id)

    @callback
    def _stop_rechecks(self) -> None:
        if not self._rechecks:
            return
        for unsub in [*self._rechecks["unsubs"], *self._rechecks["timers"].values()]:
            unsub()
        self._rechecks = None

    @callback
    def _on_pending_entity(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is not None and new.state not in UNKNOWN_STATES:
            self._schedule_recheck(event.data["entity_id"])

    @callback
    def _schedule_recheck(self, entity_id: str) -> None:
        rc = self._rechecks
        if not rc:
            return
        if timer := rc["timers"].pop(entity_id, None):
            timer()

        async def _run(_now: datetime) -> None:
            await self._async_recheck_entity(entity_id)

        rc["timers"][entity_id] = async_call_later(
            self.hass, RECHECK_SETTLE, HassJob(_run, cancel_on_shutdown=True)
        )

    async def _async_recheck_entity(self, entity_id: str) -> None:
        rc, rep = self._rechecks, self.last_report
        if not rc or not rep or rep.get("generated_at") != rc["report_id"]:
            self._stop_rechecks()  # a newer report replaced this one
            return
        rc["timers"].pop(entity_id, None)
        resolved, new = [], []
        for item in [p for p in rc["pending"] if p["entity"] == entity_id]:
            try:
                finding, still_unknown = rc["analyzer"].recheck(item, self.startup_triggered)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Re-check failed for %s", item, exc_info=True)
                finding, still_unknown = None, False
            if still_unknown:
                continue
            resolved.append(item)
            if finding is not None:
                new.append(finding)
        if not resolved:
            return
        keys = {(p["automation"], p["trigger_index"], p["entity"]) for p in resolved}
        rc["pending"] = [p for p in rc["pending"] if (p["automation"], p["trigger_index"], p["entity"]) not in keys]
        rep["pending_checks"] = [
            p for p in rep.get("pending_checks") or []
            if (p["automation"], p["trigger_index"], p["entity"]) not in keys
        ]
        if new:
            self._check_conditions(new, BaselineSource((self.prev.get("baseline") or {}).get("entities") or {}))
            self._rate(new)
            rep["findings"].extend(f.as_dict() for f in new)
        _LOGGER.debug("%s reported: %s trigger(s) resolved, %s finding(s) added", entity_id, len(resolved), len(new))
        if not rc["pending"]:
            self._stop_rechecks()
        await self._async_report_changed(rep, new)

    async def _async_recheck_timeout(self, _now: datetime) -> None:
        rc, rep = self._rechecks, self.last_report
        self._stop_rechecks()
        if not rc or not rep or rep.get("generated_at") != rc["report_id"]:
            return
        rep["unchecked"] = rep.get("pending_checks") or []
        rep["pending_checks"] = []
        await self._async_report_changed(rep, [])

    async def _async_report_changed(self, rep: dict, new: list[Finding]) -> None:
        """The last report changed after publishing: update everything that shows it."""
        refresh_summary(rep, self.opt(CONF_REPAIRS_MIN_SEVERITY))
        if self.opt(CONF_REPAIRS):
            self.data["repair_issues"] = async_sync_issues(
                self.hass, rep, self.data.get("repair_issues") or [], self.opt(CONF_REPAIRS_MIN_SEVERITY)
            )
        if self.opt(CONF_WRITE_JSON):
            await async_update_json(self.hass, rep)
        self.data["last_report"] = rep
        if self.active:
            await self.store.async_save(self.data)
        async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)
        self.hass.bus.async_fire(
            EVENT_REPORT_UPDATED,
            {
                "generated_at": rep["generated_at"],
                "added": len(new),
                "pending_checks": len(rep.get("pending_checks") or []),
                "unchecked": len(rep.get("unchecked") or []),
                "counts": rep["counts"],
                "highest_severity": rep["highest_severity"],
                "needs_attention": rep["needs_attention"],
            },
        )
        await self._async_push_update(new)

    async def _async_push_update(self, new: list[Finding]) -> None:
        """Follow-up push when a re-check found something worth pushing."""
        service = (self.opt(CONF_NOTIFY_SERVICE) or "").strip()
        worth = [f for f in new if at_least(f.severity, self.opt(CONF_PUSH_MIN_SEVERITY))]
        domain, _, name = service.partition(".")
        if not worth or not name:
            return
        lines = [f"• [{SEVERITY_LABELS[f.severity]}] {f.name}: {f.summary}"[:160] for f in worth[:3]]
        try:
            await self.hass.services.async_call(
                domain,
                name,
                {"title": "Downtime Auditor: found after re-check", "message": "\n".join(lines)},
                blocking=False,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to send the follow-up push via %s", service)

    def _startup_fired_findings(self) -> list[Finding]:
        out = []
        ids = {e.entity_id: getattr(e, "unique_id", None) for e in automation_entities(self.hass)}
        for eid, runs in self.startup_triggered.items():
            srcs = sorted({r["source"] for r in runs if r.get("source")})
            name = runs[0].get("name") or eid
            out.append(
                Finding(
                    type=FindingType.FIRED_AT_STARTUP,
                    confidence=Confidence.CONFIRMED,
                    entity_id=eid,
                    name=name,
                    summary=f"Fired {len(runs)}× in the first {int(self.opt(CONF_STARTUP_DELAY))}s after startup"
                    + (f" (by: {'; '.join(srcs)[:200]})" if srcs else ""),
                    count=len(runs),
                    details={"runs": runs},
                    item_id=ids.get(eid),
                )
            )
        return out

    async def _async_publish(self, report: dict) -> None:
        json_path = None
        if self.opt(CONF_WRITE_JSON):
            json_path = await async_write_json(
                self.hass, report, int(self.opt(CONF_REPORT_DAYS)), int(self.opt(CONF_HISTORY_DAYS))
            )
        report.setdefault("meta", {})["json_path"] = json_path
        self.last_report = report
        self.data["last_report"] = report
        if self.opt(CONF_REPAIRS):
            self.data["repair_issues"] = async_sync_issues(
                self.hass, report, self.data.get("repair_issues") or [], self.opt(CONF_REPAIRS_MIN_SEVERITY)
            )
        else:
            self.data["repair_issues"] = async_sync_issues(
                self.hass, None, self.data.get("repair_issues") or [], Severity.HIGH
            )
        async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)
        await async_deliver(
            self.hass,
            report,
            persistent=self.opt(CONF_PERSISTENT_NOTIFICATION),
            notify_service=(self.opt(CONF_NOTIFY_SERVICE) or "").strip(),
            push_only_on_findings=self.opt(CONF_PUSH_ONLY_ON_FINDINGS),
            json_path=json_path,
            push_min_severity=self.opt(CONF_PUSH_MIN_SEVERITY),
        )
        self.hass.bus.async_fire(
            EVENT_REPORT,
            {
                "window": report["window"],
                "counts": report["counts"],
                "counts_by_severity": report["counts_by_severity"],
                "highest_severity": report["highest_severity"],
                "needs_attention": report["needs_attention"],
                "actionable": report["needs_attention"],  # deprecated; removed in v0.6.0
                "skipped": report["skipped"],
                "json_path": json_path,
            },
        )

    async def async_dismiss_repairs(self) -> int:
        ids = self.data.get("repair_issues") or []
        async_sync_issues(self.hass, None, ids, Severity.HIGH)
        self.data["repair_issues"] = []
        await self.store.async_save(self.data)
        return len(ids)

    async def async_resend_last(self) -> bool:
        if not self.last_report:
            return False
        await async_deliver(
            self.hass,
            self.last_report,
            persistent=True,
            notify_service=(self.opt(CONF_NOTIFY_SERVICE) or "").strip(),
            push_only_on_findings=False,
            json_path=(self.last_report.get("meta") or {}).get("json_path"),
        )
        return True

    async def async_analyze_window(self, start: datetime, end: datetime, notify: bool) -> dict:
        """What-if: which schedule-based triggers would a downtime in [start, end] miss?"""
        window = Window(start=dt_util.as_utc(start), end=dt_util.as_utc(end), clean=True)
        analyzer = Analyzer(self.hass, window, {})
        analyzer._covered = lambda occ, lt: False  # type: ignore[method-assign]  # what-if ignores real runs
        findings = [
            f for f in analyzer.analyze({}) if f.platform in SCHEDULE_PLATFORMS
        ] + await analyzer.async_analyze_calendars()
        targets = self._condition_targets(findings)
        needed: set[str] = set()
        for _f, confs in targets:
            needed |= set(condition_entities(self.hass, confs))
        source = await async_history_source(self.hass, needed, window.start, window.end)
        self._check_conditions(findings, source, targets)
        self._rate(findings)
        n_auto, n_trig = self._counts()
        report = build_report(
            window,
            findings,
            {
                "what_if": True,
                "automations_checked": n_auto,
                "automations_skipped": len(analyzer.skipped),
                "triggers_checked": n_trig,
                "analysis_errors": analyzer.errors,
                "note": "Simulation: only time, time_pattern, sun and calendar triggers are evaluated; "
                "conditions are checked against recorder history.",
                "conditions_basis": source.basis,
            },
            self.opt(CONF_REPAIRS_MIN_SEVERITY),
        )
        if notify:
            await async_deliver(
                self.hass,
                report,
                persistent=True,
                notify_service="",
                push_only_on_findings=True,
                json_path=None,
                notification_id=f"{PERSISTENT_NOTIFICATION_ID}_what_if",
            )
        return report
