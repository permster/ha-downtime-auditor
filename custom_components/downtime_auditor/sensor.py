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
    CAT_INTERRUPTED,
    CAT_MISSED,
    CAT_POSSIBLE,
    CAT_STARTUP_FIRED,
    CAT_UNVERIFIABLE,
    DOMAIN,
    NAME,
    PANEL_URL,
    SIGNAL_REPORT_UPDATED,
)

MAX_ATTR_FINDINGS = 50

CATEGORY_SENSORS = [
    (CAT_INTERRUPTED, "Interrupted automations", "mdi:motion-pause-outline"),
    (CAT_MISSED, "Missed triggers", "mdi:calendar-remove-outline"),
    (CAT_POSSIBLE, "Possibly missed triggers", "mdi:help-circle-outline"),
    (CAT_STARTUP_FIRED, "Fired during startup", "mdi:rocket-launch-outline"),
    (CAT_UNVERIFIABLE, "Unverifiable triggers", "mdi:eye-off-outline"),
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
        "confidence": finding.get("confidence"),
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
    return out


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    auditor = hass.data[DOMAIN]
    entities: list[SensorEntity] = [
        CategorySensor(auditor, entry, cat, name, icon) for cat, name, icon in CATEGORY_SENSORS
    ]
    entities += [
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


class CategorySensor(_Base):
    """State = number of findings in a category; attribute `findings` = the list."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "findings"
    _unrecorded_attributes = frozenset({"findings", "window"})

    def __init__(self, auditor: Any, entry: ConfigEntry, category: str, name: str, icon: str) -> None:
        super().__init__(auditor, entry, category, name)
        self.category = category
        self._attr_icon = icon

    def _items(self) -> list[dict]:
        rep = self.report
        if not rep:
            return []
        return [f for f in rep.get("findings", []) if f.get("category") == self.category]

    @property
    def native_value(self) -> int | None:
        return None if self.report is None else len(self._items())

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
            "actionable": rep.get("actionable"),
            "counts": rep.get("counts"),
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
