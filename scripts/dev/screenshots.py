"""Take the README screenshots from the sandbox HA (scripts/dev/sandbox.sh demo).

Runs on Windows (or anywhere with a Chromium-family browser) with Playwright:
    py -3.13 -m venv %USERPROFILE%\\.venvs\\da-screenshots
    %USERPROFILE%\\.venvs\\da-screenshots\\Scripts\\pip install playwright
    %USERPROFILE%\\.venvs\\da-screenshots\\Scripts\\python scripts/dev/screenshots.py [out_dir]

Uses the installed Edge (channel "msedge"), so no browser download is needed;
set DA_BROWSER_CHANNEL=chrome (or "" for Playwright's bundled Chromium) otherwise.
Logs in with the sandbox's saved tokens (auth.json), read from WSL by default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

from playwright.sync_api import Page, sync_playwright

REPO = Path(__file__).resolve().parents[2]
AUTH = Path(os.environ.get("DA_SANDBOX_AUTH", r"\\wsl$\Ubuntu\home\%s\da-sandbox\auth.json" % os.environ.get("DA_WSL_USER", os.environ.get("USERNAME", "").lower())))
CHANNEL = os.environ.get("DA_BROWSER_CHANNEL", "msedge") or None
DESKTOP = {"width": 1400, "height": 1000}
MOBILE = {"width": 420, "height": 1000}


def tokens() -> dict:
    """A fresh access token plus what the HA frontend keeps in localStorage."""
    auth = json.loads(AUTH.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": auth["refresh_token"], "client_id": auth["client_id"]}
    ).encode()
    with urllib.request.urlopen(f"{auth['hass_url']}/auth/token", body, timeout=10) as r:
        tok = json.loads(r.read())
    return {
        "hassUrl": auth["hass_url"],
        "clientId": auth["client_id"],
        "access_token": tok["access_token"],
        "refresh_token": auth["refresh_token"],
        "token_type": "Bearer",
        "expires_in": tok["expires_in"],
        "expires": int(time.time() * 1000) + tok["expires_in"] * 1000,
    }


def open_panel(page: Page, url: str) -> None:
    page.goto(url)
    page.get_by_text("Highest severity").first.wait_for(timeout=30000)
    page.wait_for_timeout(800)  # icons and fonts


def shoot(page: Page, path: Path) -> None:
    page.screenshot(path=str(path))
    print("wrote", path)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs"
    out.mkdir(parents=True, exist_ok=True)
    hass_tokens = tokens()
    base = hass_tokens["hassUrl"]
    init = f"""
      if (location.origin === {json.dumps(base)}) {{
        localStorage.setItem("hassTokens", {json.dumps(json.dumps(hass_tokens))});
        localStorage.setItem("selectedLanguage", '"en"');
        sessionStorage.removeItem("downtime_auditor_panel_v1");
      }}"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=CHANNEL, headless=True)
        try:
            ctx = browser.new_context(viewport=DESKTOP, color_scheme="light", locale="en-US")
            ctx.add_init_script(init)
            page = ctx.new_page()

            # Last report, with the most severe finding expanded.
            open_panel(page, f"{base}/downtime-auditor")
            page.locator(".row-head", has_text="Garage left open alert").first.click()
            page.wait_for_timeout(300)
            shoot(page, out / "dashboard.png")

            # History tab
            page.locator(".tab", has_text="History").click()
            page.locator("table.tbl").wait_for()
            page.wait_for_timeout(500)
            shoot(page, out / "history.png")

            # Settings → Repairs
            page.goto(f"{base}/config/repairs")
            page.get_by_text("Missed during downtime").first.wait_for(timeout=30000)
            page.wait_for_timeout(800)
            shoot(page, out / "repairs.png")
            ctx.close()

            # Mobile: the condition breakdown of a finding that probably failed its conditions.
            ctx = browser.new_context(viewport=MOBILE, color_scheme="light", locale="en-US",
                                      device_scale_factor=1, is_mobile=True, has_touch=True)
            ctx.add_init_script(init)
            page = ctx.new_page()
            open_panel(page, f"{base}/downtime-auditor")
            row = page.locator(".row-head", has_text="Night alarm arm").first
            row.click()
            page.wait_for_timeout(300)
            row.evaluate("el => el.scrollIntoView({block: 'start'})")
            page.wait_for_timeout(300)
            shoot(page, out / "mobile.png")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
