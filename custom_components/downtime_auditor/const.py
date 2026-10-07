"""Constants for Downtime Auditor."""

from __future__ import annotations

from enum import StrEnum

DOMAIN = "downtime_auditor"
NAME = "Downtime Auditor"

STORAGE_KEY = f"{DOMAIN}.state"
STORAGE_VERSION = 1
CONFIG_ENTRY_VERSION = 2

REPORT_DIR = DOMAIN  # under /config
HISTORY_FILE = "history.jsonl"

# Options
CONF_HEARTBEAT_INTERVAL = "heartbeat_interval"
CONF_STARTUP_DELAY = "startup_delay"
CONF_PERSISTENT_NOTIFICATION = "persistent_notification"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_PUSH_ONLY_ON_FINDINGS = "push_only_on_findings"
CONF_WRITE_JSON = "write_json"
CONF_REPORT_DAYS = "report_retention_days"
CONF_HISTORY_DAYS = "history_retention_days"
CONF_INCLUDE_SCRIPTS = "include_scripts"
CONF_MAX_WINDOW_DAYS = "max_window_days"
CONF_SIDEBAR_PANEL = "sidebar_panel"
CONF_REPAIRS = "repairs"
CONF_REPAIRS_MIN_SEVERITY = "repairs_min_severity"
CONF_PUSH_MIN_SEVERITY = "push_min_severity"
CONF_SHOW_SEVERITY_NONE = "show_severity_none"
# Severity for triggers that can't be confirmed (event, webhook, MQTT, tag, conversation, ...): low | none
CONF_UNCONFIRMABLE_SEVERITY = "unconfirmable_severity"

# Options removed in config entry version 2 (dropped by async_migrate_entry).
LEGACY_OPTIONS = ("repairs_possible", "include_unverifiable", "retention")

DEFAULT_HEARTBEAT_INTERVAL = 60  # seconds
DEFAULT_STARTUP_DELAY = 90  # seconds
DEFAULT_PERSISTENT_NOTIFICATION = False
DEFAULT_NOTIFY_SERVICE = ""
DEFAULT_PUSH_ONLY_ON_FINDINGS = True
DEFAULT_WRITE_JSON = True
DEFAULT_REPORT_DAYS = 30
DEFAULT_HISTORY_DAYS = 365
MAX_REPORT_FILES = 500  # safety cap so a restart loop can't fill the disk
DEFAULT_INCLUDE_SCRIPTS = True
DEFAULT_MAX_WINDOW_DAYS = 14
DEFAULT_SIDEBAR_PANEL = True
DEFAULT_REPAIRS = True
DEFAULT_REPAIRS_MIN_SEVERITY = "high"
DEFAULT_PUSH_MIN_SEVERITY = "high"
DEFAULT_SHOW_SEVERITY_NONE = False
DEFAULT_UNCONFIRMABLE_SEVERITY = "low"

SIGNAL_REPORT_UPDATED = f"{DOMAIN}_report_updated"
PANEL_URL = "downtime-auditor"
PANEL_STATIC_URL = f"/{DOMAIN}_static"
PANEL_COMPONENT = "downtime-auditor-panel"

# --------------------------------------------------------------------------
# Terminology: the only source for these words (see docs/plans).
# A finding has three independent attributes: type, confidence, severity.
# --------------------------------------------------------------------------


class FindingType(StrEnum):
    """What happened."""

    INTERRUPTED = "interrupted"
    MISSED = "missed"
    FIRED_AT_STARTUP = "fired_at_startup"


class Confidence(StrEnum):
    """How sure we are it happened (CVSS Report Confidence style). Never colored."""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    POSSIBLE = "possible"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    """How much it matters (CVSS qualitative scale). The only colored attribute."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


TYPE_LABELS = {
    FindingType.INTERRUPTED: "Interrupted",
    FindingType.MISSED: "Missed",
    FindingType.FIRED_AT_STARTUP: "Fired at startup",
}
CONFIDENCE_LABELS = {
    Confidence.CONFIRMED: "Confirmed",
    Confidence.PROBABLE: "Probable",
    Confidence.POSSIBLE: "Possible",
    Confidence.UNKNOWN: "Unknown",
}
CONFIDENCE_LEVEL = {  # bars on the 4-step meter
    Confidence.CONFIRMED: 4,
    Confidence.PROBABLE: 3,
    Confidence.POSSIBLE: 2,
    Confidence.UNKNOWN: 1,
}
SEVERITY_LABELS = {
    Severity.CRITICAL: "Critical",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
    Severity.NONE: "None",
}
SEVERITY_RANK = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.NONE: 0,
}
# Home Assistant palette names (used for the label colors too).
SEVERITY_COLORS = {
    Severity.CRITICAL: "red",
    Severity.HIGH: "orange",
    Severity.MEDIUM: "amber",
    Severity.LOW: "blue-grey",
    Severity.NONE: "grey",
}
DEFAULT_SEVERITY = Severity.MEDIUM

SEVERITY_SOURCE_LABEL = "label"
SEVERITY_SOURCE_TRIGGER = "trigger"  # rated for one trigger on the dashboard
SEVERITY_SOURCE_DEFAULT = "default"

SEVERITY_LABEL_PREFIX = "downtime_auditor_sev"

EVENT_REPORT = f"{DOMAIN}_report"
EVENT_REPORT_UPDATED = f"{DOMAIN}_report_updated"  # the last report changed after it was published
PERSISTENT_NOTIFICATION_ID = f"{DOMAIN}_report"

SERVICE_ANALYZE_WINDOW = "analyze_window"
SERVICE_RESEND_LAST = "resend_last_report"
SERVICE_SNAPSHOT_NOW = "snapshot_now"
SERVICE_DISMISS_REPAIRS = "dismiss_repairs"
SERVICE_CREATE_SEVERITY_LABELS = "create_severity_labels"

# States that mean "we don't really know"
UNKNOWN_STATES = ("unknown", "unavailable", None)
