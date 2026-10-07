"""Config + options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

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
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    NAME,
    Severity,
)
from .auditor import DEFAULTS


def _schema(values: dict[str, Any]) -> vol.Schema:
    def d(key: str) -> Any:
        return values.get(key, DEFAULTS[key])

    def num(lo: int, hi: int, unit: str | None = None) -> selector.NumberSelector:
        cfg: dict[str, Any] = {"min": lo, "max": hi, "mode": selector.NumberSelectorMode.BOX}
        if unit:
            cfg["unit_of_measurement"] = unit
        return selector.NumberSelector(selector.NumberSelectorConfig(**cfg))

    # Thresholds: None is never a threshold (it means "no impact").
    severities = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[s.value for s in Severity if s != Severity.NONE],
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="severity",
        )
    )

    return vol.Schema(
        {
            vol.Required(CONF_SIDEBAR_PANEL, default=d(CONF_SIDEBAR_PANEL)): bool,
            vol.Required(CONF_REPAIRS, default=d(CONF_REPAIRS)): bool,
            vol.Required(CONF_REPAIRS_MIN_SEVERITY, default=d(CONF_REPAIRS_MIN_SEVERITY)): severities,
            vol.Required(CONF_PERSISTENT_NOTIFICATION, default=d(CONF_PERSISTENT_NOTIFICATION)): bool,
            vol.Optional(CONF_NOTIFY_SERVICE, default=d(CONF_NOTIFY_SERVICE)): str,
            vol.Required(CONF_PUSH_MIN_SEVERITY, default=d(CONF_PUSH_MIN_SEVERITY)): severities,
            vol.Required(CONF_PUSH_ONLY_ON_FINDINGS, default=d(CONF_PUSH_ONLY_ON_FINDINGS)): bool,
            vol.Required(CONF_SHOW_SEVERITY_NONE, default=d(CONF_SHOW_SEVERITY_NONE)): bool,
            vol.Required(CONF_UNCONFIRMABLE_SEVERITY, default=d(CONF_UNCONFIRMABLE_SEVERITY)): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[Severity.LOW.value, Severity.NONE.value],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="unconfirmable",
                )
            ),
            vol.Required(CONF_WRITE_JSON, default=d(CONF_WRITE_JSON)): bool,
            vol.Required(CONF_REPORT_DAYS, default=d(CONF_REPORT_DAYS)): vol.All(
                num(1, 365, "d"), vol.Coerce(int)
            ),
            vol.Required(CONF_HISTORY_DAYS, default=d(CONF_HISTORY_DAYS)): vol.All(
                num(7, 3650, "d"), vol.Coerce(int)
            ),
            vol.Required(CONF_HEARTBEAT_INTERVAL, default=d(CONF_HEARTBEAT_INTERVAL)): vol.All(
                num(10, 3600, "s"), vol.Coerce(int)
            ),
            vol.Required(CONF_STARTUP_DELAY, default=d(CONF_STARTUP_DELAY)): vol.All(
                num(0, 1800, "s"), vol.Coerce(int)
            ),
            vol.Required(CONF_INCLUDE_SCRIPTS, default=d(CONF_INCLUDE_SCRIPTS)): bool,
            vol.Required(CONF_MAX_WINDOW_DAYS, default=d(CONF_MAX_WINDOW_DAYS)): vol.All(
                num(1, 90, "d"), vol.Coerce(int)
            ),
        }
    )


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    svc = (user_input.get(CONF_NOTIFY_SERVICE) or "").strip()
    if svc and (svc.count(".") != 1 or svc.split(".")[0] not in ("notify", "script")):
        errors[CONF_NOTIFY_SERVICE] = "invalid_service"
    return errors


class DowntimeAuditorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Single-instance config flow."""

    VERSION = CONFIG_ENTRY_VERSION

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(title=NAME, data={}, options=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DowntimeAuditorOptionsFlow()


class DowntimeAuditorOptionsFlow(OptionsFlow):
    """Options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)
        values = {**self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="init", data_schema=_schema(values), errors=errors)
