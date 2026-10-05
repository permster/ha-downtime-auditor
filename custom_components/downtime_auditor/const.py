"""Constants for Downtime Auditor."""

from __future__ import annotations

DOMAIN = "downtime_auditor"
NAME = "Downtime Auditor"

STORAGE_KEY = f"{DOMAIN}.state"
STORAGE_VERSION = 1

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
CONF_INCLUDE_UNVERIFIABLE = "include_unverifiable"
CONF_MAX_WINDOW_DAYS = "max_window_days"
CONF_SIDEBAR_PANEL = "sidebar_panel"
CONF_REPAIRS = "repairs"
CONF_REPAIRS_POSSIBLE = "repairs_possible"

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
DEFAULT_INCLUDE_UNVERIFIABLE = True
DEFAULT_MAX_WINDOW_DAYS = 14
DEFAULT_SIDEBAR_PANEL = True
DEFAULT_REPAIRS = True
DEFAULT_REPAIRS_POSSIBLE = False

SIGNAL_REPORT_UPDATED = f"{DOMAIN}_report_updated"
PANEL_URL = "downtime-auditor"
PANEL_STATIC_URL = f"/{DOMAIN}_static"
PANEL_COMPONENT = "downtime-auditor-panel"

# Finding categories
CAT_INTERRUPTED = "interrupted"
CAT_MISSED = "missed"
CAT_POSSIBLE = "possibly_missed"
CAT_UNVERIFIABLE = "unverifiable"
CAT_STARTUP_FIRED = "fired_during_startup"
CAT_SKIPPED = "skipped"

CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

EVENT_REPORT = f"{DOMAIN}_report"
PERSISTENT_NOTIFICATION_ID = f"{DOMAIN}_report"

SERVICE_ANALYZE_WINDOW = "analyze_window"
SERVICE_RESEND_LAST = "resend_last_report"
SERVICE_SNAPSHOT_NOW = "snapshot_now"
SERVICE_DISMISS_REPAIRS = "dismiss_repairs"

# States that mean "we don't really know"
UNKNOWN_STATES = ("unknown", "unavailable", None)
