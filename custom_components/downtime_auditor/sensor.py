"""Sensors holding the latest downtime report (Watchman-style)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    NAME,
    PANEL_URL,
    SIGNAL_REPORT_UPDATED,
    FindingType,
    Severity,
)
from .severity import at_least

MAX_ATTR_FINDINGS = 50

# Unique-id keys match v0.4 for interrupted/missed; fired_at_startup is migrated
# from its v0.4 unique id in async_migrate_entry.
TYPE_SENSORS = [
    (FindingType.INTERRUPTED, "Interrupted automations", "mdi:motion-pause-outline"),
    (FindingType.MISSED, "Missed triggers", "mdi:calendar-remove-outline"),
    (FindingType.FIRED_AT_STARTUP, "Fired at startup", "mdi:rocket-launch-outline"),
]


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=NAME,
        manufacturer="Downtime Auditor",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=f"homeassistant://{PANEL_URL}",
    )


def compact(finding: dict) -> dict:
    """Small per-finding record for entity attributes."""
    out = {
        "name": finding.get("name"),
        "entity_id": finding.get("entity_id"),
        "summary": finding.get("summary"),
        "severity": finding.get("severity"),
        "confidence": finding.get("confidence"),
        "conditions": finding.get("conditions"),
        "platform": finding.get("platform"),
    }
    if finding.get("trigger_id") is not None:
        out["trigger_id"] = finding["trigger_id"]
    elif finding.get("trigger_index") is not None:
        out["trigger_index"] = finding["trigger_index"]
    if finding.get("count") is not None:
        out["count"] = finding["count"]
    if finding.get("occurrences"):
        out["first_due"] = finding["occurrences"][0]
    if len(finding.get("triggers") or []) > 1:
        out["triggers"] = len(finding["triggers"])
    return out


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    auditor = hass.data[DOMAIN]
    entities: list[SensorEntity] = [
        TypeSensor(auditor, entry, ftype, name, icon) for ftype, name, icon in TYPE_SENSORS
    ]
    entities += [
        HighestSeveritySensor(auditor, entry),
        DowntimeDurationSensor(auditor, entry),
        TimestampSensor(auditor, entry, "downtime_start", "Last downtime start", "start"),
        TimestampSensor(auditor, entry, "downtime_end", "Last downtime end", "end"),
        LastReportSensor(auditor, entry),
        HeartbeatSensor(auditor, entry),
    ]
    async_add_entities(entities)


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, auditor: Any, entry: ConfigEntry, key: str, name: str) -> None:
        self.auditor = auditor
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info(entry)

    @property
    def report(self) -> dict | None:
        return self.auditor.last_report

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_REPORT_UPDATED, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class TypeSensor(_Base):
    """State = findings of this type with severity Low or higher; `findings` lists all of them."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "findings"
    _unrecorded_attributes = frozenset({"findings", "window"})

    def __init__(self, auditor: Any, entry: ConfigEntry, finding_type: str, name: str, icon: str) -> None:
        super().__init__(auditor, entry, finding_type, name)
        self.finding_type = finding_type
        self._attr_icon = icon

    def _items(self) -> list[dict]:
        rep = self.report
        if not rep:
            return []
        return [f for f in rep.get("findings", []) if f.get("type") == self.finding_type]

    @property
    def native_value(self) -> int | None:
        if self.report is None:
            return None
        return sum(1 for f in self._items() if at_least(f.get("severity"), Severity.LOW))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rep = self.report
        items = self._items()
        return {
            "report_generated": rep.get("generated_at") if rep else None,
            "window": (rep or {}).get("window"),
            "truncated": len(items) > MAX_ATTR_FINDINGS,
            "findings": [compact(f) for f in items[:MAX_ATTR_FINDINGS]],
        }


class HighestSeveritySensor(_Base):
    """Most severe finding in the last report ('none' when nothing was found)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in Severity]
    _attr_translation_key = "highest_severity"
    _attr_icon = "mdi:timeline-check-outline"

    def __init__(self, auditor: Any, entry: ConfigEntry) -> None:
        super().__init__(auditor, entry, "highest_severity", "Highest severity")

    @property
    def native_value(self) -> str | None:
        rep = self.report
        if rep is None:
            return None
        return rep.get("highest_severity") or Severity.NONE.value


class DowntimeDurationSensor(_Base):
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.MINUTES
    _attr_icon = "mdi:timer-alert-outline"

    def __init__(self, auditor: Any, entry: ConfigEntry) -> None:
        super().__init__(auditor, entry, "downtime_duration", "Last downtime duration")

    @property
    def native_value(self) -> float | None:
        rep = self.report
        return rep["window"]["duration_seconds"] if rep else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rep = self.report
        if not rep:
            return {}
        w = rep["window"]
        meta = rep.get("meta") or {}
        return {
            "clean_shutdown": w.get("clean_shutdown"),
            "start_basis": w.get("start_basis"),
            "ha_version_before": meta.get("ha_version_before"),
            "ha_version_after": meta.get("ha_version_after"),
            "needs_attention": rep.get("needs_attention"),
            "actionable": rep.get("needs_attention"),  # deprecated; removed in v0.6.0
            "highest_severity": rep.get("highest_severity"),
            "counts": rep.get("counts"),
            "counts_by_severity": rep.get("counts_by_severity"),
            "skipped": rep.get("skipped"),
            "json_path": meta.get("json_path"),
        }


class TimestampSensor(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, auditor: Any, entry: ConfigEntry, key: str, name: str, field: str) -> None:
        super().__init__(auditor, entry, key, name)
        self.field = field
        self._attr_icon = "mdi:power-plug-off-outline" if field == "start" else "mdi:power-plug-outline"

    @property
    def native_value(self) -> datetime | None:
        rep = self.report
        return dt_util.parse_datetime(rep["window"][self.field]) if rep else None


class LastReportSensor(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clipboard-text-clock-outline"

    def __init__(self, auditor: Any, entry: ConfigEntry) -> None:
        super().__init__(auditor, entry, "last_report", "Last report")

    @property
    def native_value(self) -> datetime | None:
        rep = self.report
        return dt_util.parse_datetime(rep["generated_at"]) if rep else None


class HeartbeatSensor(_Base):
    """When the auditor last persisted its snapshot."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:heart-pulse"
    _attr_entity_registry_enabled_default = False  # updates every heartbeat

    def __init__(self, auditor: Any, entry: ConfigEntry) -> None:
        super().__init__(auditor, entry, "last_heartbeat", "Last heartbeat")

    @property
    def native_value(self) -> datetime | None:
        sess = (self.auditor.data or {}).get("session") or {}
        return dt_util.parse_datetime(sess["last_heartbeat"]) if sess.get("last_heartbeat") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        running = (self.auditor.data or {}).get("running") or {}
        return {
            "tracking": self.auditor.active,
            "running_now": len(running.get("runs", [])),
        }
