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
        if (t === "set_trigger_severity") return { key: `${msg.item_id}|#${msg.trigger_index}:${msg.platform}`, severity: msg.severity };
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
  for (const t of ["Interrupted", "Missed", "Fired at startup", "Reverted"]) assert(html.includes(`<div class="l">${t}</div>`), "tile " + t);
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
  assert.deepStrictEqual(saved.types, ["interrupted", "missed", "fired_at_startup", "reverted"], "state mirrored to sessionStorage");
  STATE.open.push(key);
  p._render();
  html = p.shadowRoot.innerHTML;
  assert(html.includes("Probably wouldn") && html.includes("based on pre-downtime values"), "condition headline");
  assert(html.includes("<b>Conditions:</b> fail — state — input_boolean.vac"), "the deciding check, up front");
  assert(html.includes('class="cond-all"') && html.includes('class="cond-tree"') && html.includes("<b>or</b>"), "all checks, folded");

  // A choose whose options are named by alias / what they'd do; options for other triggers fold away.
  const opt = (n, result, extra = {}) => ({ condition: `choose option ${n}`, result, option: n, conditions: [], ...extra });
  const gate = p._conditions({ result: "pass", basis: "baseline", checked: 1, steps: [{
    condition: "actions", result: "pass", why: "would have run",
    conditions: [opt(1, "fail", { other_trigger: true }), opt(2, "unknown", { does: "light.turn_on light.porch" }),
                 opt(3, "pass", { alias: "Resync pool clock", does: "script.opnpool_resync" })],
    runs: { label: "choose", certain: false, options: [
      { option: 2, does: "light.turn_on light.porch", result: "unknown" },
      { option: 3, alias: "Resync pool clock", does: "script.opnpool_resync", result: "pass" }] } }] });
  assert(gate.includes("<b>Conditions:</b> none"), "no automation conditions");
  assert(gate.includes("would run option 2 → light.turn_on light.porch (can&#39;t be checked) or “Resync pool clock” (option 3) → script.opnpool_resync")
    || gate.includes("would run option 2 → light.turn_on light.porch (can't be checked) or “Resync pool clock” (option 3) → script.opnpool_resync"),
    "which option would run, by name: " + gate.match(/<b>Actions:<\/b>[^<]*/)?.[0]);
  assert(gate.includes("(1 option is for other triggers)") && gate.includes("— for another trigger"), "other triggers' options fold away");
  assert(html.includes('data-rate="automation.morning"') && html.includes("Unrated (starts at Medium)"), "severity picker");
  assert(/href="\/config\/automation\/edit\/a" data-nav/.test(html), "real Edit link");
  assert(/href="\/config\/automation\/trace\/a" data-nav/.test(html), "real Traces link");
  const pattern = fixture.report.findings.find((f) => f.name === "Pattern");
  STATE.open.push(p._key(fixture.report, pattern));
  p._render();
  html = p.shadowRoot.innerHTML;
  assert(/<option value="high" selected>High<\/option>/.test(html), "picker shows the label rating");
  assert(html.includes('<div class="k">Rate this trigger</div>'), "single-trigger findings can rate their trigger too");
  assert(html.includes('<div class="k">Trigger</div><div class="v"><div>Time pattern: every 30 minutes</div></div>'), "which trigger, in words");
  assert.strictEqual((html.match(/data-rate-trigger=/g) || []).length, 2, "one trigger selector each for Morning and Pattern");

  // ---- a multi-trigger automation is one row, with its triggers listed when expanded
  const evt = fixture.report.findings.find((f) => f.name === "Evt");
  assert.strictEqual(evt.triggers.length, 2);
  assert(html.includes('<span class="tag">2 triggers</span>'), "multi-trigger tag");
  assert.strictEqual((html.match(/<div class="row-title">Evt /g) || []).length, 1, "one row for the automation");
  STATE.open.push(p._key(fixture.report, evt));
  p._render();
  html = p.shadowRoot.innerHTML;
  assert(html.includes('<div class="k">Triggers (2)</div>'), "trigger list in the details");
  assert(html.includes('<div class="trig-what">Event: x</div><div class="muted small">trigger 1 of 2</div>')
    && html.includes('<div class="trig-what">Event: y</div><div class="muted small">trigger 2 of 2</div>'),
    "each listed trigger says which one it is");
  assert.strictEqual((html.match(/class="trig"/g) || []).length, 2, "both triggers listed");
  assert(html.includes("Medium (unrated) → Low: can&#39;t be confirmed, so capped at Low ·")
    || html.includes("Medium (unrated) → Low: can't be confirmed, so capped at Low ·"), "starting rating → result");
  assert(html.includes('<a href="#" data-tab="rules">How is this decided?</a>'), "link to the rules");
  assert.strictEqual((html.match(/data-rate-trigger=/g) || []).length, 4, "a rating selector per trigger (Evt's 2 + Morning's + Pattern's)");
  assert(html.includes("Automation&#39;s rating") || html.includes("Automation's rating"), "default = automation's rating");
  await p._rateTrigger({ entity_id: "automation.evt", item_id: "b", trigger_id: null, trigger_index: 1, platform: "event" }, "none");
  const trig = sent.filter((m) => m.type.endsWith("set_trigger_severity")).pop();
  assert.deepStrictEqual([trig.trigger_index, trig.platform, trig.severity], [1, "event", "none"], "trigger rating sent");

  // ---- the Rules tab: from the backend's rule list and the current options
  p._setTab("rules");
  await new Promise((r) => setTimeout(r, 0));
  html = p.shadowRoot.innerHTML;
  assert(html.includes("How severity is decided") && html.includes("otherwise Medium (unrated)"), "rules: starting rating");
  assert(html.includes("(Time patterns that run again soon: off)"), "rules: an option that's off says so");
  assert((html.match(/class="rule /g) || []).length === fixture.status.severity_rules.adjust.length, "rules: every adjustment listed");
  p._setTab("report");

  // ---- what it would have changed (live states), and Run actions now with its disclaimer
  const calls = [];
  p._hass.states = {
    "input_boolean.night": { state: "off", attributes: { friendly_name: "Night" } },
    "input_boolean.day": { state: "off", attributes: {} },
  };
  p._hass.callService = async (...args) => { calls.push(args); };
  const dn = {
    type: "missed", entity_id: "automation.day_night", name: "Day night", severity: "medium", confidence: "confirmed",
    summary: "Sun sunset was due", trigger_index: 0,
    details: { effects: { certain: true, by_trigger: true, items: [
      { entity_id: "input_boolean.night", action: "input_boolean.turn_on", expected: "on" },
      { entity_id: "input_boolean.day", action: "input_boolean.turn_off", expected: "off" }] } },
  };
  STATE.open.push("dn");
  let row = p._row(dn, "dn");
  assert(row.includes("Would have changed") && row.includes("⚠ expected on") && row.includes("✓ as expected"),
    "effects compared with live states");
  assert(row.includes('data-run="dn"') && !row.includes("run-confirm"), "run button, no confirmation yet");
  p._confirmRun = "dn";
  row = p._row(dn, "dn");
  assert(row.includes("may not do what you expect") && row.includes("Check the results afterwards"), "the disclaimer");
  assert(row.includes("trigger ids"), "warns when the actions depend on which trigger fired");
  await p._runNow("automation.day_night");
  assert.strictEqual(JSON.stringify(calls.pop()),
    JSON.stringify(["automation", "trigger", { entity_id: "automation.day_night", skip_condition: true }]), "runs the actions");
  assert(p.shadowRoot.innerHTML.includes("Check that it did what you expected"), "reminder after running");
  STATE.open.pop();

  // ---- a reverted state: live value, the lost change, who uses it, and Set back with a confirmation
  const rv = fixture.reverted;
  p._hass.states = { "input_boolean.vac": { state: "on", attributes: { friendly_name: "vac" } } };
  STATE.open.push("rv");
  row = p._row(rv, "rv");
  assert(row.includes("Now") && row.includes("⚠ expected off"), "live value compared with the one before the stop");
  assert(row.includes("Lost change") && row.includes("saved copy was from"), "when the change was made, and HA's older copy");
  assert(row.includes("Rate this entity") && row.includes('data-rate="input_boolean.vac"'), "the entity can be rated");
  assert(row.includes('data-run="rv"') && row.includes("Set back to off") && !row.includes("Run actions now"), "set back, not run");
  p._confirmRun = "rv";
  row = p._row(rv, "rv");
  assert(row.includes("Automations triggered by this change will run"), "set-back confirmation");
  await p._setBack(rv.details.set_back);
  assert.strictEqual(JSON.stringify(calls.pop()),
    JSON.stringify(["input_boolean", "turn_off", { entity_id: "input_boolean.vac" }]), "sets it back");
  assert(p.shadowRoot.innerHTML.includes("Check it has the value you expected"), "reminder after setting it back");
  p._hass.states = { "input_boolean.vac": { state: "off", attributes: {} } };
  assert(p._row(rv, "rv").includes("✓ as expected"), "shows when it's right again");
  STATE.open.pop();
  // An older saved filter (before reverted existed) shows the new type.
  const env2 = makeEnv(memoryStorage());
  env2.ctx.window.sessionStorage.setItem("downtime_auditor_panel_v1", JSON.stringify({ types: ["missed"] }));
  assert.strictEqual(vm.runInContext("JSON.stringify(loadState().types)", env2.ctx), '["missed","reverted"]', "new type shown by default");

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
  STATE.types = ["interrupted", "missed", "fired_at_startup", "reverted"];

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
    for (const target of [freq, ...(freq.triggers || [])]) { // the timeline draws each trigger
      target.count = 481;
      target.occurrences_iso = Array.from({ length: 200 }, (_, i) => new Date(t0 + (i + 1) * 15000).toISOString());
    }
    dense.pending_checks = [{ automation: "automation.pool", name: "Pool schedule", entity: "switch.pool", trigger_index: 0 },
                            { automation: "automation.pool", name: "Pool schedule", entity: "switch.spa", trigger_index: 0 }];
    dense.recheck_until = new Date(Date.now() + 600000).toISOString();
    dense.unchecked = [{ automation: "automation.aux", name: "Aux", entity: "switch.aux", trigger_index: 0 }];
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
    assert(page.includes("Waiting for 2 entities to report") && page.includes("switch.spa"), "pending re-checks note");
    assert(page.includes("Couldn't check 1 entity") && page.includes("switch.aux"), "unchecked note");
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

