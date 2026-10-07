# Dev sandbox

A throwaway Home Assistant for trying Downtime Auditor end to end, on the **latest HA release** (what users run). The unit tests pin an older HA; the sandbox catches what they can't, like HA changing an internal structure.

The integration is symlinked from this repo, so the sandbox always runs your working tree.

## Run it (Linux, or WSL on Windows)

```bash
scripts/dev/sandbox.sh demo
```

About 5 minutes the first time (it builds a venv), about 2 minutes after that. It:

1. creates a fresh HA in `~/da-sandbox` on port **8124**, onboards an owner (`demo` / `demo-sandbox`) through HA's onboarding API, and adds Downtime Auditor through its config flow;
2. writes demo automations: time and time-pattern triggers, state and numeric-state triggers, an event trigger, a long-running automation and script, conditions that pass, probably fail and fail, a disabled automation, and severity labels;
3. simulates three outages: a quick clean restart, a 25-minute crash (`kill -9`), and a clean 2-hour outage with runs in progress and states changed while "down". It does this by really stopping HA, moving the stored shutdown time back, and editing restore-state, then restarting.

After the 2-hour outage, a "slow integration" (`sensor.pool_pump`, which only exists while the script sets it through the REST API) reports 30 s late, which exercises the re-checks: one automation is dropped (HA fired it) and one is added (`from: off`, which HA doesn't fire on `unknown → on`). `ctl late-report on|off` simulates it by hand after a `restart`.

Then open http://localhost:8124/downtime-auditor.

Other commands: `start`, `stop [--unclean]`, `restart`, `status`, `logs`, `reset`, and `ctl <command>`. `ctl` drives HA through its API: `call`, `options startup_delay=90`, `late-report`, `report-id`, `wait-report`, `wait-tracking` and more; see the docstring in `sandbox_ctl.py`.

Settings: `DA_SANDBOX` (default `~/da-sandbox`), `DA_SANDBOX_PORT` (8124), `DA_SANDBOX_VENV`, and `DA_SANDBOX_HA_VERSION` (`latest`, or e.g. `2026.2.3`). The venv is only rebuilt when it's missing: `rm -rf ~/.venvs/da-sandbox` to pick up a new HA release.

Needs: [`uv`](https://docs.astral.sh/uv/) (installs the right Python for the chosen HA version) and `curl`.

## Screenshots

`screenshots.py` takes the four README images (`docs/*.png`) from the sandbox with Playwright, using your installed Edge, so there's no browser download. On Windows:

```powershell
py -3.13 -m venv $HOME\.venvs\da-screenshots
& $HOME\.venvs\da-screenshots\Scripts\pip install playwright
& $HOME\.venvs\da-screenshots\Scripts\python scripts/dev/screenshots.py          # writes docs/*.png
& $HOME\.venvs\da-screenshots\Scripts\python scripts/dev/screenshots.py out\dir  # or somewhere else first
```

It logs in with the sandbox's saved tokens (`~/da-sandbox/auth.json`, read through `\\wsl$`). Set `DA_BROWSER_CHANNEL=chrome` to use Chrome instead of Edge.

## Gotchas this handles

- HA's web server answers before HA has finished starting. The scripts wait for `RUNNING` (and for the integration to be tracking) before installing or stopping.
- A bare venv lacks packages that HA's frontend needs for its service list, and the UI hangs on "Loading data". Setup installs them, read from HA's own manifests.
- HA 2026.9+ applies YAML `http:` settings as *pending* until an admin confirms them, with a blocking dialog. The demo confirms them through HA's API (`ctl confirm-http`).
- Repairs that only exist because it's a sandbox (install method, YAML migration) are ignored (`ctl tidy`), so they stay out of screenshots.
- WSL ends background processes when the `wsl` call that started them exits, so HA is started with `setsid`.
- Calling `script.turn_on` for a single-mode script that's already running blocks the REST call on current HA. Stop it first.
