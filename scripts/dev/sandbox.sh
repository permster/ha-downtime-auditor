#!/usr/bin/env bash
# Throwaway Home Assistant for trying Downtime Auditor end to end (Linux / WSL).
# The integration is symlinked from this repo, so the sandbox always runs your working tree.
#
#   scripts/dev/sandbox.sh demo        fresh sandbox + three simulated outages (≈5 min)
#   scripts/dev/sandbox.sh start|stop [--unclean]|restart|status|logs|reset
#   scripts/dev/sandbox.sh ctl <command> ...   (see scripts/dev/sandbox_ctl.py)
#
# UI: http://localhost:8124  (user demo / demo-sandbox)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export DA_SANDBOX="${DA_SANDBOX:-$HOME/da-sandbox}"
export DA_SANDBOX_PORT="${DA_SANDBOX_PORT:-8124}"
CONFIG="$DA_SANDBOX/config"
VENV="${DA_SANDBOX_VENV:-$HOME/.venvs/da-sandbox}"
LOG="$DA_SANDBOX/ha.log"
PIDFILE="$DA_SANDBOX/ha.pid"
# The sandbox runs the latest HA release by default (what users run); the tests pin
# whatever Python 3.13 resolves to. Override with e.g. DA_SANDBOX_HA_VERSION=2026.2.3.
HA_VERSION="${DA_SANDBOX_HA_VERSION:-latest}"

log() { printf '\033[1m[sandbox]\033[0m %s\n' "$*"; }
ctl() { "$VENV/bin/python" "$HERE/sandbox_ctl.py" "$@"; }
running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

cmd_setup() {
  if [ ! -x "$VENV/bin/hass" ]; then
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null || { echo "needs uv: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
    local version="$HA_VERSION" pyver
    [ "$version" = latest ] && version="$(curl -fs https://pypi.org/pypi/homeassistant/json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])')"
    pyver="$(curl -fs "https://pypi.org/pypi/homeassistant/$version/json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["requires_python"].lstrip(">="))')"
    log "creating venv $VENV (HA $version, Python $pyver)"
    uv python install "$pyver" >/dev/null
    uv venv -q -p "$pyver" "$VENV"
    VIRTUAL_ENV="$VENV" uv pip install -q "homeassistant==$version"
    # The frontend version HA itself pins
    VIRTUAL_ENV="$VENV" uv pip install -q "$("$VENV/bin/python" -c 'import json, pathlib, homeassistant.components.frontend as f; print(json.loads((pathlib.Path(f.__file__).parent / "manifest.json").read_text())["requirements"][0])')"
  fi
  # The frontend's service list imports ~20 entity components (and their dependencies)
  # even when they aren't set up; without their packages the UI hangs on "Loading data".
  if [ ! -f "$VENV/.base-requirements" ]; then
    log "installing packages the HA frontend needs"
    local constraints reqs
    constraints="$("$VENV/bin/python" -c 'import homeassistant, pathlib; print(pathlib.Path(homeassistant.__file__).parent / "package_constraints.txt")')"
    reqs="$(mktemp)"
    ctl base-requirements > "$reqs"
    PATH="$HOME/.local/bin:$PATH" VIRTUAL_ENV="$VENV" uv pip install -q -c "$constraints" -r "$reqs"
    cp "$reqs" "$VENV/.base-requirements"; rm -f "$reqs"
  fi
  mkdir -p "$CONFIG/custom_components"
  cp "$HERE/sandbox/configuration.yaml" "$HERE/sandbox/scripts.yaml" "$CONFIG/"
  sed -i "s/server_port: .*/server_port: $DA_SANDBOX_PORT/" "$CONFIG/configuration.yaml"
  [ -f "$CONFIG/automations.yaml" ] || echo "[]" > "$CONFIG/automations.yaml"
  ln -sfn "$REPO/custom_components/downtime_auditor" "$CONFIG/custom_components/downtime_auditor"
}

cmd_start() {
  running && { log "already running (pid $(cat "$PIDFILE"))"; return; }
  log "starting HA on http://localhost:$DA_SANDBOX_PORT"
  # setsid: its own session, so it outlives the shell (and the `wsl` call) that started it.
  setsid nohup "$VENV/bin/hass" -c "$CONFIG" --log-file "$LOG" >"$DA_SANDBOX/stdout.log" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 300); do
    if curl -fs -o /dev/null "http://127.0.0.1:$DA_SANDBOX_PORT/manifest.json"; then log "up"; return; fi
    running || { tail -30 "$DA_SANDBOX/stdout.log" "$LOG" 2>/dev/null; echo "HA exited"; exit 1; }
    sleep 1
  done
  echo "HA didn't come up in 5 minutes; see $LOG"; exit 1
}

cmd_stop() {
  running || { log "not running"; return; }
  local pid; pid="$(cat "$PIDFILE")"
  if [ "${1:-}" = "--unclean" ]; then
    log "killing HA (simulated crash / power loss)"
    kill -KILL "$pid"
  else
    log "stopping HA cleanly"
    kill -TERM "$pid"
  fi
  for _ in $(seq 1 120); do kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE"; log "stopped"; return; }; sleep 1; done
  echo "HA didn't stop in 2 minutes"; exit 1
}

cmd_reset() { cmd_stop; log "deleting $DA_SANDBOX"; rm -rf "$DA_SANDBOX"; }

# One simulated outage: stop, pretend it began HOURS ago, optionally change states, start, wait for the report.
outage() {
  local hours="$1" mode="${2:-clean}" since
  since="$(ctl report-id)"   # wait for a report newer than the current one
  if [ "$mode" = unclean ]; then cmd_stop --unclean; ctl backdate "$hours" --unclean
  else cmd_stop; ctl backdate "$hours"; fi
  [ "${3:-}" = changes ] && ctl offline-changes
  cmd_start
  ctl wait-report "$since"
  ctl wait-tracking   # the report comes first; tracking (and the next baseline) right after
}

cmd_demo() {
  cmd_reset
  mkdir -p "$DA_SANDBOX"
  cmd_setup
  ctl automations
  cmd_start
  ctl onboard
  ctl wait-running
  ctl confirm-http
  ctl install
  ctl wait-tracking
  ctl rate
  log "outage 1: a quick clean restart"
  outage 0.05
  log "outage 2: a crash, 25 minutes"
  outage 0.4 unclean
  log "outage 3: a clean 2-hour outage with runs in progress and changes while down"
  ctl prepare
  outage 2 clean changes
  log "the slow pool 'integration' reports 30 s later (its triggers were pending)"
  sleep 30
  ctl late-report on
  sleep 8
  ctl tidy
  log "done: http://localhost:$DA_SANDBOX_PORT/downtime-auditor  (demo / demo-sandbox)"
}

case "${1:-}" in
  setup) cmd_setup ;;
  start) cmd_setup; cmd_start ;;
  stop) shift; cmd_stop "$@" ;;
  restart) cmd_stop; cmd_start ;;
  status) running && echo "running (pid $(cat "$PIDFILE")) http://localhost:$DA_SANDBOX_PORT" || echo "stopped" ;;
  logs) tail -n "${2:-100}" -f "$LOG" ;;
  reset) cmd_reset ;;
  demo) cmd_demo ;;
  ctl) shift; ctl "$@" ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
