// Smoke test for frontend/panel.js: render every tab with real report data,
// exercise filters / state cache / links / rating, and assert on the HTML.
// No browser needed. Run by tests/test_panel_smoke.py, which generates the
// fixture from a real backend run:
//   node tests/frontend/panel_smoke.js <fixture.json> [panel.js]
const fs = require("fs");
const vm = require("vm");
const path = require("path");
const assert = require("assert");

const [fixturePath, panelArg] = process.argv.slice(2);
if (!fixturePath) {
  console.error("usage: node panel_smoke.js <fixture.json> [panel.js]");
  process.exit(2);
}
const panelPath = panelArg || path.join(__dirname, "../../custom_components/downtime_auditor/frontend/panel.js");
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const src = fs.readFileSync(panelPath, "utf8");

function makeEnv(storage) {
  const defined = {};
  class FakeRoot {
    constructor() { this.innerHTML = ""; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
  }
  class HTMLElement {
    attachShadow() { this.shadowRoot = new FakeRoot(); return this.shadowRoot; }
    dispatchEvent(e) { (this.events ||= []).push(e); }
  }
  const sent = [];
  const ctx = {
    HTMLElement,
    customElements: { define: (n, c) => { defined[n] = c; } },
    window: { sessionStorage: storage, dispatchEvent() {}, ResizeObserver: undefined },
    history: { pushState() {} },
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o?.detail; } },
    setInterval: () => 1, clearInterval() {}, setTimeout, clearTimeout,
    console, Date, JSON, Math, Object, String, Number, Boolean, Array, Set, encodeURIComponent,
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const Panel = defined["downtime-auditor-panel"];
  const hass = {
    connection: {
      sendMessagePromise: async (msg) => {
        sent.push(msg);
        const t = msg.type.split("/")[1];
        if (t === "report") return fixture.report;
        if (t === "history") return fixture.history;
        if (t === "status") return fixture.status;
        if (t === "what_if") return fixture.whatif;
        if (t === "set_severity") return { entity_id: msg.entity_id, severity: msg.severity };
        if (t === "create_severity_labels") return { created: ["downtime_auditor_sev: none"] };
        throw new Error("unexpected " + msg.type);
      },
      subscribeEvents: async () => () => {},
    },
  };
  return { Panel, hass, sent, ctx };
}

function memoryStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), map: m };
}

const tick = () => new Promise((r) => setImmediate(r));

(async () => {
  const storage = memoryStorage();
  let { Panel, hass, sent, ctx } = makeEnv(storage);
  const STATE = vm.runInContext("STATE", ctx);
  const p = new Panel();
  p.hass = hass;
  for (let i = 0; i < 5; i++) await tick();
  let html = p.shadowRoot.innerHTML;

  // ---- report tab: severity is the colored element; confidence is a neutral meter + word
  assert(html.includes("Highest severity: High"), "hero shows highest severity");
  assert(!html.includes("Possibly missed") && !html.includes("Unverifiable"), "old categories gone");
  for (const t of ["Interrupted", "Missed", "Fired at startup"]) assert(html.includes(`<div class="l">${t}</div>`), "tile " + t);
  assert((html.match(/class="sev-badge"/g) || []).length >= 3, "a severity badge per row");
  assert(html.includes('class="tag conf"') && html.includes("Confirmed") && html.includes("Unknown"), "confidence word");
  assert(!/conf-(high|medium)/.test(html), "no colored confidence classes");
  assert(html.includes("conditions?"), "likely-fail conditions flagged");
  assert(html.includes("Minimum severity") && html.includes("Show severity None"), "filter bar");
  assert(!html.includes("Conditions are not evaluated"), "old foot removed");

  // ---- expand a row: severity reason, picker, conditions tree, real links
  const morning = fixture.report.findings.find((f) => f.name === "Morning");
  const key = p._key(fixture.report, morning);
  const saved = JSON.parse(storage.getItem("downtime_auditor_panel_v1"));
  assert.deepStrictEqual(saved.types, ["interrupted", "missed", "fired_at_startup"], "state mirrored to sessionStorage");
  STATE.open.push(key);
  p._render();
  html = p.shadowRoot.innerHTML;
  assert(html.includes("Probably failed — based on pre-downtime values"), "condition headline");
  assert(html.includes('class="cond-tree"') && html.includes("<b>or</b>"), "condition breakdown tree");
  assert(html.includes('data-rate="automation.morning"') && html.includes("Unrated (Medium)"), "severity picker");
  assert(/href="\/config\/automation\/edit\/a" data-nav/.test(html), "real Edit link");
  assert(/href="\/config\/automation\/trace\/a" data-nav/.test(html), "real Traces link");
  const pattern = fixture.report.findings.find((f) => f.name === "Pattern");
  STATE.open.push(p._key(fixture.report, pattern));
  p._render();
  html = p.shadowRoot.innerHTML;
  assert(/<option value="high" selected>High<\/option>/.test(html), "picker shows the label rating");

  // ---- filters
  STATE.minSeverity = "high";
  p._render();
  html = p.shadowRoot.innerHTML;
  assert(html.includes("(1 of 3)"), "min severity filter: " + html.match(/\(\d+ of \d+\)/)?.[0]);
  STATE.minSeverity = "all";
  STATE.confidences = ["confirmed"];
  p._render();
  assert(p.shadowRoot.innerHTML.includes("(2 of 3)"), "confidence filter");
  STATE.confidences = ["confirmed", "probable", "possible", "unknown"];
  STATE.types = ["interrupted"];
  p._render();
  assert(p.shadowRoot.innerHTML.includes("(0 of 3)"), "type filter");
  STATE.types = ["interrupted", "missed", "fired_at_startup"];

  // severity None hidden unless shown
  const withNone = JSON.parse(JSON.stringify(fixture.report));
  withNone.findings[0].severity = "none";
  p._report = withNone;
  p._render();
  assert(p.shadowRoot.innerHTML.includes("(2 of 3)"), "None hidden by default");
  STATE.showNone = true;
  p._render();
  assert(p.shadowRoot.innerHTML.includes("(3 of 3)") && /class="row [^"]*dim"/.test(p.shadowRoot.innerHTML), "None shown (dimmed) when toggled");
  STATE.showNone = null;
  p._report = fixture.report;

  // ---- rating from the dashboard calls set_severity and reloads
  await p._rate("automation.morning", "critical");
  const last = sent.filter((m) => m.type.endsWith("set_severity")).pop();
  assert.deepStrictEqual({ e: last.entity_id, s: last.severity }, { e: "automation.morning", s: "critical" });
  await p._rate("automation.morning", null);
  assert.strictEqual(sent.filter((m) => m.type.endsWith("set_severity")).pop().severity, null);

  // ---- history: legacy marker, severity column, new types
  p._setTab("history");
  for (let i = 0; i < 3; i++) await tick();
  html = p.shadowRoot.innerHTML;
  assert(html.includes(">v0.4</span>"), "legacy marker");
  assert(html.includes("Highest</th>") && html.includes('<span class="muted">—</span>'), "severity column with — for v0.4");
  assert(!html.includes("possibly_missed"), "no old keys");

  // ---- live tab: labels button
  p._setTab("live");
  for (let i = 0; i < 3; i++) await tick();
  html = p.shadowRoot.innerHTML;
  assert(html.includes('data-act="create-labels"') && html.includes("Repairs: <b>High</b>"), "labels card");
  await p._createLabels();
  assert(p.shadowRoot.innerHTML.includes("Created: downtime_auditor_sev: none"), "create labels notice");

  // ---- what-if: results kept in the state cache across a new panel instance
  STATE.wiStart = "2026-10-05T01:00"; STATE.wiEnd = "2026-10-05T07:00";
  p._setTab("whatif");
  await p._runWhatIf();
  assert(p.shadowRoot.innerHTML.includes("kept while this browser tab is open"), "what-if note");
  p.disconnectedCallback();

  // Same browser tab, panel re-created (e.g. back from the automation editor):
  ({ Panel, hass } = makeEnv(storage)); // fresh module (like a reload) reading sessionStorage
  const p2 = new Panel();
  p2.hass = hass;
  for (let i = 0; i < 5; i++) await tick();
  html = p2.shadowRoot.innerHTML;
  assert(html.includes('class="tab active" data-tab="whatif"'), "tab restored");
  assert(html.includes("Simulated window"), "what-if results restored");

  // Storage that throws (private mode) must not break rendering
  const broken = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } };
  ({ Panel, hass } = makeEnv(broken));
  const p3 = new Panel();
  p3.hass = hass;
  for (let i = 0; i < 5; i++) await tick();
  assert(p3.shadowRoot.innerHTML.includes("Highest severity"), "renders without storage");

  // ---- 0.5.1: stale-page banner, pending analysis, live status, dense timeline
  {
    const statusFor = (extra) => ({ ...fixture.status, ...extra });
    let status = statusFor({ version: "9.9.9" });
    const env = makeEnv(memoryStorage());
    const send = env.hass.connection.sendMessagePromise;
    let statusCalls = 0;
    // A frequent trigger (every 15 s over the window): only the first 200 due times are stored.
    const dense = JSON.parse(JSON.stringify(fixture.report));
    const freq = dense.findings.find((f) => f.occurrences?.length);
    const t0 = new Date(dense.window.start).getTime();
    freq.count = 481;
    freq.occurrences_iso = Array.from({ length: 200 }, (_, i) => new Date(t0 + (i + 1) * 15000).toISOString());
    env.hass.connection.sendMessagePromise = async (msg) => {
      if (msg.type.endsWith("/status")) { statusCalls += 1; return status; }
      if (msg.type.endsWith("/report")) return dense;
      return send(msg);
    };
    const S = vm.runInContext("STATE", env.ctx);
    const q = new env.Panel();
    q.panel = { config: { version: "0.5.0" } }; // the version this page's code was loaded with
    q.hass = env.hass;
    for (let i = 0; i < 5; i++) await tick();
    let page = q.shadowRoot.innerHTML;
    assert(page.includes("was updated to 9.9.9") && page.includes('data-act="reload"'), "stale-page banner");
    assert(page.includes('class="count">×481</text>'), "frequent trigger drawn as a span with its count");
    const span = page.match(/<line x1="([\d.]+)" x2="([\d.]+)"[^>]*stroke-dasharray/);
    const axisEnd = Number(page.match(/class="axis"/) && page.match(/<line x1="[\d.]+" x2="([\d.]+)" y1="\d+" y2="\d+" class="axis"/)[1]);
    assert(span && Math.abs(Number(span[2]) - axisEnd) < 1, "a capped list runs to the window end, not halfway");

    status = statusFor({ version: "0.5.0", tracking: false,
      pending: { state: "settling", due_at: new Date(Date.now() + 42000).toISOString(), startup_delay: 90 } });
    await q._loadStatus();
    page = q.shadowRoot.innerHTML;
    assert(!page.includes("was updated to"), "no stale banner when versions match");
    assert(/Checking what was missed in <b>4[12] s<\/b>/.test(page), "pending countdown: " + page.match(/Checking[^<]*<b>[^<]*/)?.[0]);
    assert(page.includes("The report below is from the previous restart."), "says the shown report is the old one");
    status = statusFor({ version: "0.5.0", tracking: false, pending: { state: "starting", due_at: null, startup_delay: 90 } });
    await q._loadStatus();
    assert(q.shadowRoot.innerHTML.includes("Home Assistant is still starting"), "starting banner");

    // Live status: elapsed time + step, and a refresh as soon as a run starts.
    status = statusFor({ version: "0.5.0", pending: null, running_now: [{
      entity_id: "automation.wake_up_routine", name: "Wake-up routine", domain: "automation", item_id: "wake_up",
      mode: "parallel", current_runs: 1,
      runs: [{ last_step: "action/1", last_step_config: { delay: "00:20:00" },
               timestamp: { start: new Date(Date.now() - 125000).toISOString() } }],
    }] });
    q._setTab("live"); // like a click: records what's running now
    await q._loadStatus();
    page = q.shadowRoot.innerHTML;
    assert(/running 2m [56]s · at action\/1 \(delay &quot;00:20:00&quot;\)/.test(page), "live elapsed: " + page.match(/running [^<]*/)?.[0]);
    assert(page.includes('class="live-dot"'), "live indicator");
    // The very first change after opening the tab must count (this was missed once).
    const before = statusCalls;
    q.hass = { ...env.hass, states: { "automation.wake_up_routine": { attributes: { current: 1 } } } };
    await new Promise((r) => setTimeout(r, 300));
    assert(statusCalls > before, "a run starting refreshes Live status right away");
    const mid = statusCalls;
    q.hass = { ...env.hass, states: { "automation.wake_up_routine": { attributes: { current: 1 } } } };
    await new Promise((r) => setTimeout(r, 300));
    assert(statusCalls === mid, "unrelated updates don't refetch");
  }

  console.log("PANEL SMOKE OK");
})().catch((e) => { console.error("PANEL SMOKE FAILED:", e.message); process.exit(1); });

