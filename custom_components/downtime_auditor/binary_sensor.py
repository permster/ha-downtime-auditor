"""Binary sensors for Downtime Auditor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_REPORT_UPDATED
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    auditor = hass.data[DOMAIN]
    async_add_entities([NeedsAttention(auditor, entry), UncleanShutdown(auditor, entry)])


class _Base(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, auditor: Any, entry: ConfigEntry, key: str, name: str) -> None:
        self.auditor = auditor
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_REPORT_UPDATED, self._update)
        )

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class NeedsAttention(_Base):
    """On when the last report has interrupted/missed/possibly-missed findings."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, auditor: Any, entry: ConfigEntry) -> None:
        super().__init__(auditor, entry, "needs_attention", "Needs attention")

    @property
    def is_on(self) -> bool | None:
        rep = self.auditor.last_report
        return None if rep is None else rep.get("actionable", 0) > 0


class UncleanShutdown(_Base):
    """On when the last downtime was a crash / power loss rather than a clean stop."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:flash-alert-outline"

    def __init__(self, auditor: Any, entry: ConfigEntry) -> None:
        super().__init__(auditor, entry, "unclean_shutdown", "Last shutdown unclean")

    @property
    def is_on(self) -> bool | None:
        rep = self.auditor.last_report
        return None if rep is None else not rep["window"]["clean_shutdown"]
