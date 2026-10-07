"""Work out which automations were missed or interrupted during a downtime window."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from . import schedule
from .const import (
    DEFAULT_SEVERITY,
    SEVERITY_SOURCE_DEFAULT,
    UNKNOWN_STATES,
    Confidence,
    FindingType,
)
from .snapshot import (
    as_list,
    automation_entities,
    jsonable,
    render_bool,
    render_number,
    template_source,
    trigger_configs,
    trigger_enabled,
    trigger_entities,
    trigger_platform,
)

_LOGGER = logging.getLogger(__name__)

# Triggers that fire on a message/event: anything sent while HA was down is gone,
# so there's no way to tell whether one was missed.
LOST_MESSAGE_REASONS = {
    "event": "Events fired while Home Assistant was down are never delivered.",
    "webhook": "Webhook calls made while Home Assistant was down are lost.",
    "mqtt": "MQTT messages sent while Home Assistant was down are lost "
    "(retained ones are re-sent when it reconnects, which may simply run the automation then).",
    "tag": "Tag scans while Home Assistant was down are lost.",
    "conversation": "Voice and text commands while Home Assistant was down are lost.",
    "device": "Device triggers that are presses or events (buttons, remotes) aren't recorded, "
    "so any while Home Assistant was down are lost.",
    "persistent_notification": "Notification events while Home Assistant was down are lost.",
    "geo_location": "Geo-location enter/leave events while Home Assistant was down are lost.",
    "homeassistant": None,  # start/shutdown triggers are not "missed"
}

CONFIRMED_IF_CLEAN = {True: Confidence.CONFIRMED, False: Confidence.PROBABLE}


@dataclass
class Finding:
    """One thing worth telling the user about."""

    type: str
    confidence: str
    entity_id: str
    name: str
    summary: str
    platform: str | None = None
    trigger_index: int | None = None
    trigger_id: str | None = None
    occurrences: list[str] = field(default_factory=list)
    occurrences_iso: list[str] = field(default_factory=list)
    count: int | None = None
    details: dict[str, Any] = field(default_factory=dict)
    item_id: str | None = None  # automation config id / script object id (for UI links)
    severity: str = DEFAULT_SEVERITY
    severity_source: str = SEVERITY_SOURCE_DEFAULT
    severity_base: str = DEFAULT_SEVERITY  # the rating before capping (label or default)
    severity_reason: str = ""
    conditions: str | None = None  # pass / fail / unknown; None = not evaluated

    def as_dict(self) -> dict:
        """Serialise."""
        return jsonable(asdict(self))


@dataclass
class Window:
    """The period during which triggers were not being listened to."""

    start: datetime  # last moment we know HA was alive
    end: datetime  # moment automations re-attached (homeassistant_started)
    clean: bool

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return dt_util.as_utc(value) if value.tzinfo else value.replace(tzinfo=dt_util.UTC)
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(parsed)


def _local(dt: datetime) -> str:
    return dt_util.as_local(dt).strftime("%Y-%m-%d %H:%M:%S")


class Analyzer:
    """Evaluate every automation trigger against a downtime window."""

    def __init__(
        self,
        hass: HomeAssistant,
        window: Window,
        baseline: dict | None,
    ) -> None:
        self.hass = hass
        self.window = window
        self.baseline = baseline or {}
        self.pre_entities: dict = self.baseline.get("entities") or {}
        self.pre_templates: dict = self.baseline.get("templates") or {}
        self.pre_automations: dict = self.baseline.get("automations") or {}
        self.tz = dt_util.get_default_time_zone()
        self._pending_calendar: list[dict] = []
        self.skipped: list[str] = []  # automations that were off; counted, not reported
        self.deferred: list[dict] = []  # triggers whose entity hadn't reported yet (re-checked later)
        self.errors: list[dict] = []  # triggers we couldn't analyze (shown apart, not as findings)

    # ------------------------------------------------------------------ helpers

    def _pre_value(self, entity_id: str, attribute: str | None) -> tuple[bool, Any]:
        """Return (have_baseline, value)."""
        rec = self.pre_entities.get(entity_id)
        if rec is None:
            return False, None
        if attribute:
            return True, (rec.get("attributes") or {}).get(attribute)
        return True, rec.get("state")

    @staticmethod
    def _post_value(state: State | None, attribute: str | None) -> Any:
        if state is None:
            return None
        if attribute:
            return jsonable(state.attributes.get(attribute))
        return state.state

    def _covered(self, occ: datetime, last_triggered: datetime | None) -> bool:
        """Automation ran at/after the occurrence but before triggers re-attached.

        Only possible after an unclean shutdown, where the window start is the
        last heartbeat rather than the real moment HA died.
        """
        return last_triggered is not None and occ <= last_triggered < self.window.end

    def _changed_after_start(self, state: State | None) -> bool:
        return state is not None and state.last_changed >= self.window.end

    # ------------------------------------------------------------------ main

    def analyze(self, startup_triggered: dict[str, list[dict]]) -> list[Finding]:
        findings: list[Finding] = []
        for ent in automation_entities(self.hass):
            entity_id = getattr(ent, "entity_id", None)
            if not entity_id:
                continue
            try:
                findings.extend(self._analyze_automation(ent, entity_id, startup_triggered))
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Failed analyzing %s", entity_id)
                self._error({"entity_id": entity_id, "name": entity_id}, None, err)
        return findings

    def _error(self, ctx: dict, idx: int | None, err: Exception | str) -> None:
        """A trigger we couldn't analyze is our problem, not a missed trigger: listed apart."""
        self.errors.append(
            {
                "automation": ctx["entity_id"],
                "name": ctx.get("name") or ctx["entity_id"],
                "trigger_index": idx,
                "platform": ctx.get("platform"),
                "error": str(err),
            }
        )
        _LOGGER.warning(
            "Couldn't analyze %s trigger #%s (%s): %s. Please report this at "
            "https://github.com/permster/ha-downtime-auditor/issues",
            ctx["entity_id"], idx, ctx.get("platform"), err,
        )

    def _analyze_automation(
        self, ent: Any, entity_id: str, startup_triggered: dict[str, list[dict]]
    ) -> list[Finding]:
        state = self.hass.states.get(entity_id)
        name = (state.attributes.get("friendly_name") if state else None) or entity_id
        pre = self.pre_automations.get(entity_id)
        was_enabled = pre.get("enabled") if pre is not None else None
        if was_enabled is None:  # no baseline for it (or what-if): fall back to its current state
            was_enabled = not (state is not None and state.state == "off")
        if not was_enabled:
            self.skipped.append(entity_id)
            return []

        last_triggered = _parse_dt(state.attributes.get("last_triggered")) if state else None
        fired_after_start = bool(startup_triggered.get(entity_id))
        out: list[Finding] = []

        for idx, conf in enumerate(trigger_configs(ent)):
            if not trigger_enabled(conf):
                continue
            platform = trigger_platform(conf)
            handler = getattr(self, f"_t_{platform}", None)
            ctx = {
                "entity_id": entity_id,
                "name": name,
                "idx": idx,
                "conf": conf,
                "platform": platform,
                "trigger_id": str(conf["id"]) if conf.get("id") is not None else None,
                "last_triggered": last_triggered,
                "fired_after_start": fired_after_start,
                "item_id": getattr(ent, "unique_id", None),
            }
            try:
                results = handler(ctx) if handler else self._t_generic(ctx)
            except Exception as err:  # noqa: BLE001
                self._error(ctx, idx, err)
                results = []
            out.extend(r for r in results if r is not None)

        if state is not None and state.state == "off":
            for f in out:
                f.details["note"] = "Automation is currently turned off."
        return out

    def recheck(self, pending: dict, startup_triggered: dict[str, list[dict]]) -> tuple[Finding | None, bool]:
        """Re-run one deferred trigger now that its entity may have reported.

        Returns (finding or None, still_unknown). Uses the same window and
        pre-downtime values as the original analysis; "after" is the value now.
        Counts as handled by HA if the automation triggered after the entity changed.
        """
        auto_id, target = pending["automation"], pending["entity"]
        ent = next((e for e in automation_entities(self.hass) if getattr(e, "entity_id", None) == auto_id), None)
        confs = trigger_configs(ent) if ent is not None else []
        idx = pending["trigger_index"]
        if idx >= len(confs):
            return None, False  # automation removed or edited since
        state = self.hass.states.get(auto_id)
        target_state = self.hass.states.get(target)
        last_triggered = _parse_dt(state.attributes.get("last_triggered")) if state else None
        fired = bool(startup_triggered.get(auto_id)) or bool(
            last_triggered and target_state and last_triggered >= target_state.last_changed
        )
        conf = confs[idx]
        platform = trigger_platform(conf)
        ctx = {
            "entity_id": auto_id,
            "name": pending.get("name") or auto_id,
            "idx": idx,
            "conf": conf,
            "platform": platform,
            "trigger_id": str(conf["id"]) if conf.get("id") is not None else None,
            "last_triggered": last_triggered,
            "fired_after_start": fired,
            "item_id": getattr(ent, "unique_id", None),
        }
        handler = getattr(self, f"_t_{platform}", None) or self._t_generic
        before = len(self.deferred)
        results = [r for r in handler(ctx) if r is not None and (r.details or {}).get("entity") == target]
        # A multi-entity trigger re-evaluates its other entities too; those are tracked already.
        still_unknown = any(d["entity"] == target for d in self.deferred[before:])
        del self.deferred[before:]
        if results:
            results[0].details["rechecked"] = True
        return (results[0] if results else None), still_unknown

    def _mk(self, ctx: dict, finding_type: str, confidence: str, summary: str, **kw: Any) -> Finding:
        return Finding(
            type=finding_type,
            confidence=confidence,
            entity_id=ctx["entity_id"],
            name=ctx["name"],
            summary=summary,
            platform=ctx["platform"],
            trigger_index=ctx["idx"],
            trigger_id=ctx["trigger_id"],
            item_id=ctx.get("item_id"),
            **kw,
        )

    def _scheduled(self, ctx: dict, occs: list[datetime], label: str) -> list[Finding]:
        """Common handling for anything that produces a list of fire times."""
        missed = [o for o in occs if not self._covered(o, ctx["last_triggered"])]
        if not missed:
            return []
        shown = [_local(o) for o in missed[:25]]
        summary = (
            f"{label} was due at {shown[0]} while Home Assistant was down"
            if len(missed) == 1
            else f"{label} was due {len(missed)} times while Home Assistant was down "
            f"(first {shown[0]}, last {_local(missed[-1])})"
        )
        return [
            self._mk(
                ctx,
                FindingType.MISSED,
                CONFIRMED_IF_CLEAN[self.window.clean],
                summary,
                occurrences=shown,
                occurrences_iso=[o.isoformat() for o in missed[:200]],
                count=len(missed),
                details={},
            )
        ]

    # ------------------------------------------------------------------ time

    def _entity_time_occurrences(self, entity_id: str, offset: timedelta) -> tuple[list[datetime], str]:
        have, value = self._pre_value(entity_id, None)
        source = "value before downtime"
        if not have or value in UNKNOWN_STATES:
            st = self.hass.states.get(entity_id)
            value = st.state if st else None
            source = "current value"
        if value in UNKNOWN_STATES:
            return [], f"{entity_id} has no usable value"
        w = self.window
        domain = entity_id.split(".", 1)[0]
        text = str(value)
        if domain == "input_datetime":
            if (d := dt_util.parse_datetime(text)) is not None and ("-" in text and ":" in text):
                if d.tzinfo is None:
                    d = d.replace(tzinfo=self.tz)
                d = d + offset
                return ([d] if w.start < d <= w.end else []), source
            if (t := dt_util.parse_time(text)) is not None:
                return schedule.daily_occurrences(t, w.start, w.end, self.tz, None, offset), source
            if (dd := dt_util.parse_date(text)) is not None:
                d = schedule.local_dt(dd, time(0, 0), self.tz) + offset
                return ([d] if w.start < d <= w.end else []), source
            return [], f"could not parse {entity_id}={text}"
        # sensor with device_class timestamp
        d = _parse_dt(text)
        if d is None:
            return [], f"{entity_id} is not a timestamp"
        d = d + offset
        return ([d] if w.start < d <= w.end else []), source

    def _t_time(self, ctx: dict) -> list[Finding]:
        conf = ctx["conf"]
        weekdays = schedule.normalize_weekdays(conf.get("weekday"))
        occs: list[datetime] = []
        labels: list[str] = []
        notes: list[str] = []
        for at in as_list(conf.get("at")):
            if isinstance(at, time):
                occs += schedule.daily_occurrences(at, self.window.start, self.window.end, self.tz, weekdays)
                labels.append(at.strftime("%H:%M:%S"))
            elif isinstance(at, str) and ":" in at and "." not in at:
                t = dt_util.parse_time(at)
                if t:
                    occs += schedule.daily_occurrences(t, self.window.start, self.window.end, self.tz, weekdays)
                    labels.append(at)
            else:
                eid = at if isinstance(at, str) else at.get("entity_id")
                offset = at.get("offset", timedelta(0)) if isinstance(at, dict) else timedelta(0)
                if not isinstance(offset, timedelta):
                    offset = timedelta(seconds=float(offset or 0))
                found, note = self._entity_time_occurrences(eid, offset)
                occs += found
                labels.append(eid + (f" offset {offset}" if offset else ""))
                notes.append(note)
        res = self._scheduled(ctx, sorted(occs), f"Time trigger ({', '.join(labels)})")
        for f in res:
            if notes:
                f.details["time_source"] = notes
            if weekdays is not None:
                f.details["weekday"] = conf.get("weekday")
        return res

    def _t_time_pattern(self, ctx: dict) -> list[Finding]:
        conf = ctx["conf"]
        count, occs = schedule.time_pattern_occurrences(
            self.window.start,
            self.window.end,
            self.tz,
            conf.get("hours"),
            conf.get("minutes"),
            conf.get("seconds"),
        )
        if not count:
            return []
        pattern = "/".join(str(conf.get(k, "-")) for k in ("hours", "minutes", "seconds"))
        res = self._scheduled(ctx, occs, f"Time pattern (h/m/s {pattern})")
        for f in res:
            f.count = count if f.count == len(occs) else f.count
        return res

    def _t_sun(self, ctx: dict) -> list[Finding]:
        from homeassistant.helpers.sun import get_astral_event_date

        conf = ctx["conf"]
        event = conf.get("event")
        offset = conf.get("offset") or timedelta(0)
        if not isinstance(offset, timedelta):
            offset = timedelta(seconds=float(offset))
        occs: list[datetime] = []
        for day in schedule.iter_local_dates(
            self.window.start - timedelta(days=1), self.window.end + timedelta(days=1), self.tz
        ):
            when = get_astral_event_date(self.hass, event, day)
            if when is None:
                continue
            when = when + offset
            if self.window.start < when <= self.window.end:
                occs.append(when)
        label = f"Sun {event}" + (f" (offset {offset})" if offset else "")
        return self._scheduled(ctx, sorted(set(occs)), label)

    def _t_calendar(self, ctx: dict) -> list[Finding]:
        # Calendar triggers need an async service call; resolved in async_analyze_calendar.
        ctx_copy = dict(ctx)
        self._pending_calendar.append(ctx_copy)
        return []

    async def async_analyze_calendars(self) -> list[Finding]:
        """Resolve calendar triggers (needs async service calls)."""
        out: list[Finding] = []
        pending, self._pending_calendar = self._pending_calendar, []
        for ctx in pending:
            conf = ctx["conf"]
            eid = conf.get("entity_id")
            event = conf.get("event", "start")
            offset = conf.get("offset") or timedelta(0)
            if not isinstance(offset, timedelta):
                offset = timedelta(seconds=float(offset))
            try:
                resp = await self.hass.services.async_call(
                    "calendar",
                    "get_events",
                    {
                        "entity_id": eid,
                        "start_date_time": dt_util.as_local(self.window.start - offset - timedelta(days=1)),
                        "end_date_time": dt_util.as_local(self.window.end - offset + timedelta(days=1)),
                    },
                    blocking=True,
                    return_response=True,
                )
            except Exception as err:  # noqa: BLE001
                self._error(ctx, ctx["idx"], f"could not query {eid}: {err}")
                continue
            occs: list[datetime] = []
            titles: list[str] = []
            for ev in (resp or {}).get(eid, {}).get("events", []):
                when = _parse_dt(ev.get(event))
                if when is None:
                    continue
                when = when + offset
                if self.window.start < when <= self.window.end:
                    occs.append(when)
                    titles.append(str(ev.get("summary")))
            res = self._scheduled(ctx, sorted(occs), f"Calendar {event} on {eid}")
            for f in res:
                f.details["events"] = titles
            out.extend(res)
        return out

    # ------------------------------------------------------------------ state-ish

    def _state_like(self, ctx: dict, entity_id: str, attribute: str | None, matcher) -> Finding | None:
        """Shared pre/post comparison for state-driven triggers.

        matcher(pre_val, post_val) -> bool | None (None = can't tell)
        """
        conf = ctx["conf"]
        have, pre_val = self._pre_value(entity_id, attribute)
        post_state = self.hass.states.get(entity_id)
        post_val = self._post_value(post_state, attribute)
        target = f"{entity_id}" + (f"[{attribute}]" if attribute else "")
        details = {
            "entity": entity_id,
            "attribute": attribute,
            "before": pre_val,
            "after": post_val,
            "before_captured_at": self.baseline.get("captured_at"),
        }
        if not have:
            return None if post_val is None else self._mk(
                ctx,
                FindingType.MISSED,
                Confidence.POSSIBLE,
                f"No pre-downtime baseline for {target}; now '{post_val}'. Can't tell if it changed.",
                details=details,
            )
        if post_val in UNKNOWN_STATES and attribute is None:
            # Its integration hasn't reported yet: that says nothing about the trigger.
            # Re-checked once it reports (see DowntimeAuditor's pending checks).
            self.deferred.append(
                {
                    "automation": ctx["entity_id"],
                    "name": ctx["name"],
                    "trigger_index": ctx["idx"],
                    "trigger_id": ctx["trigger_id"],
                    "platform": ctx["platform"],
                    "entity": entity_id,
                    "before": jsonable(pre_val),
                    "state": post_val,
                }
            )
            return None
        if pre_val == post_val:
            return None  # unchanged (a change-and-change-back during downtime is undetectable)

        match = matcher(pre_val, post_val)
        if match is False:
            return None

        changed_after = self._changed_after_start(post_state)
        if changed_after and ctx["fired_after_start"]:
            # The change landed after triggers were attached and the automation fired.
            return None

        has_for = conf.get("for") is not None
        if match is None:
            confd = Confidence.POSSIBLE
            why = "change detected but match could not be evaluated"
        elif has_for:
            confd = Confidence.PROBABLE
            why = "matches, but the `for:` duration can't be verified across the downtime"
            details["for"] = jsonable(conf.get("for"))
        elif changed_after:
            confd = Confidence.PROBABLE
            why = (
                "value changed as integrations came back after startup; the automation did not "
                "fire, likely because the transition was seen as from 'unavailable'"
            )
        else:
            confd = CONFIRMED_IF_CLEAN[self.window.clean]
            why = "transition matches the trigger"
        details["last_changed"] = jsonable(post_state.last_changed) if post_state else None
        return self._mk(
            ctx,
            FindingType.MISSED,
            confd,
            f"{target} changed '{pre_val}' → '{post_val}' during downtime ({why})",
            details=details,
        )

    def _t_state(self, ctx: dict) -> list[Finding]:
        conf = ctx["conf"]
        attribute = conf.get("attribute")
        frm, to = conf.get("from"), conf.get("to")
        not_from = as_list(conf.get("not_from"))
        not_to = as_list(conf.get("not_to"))

        def matcher(pre: Any, post: Any) -> bool:
            if not schedule.state_matches(pre, frm) or not schedule.state_matches(post, to):
                return False
            if pre in not_from or post in not_to:
                return False
            return True

        out = []
        for eid in as_list(conf.get("entity_id")):
            out.append(self._state_like(ctx, eid, attribute, matcher))
        return out

    def _resolve_bound(self, value: Any) -> tuple[Any, str | None]:
        if isinstance(value, str) and "." in value:
            st = self.hass.states.get(value)
            return (st.state if st else None), value
        return value, None

    def _t_numeric_state(self, ctx: dict) -> list[Finding]:
        conf = ctx["conf"]
        attribute = conf.get("attribute")
        above, above_ent = self._resolve_bound(conf.get("above"))
        below, below_ent = self._resolve_bound(conf.get("below"))
        src = template_source(conf)
        out = []
        for eid in as_list(conf.get("entity_id")):
            if src:
                key = f"{ctx['entity_id']}#{ctx['idx']}#{eid}"
                pre_t = self.pre_templates.get(key)
                post_t = render_number(self.hass, src, self.hass.states.get(eid))
                if pre_t is None:
                    continue
                pre_in = schedule.in_numeric_range(pre_t, above, below)
                post_in = schedule.in_numeric_range(post_t, above, below)
                if pre_in is False and post_in:
                    out.append(
                        self._mk(
                            ctx,
                            FindingType.MISSED,
                            Confidence.PROBABLE,
                            f"value_template for {eid} crossed into range ({pre_t} → {post_t}) during downtime",
                            details={"before": pre_t, "after": post_t, "above": above, "below": below},
                        )
                    )
                continue

            def matcher(pre: Any, post: Any) -> bool | None:
                pre_in = schedule.in_numeric_range(pre, above, below)
                post_in = schedule.in_numeric_range(post, above, below)
                if post_in is None:
                    return False
                if not post_in:
                    return False
                if pre_in is None:
                    return None  # was non-numeric; HA would fire on next numeric update
                return not pre_in

            f = self._state_like(ctx, eid, attribute, matcher)
            if f is not None:
                f.details.update({"above": above, "below": below})
                if above_ent or below_ent:
                    f.details["threshold_entities"] = [e for e in (above_ent, below_ent) if e]
                    f.details["threshold_note"] = "Threshold comes from an entity; its current value was used."
            out.append(f)
        return out

    def _t_template(self, ctx: dict) -> list[Finding]:
        src = template_source(ctx["conf"])
        if not src:
            return []
        key = f"{ctx['entity_id']}#{ctx['idx']}"
        pre = self.pre_templates.get(key)
        post = render_bool(self.hass, src)
        details = {"template": src, "before": pre, "after": post}
        if post is not True:
            return []
        if pre is None:
            return [
                self._mk(
                    ctx,
                    FindingType.MISSED,
                    Confidence.POSSIBLE,
                    "Template is true now, but its value before the downtime is unknown.",
                    details=details,
                )
            ]
        if pre is False:
            if ctx["fired_after_start"]:
                return []
            return [
                self._mk(
                    ctx,
                    FindingType.MISSED,
                    Confidence.PROBABLE if ctx["conf"].get("for") else CONFIRMED_IF_CLEAN[self.window.clean],
                    "Template went false → true during downtime. HA doesn't fire a template "
                    "trigger that is already true at startup, so this run was skipped.",
                    details=details,
                )
            ]
        return []

    def _t_zone(self, ctx: dict) -> list[Finding]:
        conf = ctx["conf"]
        zone_id = conf.get("zone")
        event = conf.get("event")
        zone_state = self.hass.states.get(zone_id) if zone_id else None
        zone_name = "home" if zone_id == "zone.home" else (
            zone_state.attributes.get("friendly_name") if zone_state else None
        )

        def matcher(pre: Any, post: Any) -> bool | None:
            if zone_name is None:
                return None
            if event == "enter":
                return pre != zone_name and post == zone_name
            if event == "leave":
                return pre == zone_name and post != zone_name
            return None

        out = []
        for eid in as_list(conf.get("entity_id")):
            f = self._state_like(ctx, eid, None, matcher)
            if f is not None:
                f.details["zone"] = zone_id
                f.details["event"] = event
                if f.confidence in (Confidence.CONFIRMED, Confidence.PROBABLE):
                    f.confidence = Confidence.POSSIBLE  # zone name matching is approximate
            out.append(f)
        return out

    def _t_homeassistant(self, ctx: dict) -> list[Finding]:
        return []  # start/shutdown triggers fire as part of the restart itself

    def _t_device(self, ctx: dict) -> list[Finding]:
        conf = ctx["conf"]
        eid = conf.get("entity_id")
        if isinstance(eid, str) and "." not in eid:
            try:
                from homeassistant.helpers import entity_registry as er

                eid = er.async_resolve_entity_id(er.async_get(self.hass), eid)
            except Exception:  # noqa: BLE001
                eid = None
        if not eid:
            return self._t_generic(ctx)

        def matcher(pre: Any, post: Any) -> bool | None:
            return None  # device trigger semantics vary per integration

        f = self._state_like(ctx, eid, None, matcher)
        if f is not None:
            f.confidence = Confidence.POSSIBLE  # device trigger semantics vary per integration
            f.summary = (
                f"Device trigger '{conf.get('type')}' on {eid}: state changed "
                f"'{f.details.get('before')}' → '{f.details.get('after')}' during downtime"
            )
        return [f]

    def _t_generic(self, ctx: dict) -> list[Finding]:
        """Any other platform: compare referenced entities, else a missed trigger of unknown confidence."""
        conf = ctx["conf"]
        platform = ctx["platform"]
        entities = sorted(trigger_entities(self.hass, conf))
        out: list[Finding | None] = []
        if entities:
            for eid in entities:
                f = self._state_like(ctx, eid, None, lambda pre, post: None)
                if f is not None:
                    f.summary = f"[{platform}] {f.summary}"
                out.append(f)
            return out
        reason = LOST_MESSAGE_REASONS.get(
            platform,
            f"'{platform}' triggers fire on messages or events; any while Home Assistant was down are lost.",
        )
        if reason is None:
            return []
        details = {"reason": reason}
        if platform == "event":
            details["event_type"] = jsonable(conf.get("event_type"))
        if platform == "mqtt":
            details["topic"] = jsonable(conf.get("topic"))
        if platform == "webhook":
            details["webhook_id"] = "(hidden)"
        return [self._mk(ctx, FindingType.MISSED, Confidence.UNKNOWN, reason, details=details)]


def interrupted_findings(running: dict | None, window: Window, clean: bool) -> list[Finding]:
    """Turn the pre-downtime running snapshot into findings."""
    if not running:
        return []
    out: list[Finding] = []
    captured = _parse_dt(running.get("captured_at"))
    # Measure progress up to when the snapshot was taken (== shutdown for a clean stop).
    ref = captured or window.start
    for rec in running.get("runs", []):
        runs = rec.get("runs") or []
        lines = []
        for run in runs:
            start = _parse_dt((run.get("timestamp") or {}).get("start"))
            elapsed = max(0.0, (ref - start).total_seconds()) if start else None
            step = run.get("last_step")
            step_cfg = run.get("last_step_config")
            result = run.get("last_step_result") or {}
            desc = f"at step {step}" if step else "at an unknown step"
            if isinstance(step_cfg, dict):
                if "delay" in step_cfg:
                    desc += f" (delay {step_cfg['delay']})"
                elif "wait_template" in step_cfg or "wait_for_trigger" in step_cfg:
                    desc += " (waiting)"
                elif "action" in step_cfg or "service" in step_cfg:
                    desc += f" ({step_cfg.get('action') or step_cfg.get('service')})"
            if isinstance(result, dict) and "delay" in result and result.get("done") is False:
                step_ts = _parse_dt(run.get("last_step_timestamp"))
                if step_ts:
                    done_s = max(0.0, (ref - step_ts).total_seconds())
                    desc += f", {schedule.fmt_duration(done_s)} of {schedule.fmt_duration(float(result['delay']))} elapsed"
            if elapsed is not None:
                desc += f"; had been running {schedule.fmt_duration(elapsed)}"
            if run.get("trigger"):
                desc += f"; triggered by {run['trigger']}"
            desc += "; this step and everything after it did not run"
            lines.append(desc)
        if not lines:
            lines.append(f"{rec.get('current_runs', 1)} run(s) in progress (no trace detail available)")
        summary = "Was running when Home Assistant went down: " + " | ".join(lines)
        if not clean and captured:
            summary += f" (as of last snapshot {_local(captured)})"
        out.append(
            Finding(
                type=FindingType.INTERRUPTED,
                confidence=CONFIRMED_IF_CLEAN[clean],
                entity_id=rec.get("entity_id"),
                name=rec.get("name") or rec.get("entity_id"),
                summary=summary,
                platform=rec.get("domain"),
                count=rec.get("current_runs"),
                details={"mode": rec.get("mode"), "runs": runs},
                item_id=rec.get("item_id"),
            )
        )
    return out
