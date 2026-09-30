"""Lifecycle: heartbeat, run tracking, shutdown snapshot, post-restart analysis."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, __version__ as HA_VERSION
from homeassistant.core import CoreState, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .analyzer import Analyzer, Finding, Window, _parse_dt, interrupted_findings
from .const import (
    CAT_STARTUP_FIRED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_INCLUDE_SCRIPTS,
    CONF_INCLUDE_UNVERIFIABLE,
    CONF_LOW,
    CONF_MAX_WINDOW_DAYS,
    CONF_NOTIFY_SERVICE,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_PUSH_ONLY_ON_FINDINGS,
    CONF_REPAIRS,
    CONF_REPAIRS_POSSIBLE,
    CONF_RETENTION,
    CONF_SIDEBAR_PANEL,
    CONF_STARTUP_DELAY,
    CONF_WRITE_JSON,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_INCLUDE_SCRIPTS,
    DEFAULT_INCLUDE_UNVERIFIABLE,
    DEFAULT_MAX_WINDOW_DAYS,
    DEFAULT_NOTIFY_SERVICE,
    DEFAULT_PERSISTENT_NOTIFICATION,
    DEFAULT_PUSH_ONLY_ON_FINDINGS,
    DEFAULT_REPAIRS,
    DEFAULT_REPAIRS_POSSIBLE,
    DEFAULT_RETENTION,
    DEFAULT_SIDEBAR_PANEL,
    DEFAULT_STARTUP_DELAY,
    DEFAULT_WRITE_JSON,
    EVENT_REPORT,
    PERSISTENT_NOTIFICATION_ID,
    SIGNAL_REPORT_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .repairs import async_sync_issues
from .report import async_deliver, async_write_json, build_report
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
    CONF_RETENTION: DEFAULT_RETENTION,
    CONF_INCLUDE_SCRIPTS: DEFAULT_INCLUDE_SCRIPTS,
    CONF_INCLUDE_UNVERIFIABLE: DEFAULT_INCLUDE_UNVERIFIABLE,
    CONF_MAX_WINDOW_DAYS: DEFAULT_MAX_WINDOW_DAYS,
    CONF_SIDEBAR_PANEL: DEFAULT_SIDEBAR_PANEL,
    CONF_REPAIRS: DEFAULT_REPAIRS,
    CONF_REPAIRS_POSSIBLE: DEFAULT_REPAIRS_POSSIBLE,
}

SCHEDULE_PLATFORMS = {"time", "time_pattern", "sun", "calendar"}


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
        self._frozen = False  # set once the shutdown snapshot is written

    # ------------------------------------------------------------------ options

    def opt(self, key: str) -> Any:
        return self.entry.options.get(key, self.entry.data.get(key, DEFAULTS[key]))

    # ------------------------------------------------------------------ setup

    async def async_start(self) -> None:
        self.prev = await self.store.async_load() or {}
        self.last_report = self.prev.get("last_report")
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
        }

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

        if self.hass.state is CoreState.running:
            # Loaded after startup (fresh install or config-entry reload):
            # automations never stopped, so there is no downtime to analyse.
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
        if self.active:
            # Mark as clean so a later reload doesn't look like a crash.
            self._refresh(clean=True)
            await self.store.async_save(self.data)

    # ------------------------------------------------------------------ events

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
        _LOGGER.debug("HA started; analysing in %ss", delay)
        self._analysis_unsub = async_call_later(
            self.hass, delay, HassJob(self._async_run_startup_analysis, cancel_on_shutdown=True)
        )

    async def _async_run_startup_analysis(self, _now: datetime | None = None) -> None:
        self._analysis_unsub = None
        report = None
        try:
            report = await self._async_analyse_previous_downtime()
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

    async def _async_analyse_previous_downtime(self) -> dict | None:
        prev_sess = self.prev.get("session")
        if not prev_sess:
            _LOGGER.info("No previous session recorded (first run); nothing to analyse")
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

        analyzer = Analyzer(self.hass, window, self.prev.get("baseline"), self.opt(CONF_INCLUDE_UNVERIFIABLE))
        findings: list[Finding] = []
        findings += interrupted_findings(self.prev.get("running"), window, clean)
        findings += analyzer.analyze(self.startup_triggered)
        findings += await analyzer.async_analyze_calendars()
        findings += self._startup_fired_findings()

        n_auto, n_trig = self._counts()
        meta = {
            "previous_session": prev_sess.get("id"),
            "ha_version_before": prev_sess.get("ha_version"),
            "ha_version_after": HA_VERSION,
            "ha_started_at": end.isoformat(),
            "analysed_at": dt_util.utcnow().isoformat(),
            "startup_delay_seconds": int(self.opt(CONF_STARTUP_DELAY)),
            "automations_checked": n_auto,
            "triggers_checked": n_trig,
            "baseline_available": bool(self.prev.get("baseline")),
            "baseline_captured_at": (self.prev.get("baseline") or {}).get("captured_at"),
            "window_truncated_to_days": max_days if truncated else None,
        }
        report = build_report(window, findings, meta)
        await self._async_publish(report)
        return report

    def _startup_fired_findings(self) -> list[Finding]:
        out = []
        ids = {e.entity_id: getattr(e, "unique_id", None) for e in automation_entities(self.hass)}
        for eid, runs in self.startup_triggered.items():
            srcs = sorted({r["source"] for r in runs if r.get("source")})
            name = runs[0].get("name") or eid
            out.append(
                Finding(
                    category=CAT_STARTUP_FIRED,
                    confidence=CONF_LOW,
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
            json_path = await async_write_json(self.hass, report, int(self.opt(CONF_RETENTION)))
        report.setdefault("meta", {})["json_path"] = json_path
        self.last_report = report
        self.data["last_report"] = report
        if self.opt(CONF_REPAIRS):
            self.data["repair_issues"] = async_sync_issues(
                self.hass, report, self.data.get("repair_issues") or [], self.opt(CONF_REPAIRS_POSSIBLE)
            )
        else:
            self.data["repair_issues"] = async_sync_issues(
                self.hass, None, self.data.get("repair_issues") or [], False
            )
        async_dispatcher_send(self.hass, SIGNAL_REPORT_UPDATED)
        await async_deliver(
            self.hass,
            report,
            persistent=self.opt(CONF_PERSISTENT_NOTIFICATION),
            notify_service=(self.opt(CONF_NOTIFY_SERVICE) or "").strip(),
            push_only_on_findings=self.opt(CONF_PUSH_ONLY_ON_FINDINGS),
            json_path=json_path,
        )
        self.hass.bus.async_fire(
            EVENT_REPORT,
            {
                "window": report["window"],
                "counts": report["counts"],
                "actionable": report["actionable"],
                "json_path": json_path,
            },
        )

    async def async_dismiss_repairs(self) -> int:
        ids = self.data.get("repair_issues") or []
        async_sync_issues(self.hass, None, ids, False)
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
        analyzer = Analyzer(self.hass, window, {}, include_unverifiable=False)
        analyzer._covered = lambda occ, lt: False  # type: ignore[method-assign]  # what-if ignores real runs
        findings = [
            f for f in analyzer.analyze({}) if f.platform in SCHEDULE_PLATFORMS
        ] + await analyzer.async_analyze_calendars()
        n_auto, n_trig = self._counts()
        report = build_report(
            window,
            findings,
            {
                "what_if": True,
                "automations_checked": n_auto,
                "triggers_checked": n_trig,
                "note": "Simulation: only time, time_pattern, sun and calendar triggers are evaluated.",
            },
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
