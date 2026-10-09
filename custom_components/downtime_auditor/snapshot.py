"""Capture what we need *before* downtime so it can be compared afterwards.

Everything here is defensive: it touches a few semi-internal Home Assistant
structures (automation trigger config, trace data). If those change in a
future HA release the auditor degrades to less detail instead of failing.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.template import Template, result_as_boolean
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

AUTOMATION_DOMAIN = "automation"
SCRIPT_DOMAIN = "script"


# --------------------------------------------------------------------------
# Automation / trigger discovery
# --------------------------------------------------------------------------


def automation_entities(hass: HomeAssistant) -> list[Any]:
    """Return live AutomationEntity objects (empty list if unavailable)."""
    component = hass.data.get(AUTOMATION_DOMAIN)
    try:
        return list(component.entities) if component is not None else []
    except Exception:  # noqa: BLE001
        return []


def trigger_configs(entity: Any) -> list[dict]:
    """Validated trigger config for an automation (blueprints already expanded).

    HA 2026.9+ keeps the fields of integration-provided triggers (sun, zone, ...)
    under `options`; they're lifted to the top level so every HA version reads the same.
    """
    confs = getattr(entity, "_trigger_config", None)
    if confs is None:
        raw = getattr(entity, "raw_config", None) or {}
        confs = raw.get("triggers") or raw.get("trigger") or []
        if isinstance(confs, dict):
            confs = [confs]
    out = []
    for conf in confs:
        if not isinstance(conf, dict):
            continue
        if isinstance(opts := conf.get("options"), dict):
            conf = {**{k: v for k, v in conf.items() if k != "options"}, **opts}
        out.append(conf)
    return out


def trigger_platform(conf: dict) -> str:
    """Trigger platform key ('state', 'time', 'sun', ...)."""
    return str(conf.get("platform") or conf.get("trigger") or "unknown")


def trigger_enabled(conf: dict) -> bool:
    """Whether a trigger is enabled (templated 'enabled' treated as enabled)."""
    enabled = conf.get("enabled", True)
    return enabled if isinstance(enabled, bool) else True


def as_list(value: Any) -> list:
    """Coerce to list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def trigger_entities(hass: HomeAssistant, conf: dict) -> set[str]:
    """Every entity id a trigger depends on."""
    found: set[str] = set()
    try:
        from homeassistant.helpers import trigger as trigger_helper

        found |= set(trigger_helper.async_extract_entities(conf))
    except Exception:  # noqa: BLE001
        pass
    for key in ("entity_id", "zone"):
        for val in as_list(conf.get(key)):
            if isinstance(val, str) and "." in val:
                found.add(val)
    for key in ("above", "below"):
        val = conf.get(key)
        if isinstance(val, str) and "." in val:
            found.add(val)
    for at in as_list(conf.get("at")):
        if isinstance(at, str) and "." in at:
            found.add(at)
        elif isinstance(at, dict) and isinstance(at.get("entity_id"), str):
            found.add(at["entity_id"])
    return found


def trigger_attributes(conf: dict) -> set[str]:
    """Attributes a trigger watches."""
    attr = conf.get("attribute")
    return {attr} if isinstance(attr, str) else set()


def template_source(conf: dict) -> str | None:
    """Raw template string of a template trigger."""
    tpl = conf.get("value_template")
    if tpl is None:
        return None
    return getattr(tpl, "template", None) or str(tpl)


def render_bool(hass: HomeAssistant, source: str, variables: dict | None = None) -> bool | None:
    """Render a template to a boolean, None on error."""
    try:
        result = Template(source, hass).async_render(variables or {}, parse_result=False)
        return result_as_boolean(result)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Template baseline render failed (%s): %s", source, err)
        return None


def render_number(hass: HomeAssistant, source: str, state: State | None) -> Any:
    """Render a numeric_state value_template with `state` bound."""
    try:
        return Template(source, hass).async_render({"state": state}, parse_result=False)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Serialisation helpers
# --------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    """Best-effort conversion to JSON-safe primitives."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if hasattr(value, "template"):
        return str(value.template)
    return str(value)


def state_record(state: State | None, attributes: set[str]) -> dict | None:
    """Minimal serializable state record."""
    if state is None:
        return None
    rec: dict[str, Any] = {
        "state": state.state,
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
        "attributes": {},
    }
    for attr in attributes:
        if attr in state.attributes:
            rec["attributes"][attr] = jsonable(state.attributes[attr])
    return rec


# --------------------------------------------------------------------------
# Baseline capture
# --------------------------------------------------------------------------


def capture_baseline(hass: HomeAssistant) -> dict:
    """Capture entity states, template results and automation meta.

    Entities read by automation *conditions* are included too, so conditions can
    be checked against pre-downtime values.
    """
    from .conditions import automation_conditions, condition_entities  # noqa: PLC0415  (import cycle)

    entities: dict[str, set[str]] = {}
    templates: dict[str, bool | None] = {}
    automations: dict[str, dict] = {}

    for ent in automation_entities(hass):
        entity_id = getattr(ent, "entity_id", None)
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        automations[entity_id] = {
            "name": getattr(ent, "name", None) or entity_id,
            "enabled": state.state == "on" if state else None,
            "last_triggered": jsonable(state.attributes.get("last_triggered")) if state else None,
            "unique_id": getattr(ent, "unique_id", None),
        }
        for idx, conf in enumerate(trigger_configs(ent)):
            platform = trigger_platform(conf)
            for eid in trigger_entities(hass, conf):
                entities.setdefault(eid, set()).update(trigger_attributes(conf))
            if platform == "template" and (src := template_source(conf)):
                templates[f"{entity_id}#{idx}"] = render_bool(hass, src)
            if platform == "numeric_state" and (src := template_source(conf)):
                # Store the rendered value for each entity so we can compare.
                for eid in as_list(conf.get("entity_id")):
                    val = render_number(hass, src, hass.states.get(eid))
                    templates[f"{entity_id}#{idx}#{eid}"] = jsonable(val)
        try:
            for eid, attrs in condition_entities(hass, automation_conditions(ent)).items():
                entities.setdefault(eid, set()).update(attrs)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not collect condition entities for %s", entity_id, exc_info=True)

    return {
        "captured_at": dt_util.utcnow().isoformat(),
        "entities": {
            eid: state_record(hass.states.get(eid), attrs) for eid, attrs in entities.items()
        },
        "templates": templates,
        "automations": automations,
    }


# --------------------------------------------------------------------------
# Running automations / scripts
# --------------------------------------------------------------------------


def _running_traces(hass: HomeAssistant) -> dict[tuple[str, str], list[dict]]:
    """Map (domain, item_id) -> list of running trace summaries."""
    out: dict[tuple[str, str], list[dict]] = {}
    data = hass.data.get("trace")
    if not isinstance(data, dict):
        return out
    for traces in data.values():
        # HA 2026.9+: TraceData(runs, not_triggered); earlier: a dict of run_id -> trace.
        bucket = getattr(traces, "runs", traces)
        try:
            items = list(bucket.values())
        except Exception:  # noqa: BLE001
            continue
        for trace in items:
            try:
                short = trace.as_short_dict()
            except Exception:  # noqa: BLE001
                continue
            if short.get("state") != "running":
                continue
            detail = dict(jsonable(short))
            # Pull the result of the step it's sitting on (delay/wait info).
            try:
                ext = trace.as_extended_dict()
                steps = ext.get("trace") or {}
                last = short.get("last_step")
                if last and last in steps and steps[last]:
                    last_item = steps[last][-1]
                    detail["last_step_result"] = jsonable(last_item.get("result"))
                    detail["last_step_timestamp"] = jsonable(last_item.get("timestamp"))
                ctx = ext.get("context")
                detail["context_id"] = jsonable(
                    ctx.get("id") if isinstance(ctx, dict) else getattr(ctx, "id", None)
                )
                cfg = ext.get("config") or {}
                if last and cfg:
                    detail["last_step_config"] = jsonable(_resolve_path(cfg, last))
            except Exception:  # noqa: BLE001
                pass
            key = (str(short.get("domain")), str(short.get("item_id")))
            out.setdefault(key, []).append(detail)
    return out


def _resolve_path(config: Any, path: str) -> Any:
    """Resolve a trace path like 'action/2/sequence/0' inside a config."""
    node = config
    for part in path.split("/"):
        if isinstance(node, dict):
            if part in node:
                node = node[part]
            elif part == "action" and "actions" in node:
                node = node["actions"]
            elif part == "sequence" and "sequence" in node:
                node = node["sequence"]
            else:
                return None
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    if isinstance(node, dict):
        # Keep it short: drop nested sequences
        return {k: v for k, v in node.items() if k not in ("sequence", "then", "else", "default")}
    return node


def capture_running(hass: HomeAssistant, include_scripts: bool) -> list[dict]:
    """Return a record for every automation/script run currently in progress."""
    traces = _running_traces(hass)
    runs: list[dict] = []
    now = dt_util.utcnow()

    domains = [AUTOMATION_DOMAIN] + ([SCRIPT_DOMAIN] if include_scripts else [])
    unique_ids: dict[str, str] = {}
    for ent in automation_entities(hass):
        if getattr(ent, "entity_id", None):
            unique_ids[ent.entity_id] = str(getattr(ent, "unique_id", None))

    for domain in domains:
        for state in hass.states.async_all(domain):
            current = state.attributes.get("current") or 0
            try:
                current = int(current)
            except (TypeError, ValueError):
                current = 0
            if current <= 0:
                continue
            item_id = (
                unique_ids.get(state.entity_id)
                if domain == AUTOMATION_DOMAIN
                else state.entity_id.split(".", 1)[1]
            )
            trace_list = traces.get((domain, str(item_id)), [])
            record = {
                "entity_id": state.entity_id,
                "item_id": item_id,
                "domain": domain,
                "name": state.attributes.get("friendly_name") or state.entity_id,
                "mode": state.attributes.get("mode"),
                "current_runs": current,
                "last_triggered": jsonable(state.attributes.get("last_triggered")),
                "captured_at": now.isoformat(),
                "runs": trace_list,
            }
            runs.append(record)
    return runs
