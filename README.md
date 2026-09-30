# Downtime Auditor for Home Assistant

After every restart, crash or power loss, Downtime Auditor tells you:

- **Interrupted:** every automation and script that was mid-run when HA went down. It shows the step it was stuck on (for example `delay 00:10:00`), how far it had got, and what triggered it.
- **Missed:** every automation trigger that would have fired while HA was down. This covers time, time pattern, sun, calendar, state, numeric state, template and zone triggers.
- **Possibly missed:** matches that can't be confirmed, such as `for:` durations, device triggers, entities still `unavailable`, and triggers with no baseline.
- **Fired during startup:** automations that fired in the first N seconds after boot. These are often spurious `unavailable → on` transitions.
- **Unverifiable:** event, webhook, MQTT, tag and conversation triggers. Anything sent to these while HA was down is lost and can't be reconstructed.

It only reports. It never re-runs anything.

Results show up in four places:

1. **A sidebar dashboard** (admins only) with four tabs:
   - **Last report:** a summary, category tiles you can click to filter, a timeline of the downtime showing each missed time, and a searchable list of findings. Each finding expands to show before/after values, due times and the step a run stopped at, with links to the automation's editor and traces.
   - **History:** every restart, with a duration chart. Click any entry to open that report.
   - **Live status:** heartbeat health and what's running right now, i.e. what *would* be interrupted if HA stopped this instant.
   - **What-if:** pick any time window and see which scheduled triggers a downtime then would miss.
2. **Settings → Repairs:** one issue per interrupted or missed finding, the way Spook raises its issues. Previous issues are replaced by each new report. You can ignore them individually or clear them with a service.
3. **Entities** (Watchman-style) on a *Downtime Auditor* device: one count sensor per category, with the findings in a `findings` attribute (excluded from the recorder), plus duration and timestamp sensors and problem binary sensors.
4. **JSON history** under `/config/downtime_auditor/`, plus an optional phone push. A persistent notification is also available but is off by default.

![Dashboard](docs/dashboard.png)

| History | Repairs | Mobile |
|---|---|---|
| ![History](docs/history.png) | ![Repairs](docs/repairs.png) | ![Mobile](docs/mobile.png) |

## How it works

| Phase | What happens |
|---|---|
| While running | Every *heartbeat* (default 60 s), it saves to `.storage`: the state of every entity your triggers reference, the current result of every template trigger, each automation's on/off state, and every automation or script run in progress (read from the trace system, including the current step and its delay or wait result). It also saves whenever an automation or script starts or finishes. |
| Clean shutdown | A HA *shutdown job* runs **before** integrations and scripts are stopped. It takes a final snapshot, records the exact shutdown time, and then freezes the snapshot so HA cancelling those runs can't overwrite the record of what was interrupted. |
| Crash or power loss | No shutdown job runs. The window start is the last heartbeat, and the report is marked **unclean**. If an automation's `last_triggered` shows it ran after the last heartbeat, it isn't reported as missed. |
| Boot | It waits for `homeassistant_started` (the moment automations re-attach their triggers). It then waits the *startup settle delay* so integrations can restore real states, and analyses the window `[shutdown or last heartbeat → started]`. |

### Per-trigger logic

| Trigger | How it's checked | Confidence |
|---|---|---|
| `time` (fixed, `input_datetime`, timestamp `sensor`, offset, `weekday`) | Computes every occurrence in the window, using the helper's value from **before** the downtime. | high |
| `time_pattern` | Replicates HA's pattern and defaulting rules and counts the matches. | high |
| `sun` | Sunrise/sunset (+offset) for each day in the window. | high |
| `calendar` | Calls `calendar.get_events` for the window (+offset). | high |
| `state` | Compares before vs. after, honouring `from`, `to`, `not_from`, `not_to` and `attribute`. | high; medium with `for:` |
| `numeric_state` | Detects a crossing from outside the range to inside it. Supports `attribute`, `value_template` and entity thresholds. | high |
| `template` | Compares the stored result before downtime with the result now. False → true is a miss, because HA never fires a template trigger that is already true at startup. | high |
| `zone` | Enter/leave, based on the person/tracker state before and after. | medium |
| `device`, new entity-style triggers | Reports any change in the referenced entity. | medium |
| `event`, `webhook`, `mqtt`, `tag`, `conversation`, … | Listed as unverifiable. | – |
| `homeassistant` start/shutdown | Ignored (they fire as part of the restart). | – |

Other details:

- Blueprint automations are analysed with their inputs substituted.
- Disabled triggers (`enabled: false`) are skipped.
- Automations that were **off** before the downtime are skipped.
- **Conditions are not evaluated.** A "missed" trigger might not have passed its conditions.

Known blind spots:

- A state that changed **and changed back** during the outage is invisible, because nothing was recorded while HA was down.
- After an unclean stop, anything between the last heartbeat and the crash has a ±heartbeat margin of error.

## Install (HACS custom repository)

1. Push this folder to a GitHub repo, e.g. `ha-downtime-auditor`.
2. Replace `YOUR_GITHUB_USERNAME` in `custom_components/downtime_auditor/manifest.json`.
3. In HACS, open **⋮ → Custom repositories**, add the repo URL with type **Integration**, then install **Downtime Auditor**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration → Downtime Auditor**.

The first boot only starts tracking. Reports begin from the **next** restart. The sidebar dashboard appears immediately.

Manual install: copy `custom_components/downtime_auditor` into `/config/custom_components/`.

## Entities

All entities belong to the **Downtime Auditor** device.

| Entity | State | Attributes |
|---|---|---|
| `sensor.downtime_auditor_interrupted_automations` | count | `findings` (list), `window`, `report_generated`, `truncated` |
| `sensor.downtime_auditor_missed_triggers` | count | same |
| `sensor.downtime_auditor_possibly_missed_triggers` | count | same |
| `sensor.downtime_auditor_fired_during_startup` | count | same |
| `sensor.downtime_auditor_unverifiable_triggers` | count | same |
| `sensor.downtime_auditor_last_downtime_duration` | duration | `clean_shutdown`, `counts`, `actionable`, HA version before/after, `json_path` |
| `sensor.downtime_auditor_last_downtime_start` / `_end` | timestamp | |
| `sensor.downtime_auditor_last_report` | timestamp | |
| `sensor.downtime_auditor_last_heartbeat` (diagnostic, disabled by default) | timestamp | `tracking`, `running_now` |
| `binary_sensor.downtime_auditor_needs_attention` | on if anything was interrupted/missed/possibly missed | |
| `binary_sensor.downtime_auditor_last_shutdown_unclean` | on after a crash or power loss | |

Each item in `findings` includes `name`, `entity_id`, `summary`, `confidence`, `platform`, the trigger id or index, and, where relevant, `count` and `first_due`. Up to 50 items are stored per sensor. The attribute is excluded from the recorder so it doesn't bloat your database.

Example Markdown card for your own dashboard:

```yaml
type: markdown
content: >
  {% set m = state_attr('sensor.downtime_auditor_missed_triggers', 'findings') or [] %}
  **Last downtime:** {{ states('sensor.downtime_auditor_last_downtime_duration') }} min
  {% for f in m %}
  - **{{ f.name }}** — {{ f.summary }}
  {% endfor %}
```

## Options

| Option | Default | Notes |
|---|---|---|
| Sidebar dashboard | on | Adds a *Downtime Auditor* entry to the sidebar (admins only). |
| Create Repairs issues | on | One issue per interrupted automation or missed trigger. |
| Repairs for "possibly missed" too | off | |
| Persistent notification | off | Posts the markdown report to the notification panel. |
| Push notify service | blank | `notify.mobile_app_xxx`, or a script that accepts `title` and `message` variables. |
| Push only when something was found | on | |
| Write JSON reports | on | Writes `reports/report-*.json`, `last_report.json` and `history.jsonl`. The History tab needs this. |
| JSON reports to keep | 100 | `history.jsonl` is never pruned. |
| Heartbeat interval | 60 s | Lower gives better crash accuracy but more disk writes. |
| Startup settle delay | 90 s | Increase if Zigbee/Z-Wave/cloud entities take a while to come back. |
| Track scripts | on | Report scripts that were interrupted. |
| List unverifiable triggers | on | |
| Maximum window | 14 d | Caps the analysis after a very long outage. |

## Services

- `downtime_auditor.analyze_window` takes `start` and `end` (and optionally `notify`). It runs a **what-if** analysis for schedule-based triggers ("what would a 2am–6am outage miss?") and returns the report as a response. It's handy for sanity-checking results without restarting.
- `downtime_auditor.resend_last_report` shows the last report again and re-sends the push.
- `downtime_auditor.snapshot_now` forces a snapshot and returns what's currently running. Useful for debugging.
- `downtime_auditor.dismiss_repairs` removes every Repairs issue raised by the latest report.

## Event

After every analysis it fires `downtime_auditor_report` with `window`, `counts`, `actionable` and `json_path`. You can use it to build your own follow-ups:

```yaml
triggers:
  - trigger: event
    event_type: downtime_auditor_report
conditions:
  - "{{ trigger.event.data.counts.interrupted | default(0) > 0 }}"
actions:
  - action: notify.mobile_app_phone
    data:
      message: "{{ trigger.event.data.counts.interrupted }} automation(s) were interrupted by the restart"
```

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```

The tests run a real HA core. They cover:

- a full shutdown → outage → boot cycle
- a crash/heartbeat cycle
- the real `homeassistant_started` boot path and a real `async_stop` with a run in progress
- entities, Repairs issues and their replacement
- every websocket command, including path-traversal rejection
- sidebar panel registration and removal
- the what-if service and the config flow

The dashboard is a plain web component (`frontend/panel.js`, no build step) that talks to the `downtime_auditor/*` websocket commands.

The icon shows as a broken image in Repairs and on the integration page until a brand icon is submitted to [home-assistant/brands](https://github.com/home-assistant/brands). This is normal for custom integrations.

## Compatibility

Home Assistant 2024.11 or newer; tested against 2026.2. The integration reads two semi-internal structures: automation trigger config and trace data. If a future HA release changes them, it degrades to less detail rather than failing.
