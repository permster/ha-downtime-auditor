/* Downtime Auditor sidebar dashboard. Plain web component, no build step. */

// Terminology mirrors const.py: type (what happened), confidence (how sure; never
// colored), severity (how much it matters; the only colored attribute).
const TYPES = [
  { key: "interrupted", label: "Interrupted", icon: "mdi:motion-pause-outline" },
  { key: "missed", label: "Missed", icon: "mdi:calendar-remove-outline" },
  { key: "fired_at_startup", label: "Fired at startup", icon: "mdi:rocket-launch-outline" },
];
const TYPE = Object.fromEntries(TYPES.map((t) => [t.key, t]));
const SEVERITIES = [
  { key: "critical", label: "Critical", color: "var(--da-sev-critical)", rank: 4 },
  { key: "high", label: "High", color: "var(--da-sev-high)", rank: 3 },
  { key: "medium", label: "Medium", color: "var(--da-sev-medium)", rank: 2 },
  { key: "low", label: "Low", color: "var(--da-sev-low)", rank: 1 },
  { key: "none", label: "None", color: "var(--da-sev-none)", rank: 0 },
];
const SEV = Object.fromEntries(SEVERITIES.map((s) => [s.key, s]));
const CONFIDENCES = [
  { key: "confirmed", label: "Confirmed", level: 4 },
  { key: "probable", label: "Probable", level: 3 },
  { key: "possible", label: "Possible", level: 2 },
  { key: "unknown", label: "Unknown", level: 1 },
];
const CONF = Object.fromEntries(CONFIDENCES.map((c) => [c.key, c]));
const TABS = [
  { key: "report", label: "Last report", icon: "mdi:clipboard-text-clock-outline" },
  { key: "history", label: "History", icon: "mdi:history" },
  { key: "live", label: "Live status", icon: "mdi:heart-pulse" },
  { key: "whatif", label: "What-if", icon: "mdi:flask-outline" },
];
const SOURCE_TEXT = { label: "set by label", default: "unrated (default)", trigger: "set for this trigger" };
const DENSE_OCCURRENCES = 40; // more due times than this are drawn as a span on the timeline

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const rank = (s) => SEV[s]?.rank ?? -1;

function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60), mm = m % 60;
  if (h < 48) return `${h}h ${mm}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "medium" }) : "—");
const fmtShort = (iso) => (iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—");
function fmtAgo(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 5 ? "just now" : `${fmtDur(s)} ago`;
}
// "2026-09-29 06:30:00" (HA local strings in findings) -> Date
const parseLocal = (s) => (s ? new Date(String(s).replace(" ", "T")) : null);

// ------------------------------------------------------------------ panel state
// HA re-creates the panel element when you come back from the automation editor,
// so state lives at module level and is mirrored to sessionStorage (per browser tab).
const STORE_KEY = "downtime_auditor_panel_v1";
const DEFAULT_STATE = () => ({
  tab: "report",
  types: TYPES.map((t) => t.key),
  minSeverity: "all",
  confidences: CONFIDENCES.map((c) => c.key),
  showNone: null, // null = follow the integration option
  search: "",
  open: [],
  viewingFile: null,
  wiStart: null,
  wiEnd: null,
  whatif: null,
});
function loadState() {
  let saved = null;
  try { saved = JSON.parse(window.sessionStorage.getItem(STORE_KEY) || "null"); } catch (e) { /* private mode etc. */ }
  return { ...DEFAULT_STATE(), ...(saved && typeof saved === "object" ? saved : {}) };
}
const STATE = loadState();
let LOADED_VERSION = null; // integration version this page's panel.js came from
function saveState() {
  try { window.sessionStorage.setItem(STORE_KEY, JSON.stringify(STATE)); } catch (e) {
    // Quota (large what-if results): keep everything but the results.
    try { window.sessionStorage.setItem(STORE_KEY, JSON.stringify({ ...STATE, whatif: null })); } catch (e2) { /* ignore */ }
  }
}

class DowntimeAuditorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._report = null;
    this._history = null;
    this._status = null;
    this._error = null;
    this._notice = null;
    this._loaded = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.hass = hass;
    if (STATE.tab === "live") this._watchRuns(hass);
  }
  get hass() { return this._hass; }
  set narrow(v) {
    this._narrow = v;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.narrow = v;
  }
  set panel(p) {
    this._panel = p;
    // The version this page's code was loaded with (module-level: survives re-created elements).
    if (LOADED_VERSION === null && p?.config?.version) LOADED_VERSION = p.config.version;
  }

  // Live status: refresh the moment any automation/script starts or finishes,
  // instead of waiting for the next poll. HA hands us a new `hass` on every state change.
  _runSignature(hass) {
    let sig = "";
    for (const id in hass?.states || {}) {
      if (!id.startsWith("automation.") && !id.startsWith("script.")) continue;
      const cur = hass.states[id].attributes?.current;
      if (cur > 0) sig += `${id}:${cur};`;
    }
    return sig;
  }

  _watchRuns(hass) {
    const sig = this._runSignature(hass);
    if (sig === this._runSig) return;
    this._runSig = sig;
    clearTimeout(this._runTimer);
    this._runTimer = setTimeout(() => this._loadStatus(), 250);
  }

  async _ws(type, extra = {}) {
    return this._hass.connection.sendMessagePromise({ type: `downtime_auditor/${type}`, ...extra });
  }

  async _init() {
    this._render();
    this._loadStatus(false);
    await this._loadReport(STATE.viewingFile || undefined);
    if (STATE.tab === "history") this._loadHistory();
    const onReport = () => {
      if (!STATE.viewingFile) this._loadReport();
      this._history = null;
      if (STATE.tab === "history") this._loadHistory();
      this._loadStatus(false);
    };
    try {
      // A new report, and later changes to it (entities that reported late, re-rating).
      const unsubs = await Promise.all([
        this._hass.connection.subscribeEvents(onReport, "downtime_auditor_report"),
        this._hass.connection.subscribeEvents(onReport, "downtime_auditor_report_updated"),
      ]);
      this._unsub = () => unsubs.forEach((u) => u());
    } catch (e) { /* non-admin or older HA */ }
    // Every 2 s: poll while a restart is being analyzed (countdown), every 10 s on Live status,
    // and redraw Live status so "running for …" keeps counting.
    let ticks = 0;
    this._tick = setInterval(() => {
      ticks += 1;
      if (this._status?.pending) this._loadStatus();
      else if (STATE.tab === "live" && ticks % 5 === 0) this._loadStatus();
      else if (STATE.tab === "live" && this._status?.running_now?.length) this._render();
    }, 2000);
  }

  disconnectedCallback() {
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    if (this._unsub) this._unsub();
    clearInterval(this._tick);
    this._unsub = null;
    saveState();
  }
  connectedCallback() {
    if (this._hass && !this._unsub && this._loaded) this._init();
    if (!this._ro && window.ResizeObserver) {
      let last = 0;
      this._ro = new ResizeObserver(() => {
        const w = this.clientWidth;
        if (Math.abs(w - last) > 40) { last = w; if (this._loaded) this._render(); }
      });
      this._ro.observe(this);
    }
  }

  async _loadReport(file) {
    try {
      this._report = await this._ws("report", file ? { file } : {});
      STATE.viewingFile = file || null;
      this._error = null;
    } catch (e) {
      if (file) { STATE.viewingFile = null; return this._loadReport(); } // file expired since
      this._error = e.message || String(e);
    }
    this._loaded = true;
    this._render();
  }
  async _loadHistory() {
    try { this._history = await this._ws("history", { limit: 500 }); } catch (e) { this._error = e.message; }
    this._render();
  }
  async _loadStatus(render = true) {
    try { this._status = await this._ws("status"); } catch (e) { this._error = e.message; }
    if (render || this._loaded) this._render();
  }

  _setTab(t) {
    STATE.tab = t;
    if (t === "history" && !this._history) this._loadHistory();
    if (t === "live") {
      this._runSig = this._runSignature(this._hass); // baseline, so the very next change counts
      this._loadStatus();
    }
    this._render();
  }

  _navigate(path) {
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }
  _moreInfo(entityId) {
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  _showNone() {
    return STATE.showNone ?? Boolean(this._status?.show_severity_none);
  }

  // ---------------------------------------------------------------- render

  _render() {
    const r = this.shadowRoot;
    const scrollY = r.querySelector(".content")?.scrollTop || 0;
    r.innerHTML = `<style>${STYLE}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Downtime Auditor</div>
        <button class="icon-btn" data-act="refresh" title="Refresh"><ha-icon icon="mdi:refresh"></ha-icon></button>
      </div>
      <div class="tabs" role="tablist">
        ${TABS.map((t) => `<button role="tab" class="tab ${STATE.tab === t.key ? "active" : ""}" data-tab="${t.key}">
          <ha-icon icon="${t.icon}"></ha-icon><span>${t.label}</span></button>`).join("")}
      </div>
      <div class="content">${this._staleBanner()}${this._pendingBanner()}${
        this._error ? `<div class="banner err">${esc(this._error)}</div>` : ""}${
        this._notice ? `<div class="banner">${esc(this._notice)}</div>` : ""}${this._body()}</div>`;
    const mb = r.querySelector("ha-menu-button");
    if (mb) { mb.hass = this._hass; mb.narrow = this._narrow; }
    const content = r.querySelector(".content");
    if (content) content.scrollTop = scrollY;
    this._bind();
    saveState();
  }

  // The integration was updated but this page still runs the old dashboard code.
  _staleBanner() {
    const server = this._status?.version;
    if (!server || !LOADED_VERSION || server === LOADED_VERSION) return "";
    return `<div class="banner warn-banner"><ha-icon icon="mdi:update"></ha-icon>
      <span>Downtime Auditor was updated to ${esc(server)}, but this page is still showing the ${esc(LOADED_VERSION)} dashboard.</span>
      <button class="btn sm" data-act="reload">Reload</button></div>`;
  }

  // A restart is being analyzed: the report below is the previous one.
  _pendingBanner() {
    const p = this._status?.pending;
    if (!p) return "";
    if (p.state === "starting" || !p.due_at) {
      return `<div class="banner"><ha-icon icon="mdi:timer-sand"></ha-icon>
        <span>Home Assistant is still starting. Downtime Auditor checks what the restart missed once it has
        started and had ${esc(p.startup_delay)} s to settle.</span></div>`;
    }
    const secs = Math.max(0, Math.round((new Date(p.due_at).getTime() - Date.now()) / 1000));
    return `<div class="banner"><ha-icon icon="mdi:timer-sand"></ha-icon>
      <span>Home Assistant restarted. Checking what was missed ${secs ? `in <b>${secs} s</b>` : "now"}
      (after a ${esc(p.startup_delay)} s settle delay, so integrations can restore their states).
      ${this._report ? "The report below is from the previous restart." : ""}</span></div>`;
  }

  _body() {
    if (!this._loaded) return `<div class="empty">Loading…</div>`;
    switch (STATE.tab) {
      case "history": return this._historyView();
      case "live": return this._liveView();
      case "whatif": return this._whatIfView();
      default: return this._reportView(this._report, { viewingFile: STATE.viewingFile });
    }
  }

  _visible(f) {
    if (!STATE.types.includes(f.type)) return false;
    if (!STATE.confidences.includes(f.confidence)) return false;
    if (f.severity === "none") return this._showNone();
    if (STATE.minSeverity !== "all" && rank(f.severity) < rank(STATE.minSeverity)) return false;
    return this._matches(f);
  }

  _matches(f) {
    if (!STATE.search) return true;
    const q = STATE.search.toLowerCase();
    return [f.name, f.entity_id, f.summary, f.platform, f.trigger_id, f.severity, f.confidence,
      ...(f.triggers || []).map((c) => c.summary)]
      .some((v) => String(v ?? "").toLowerCase().includes(q));
  }

  _reportView(rep, { viewingFile = null, whatIf = false } = {}) {
    if (!rep) {
      return `<div class="card empty-card">
        <ha-icon icon="mdi:clipboard-clock-outline" class="big"></ha-icon>
        <h2>No report yet</h2>
        <p>Downtime Auditor is recording baselines now. The first report appears after the <b>next</b> restart
        of Home Assistant. Try the <a href="#" data-tab="whatif">What-if</a> tab in the meantime.</p></div>`;
    }
    const w = rep.window, meta = rep.meta || {}, counts = rep.counts || {};
    const findings = rep.findings || [];
    const shown = findings.filter((f) => this._visible(f));
    const noneCount = findings.filter((f) => f.severity === "none").length;
    const skipped = rep.skipped ?? meta.automations_skipped ?? 0;
    const verBump = meta.ha_version_before && meta.ha_version_before !== meta.ha_version_after;
    const top = rep.highest_severity;

    return `
      ${viewingFile ? `<div class="banner">Viewing an older report from ${esc(fmtTime(rep.generated_at))}.
        <a href="#" data-act="latest">Back to latest</a></div>` : ""}
      ${rep.legacy ? `<div class="banner">Recorded by v0.4, before severity ratings existed: severities are defaults.</div>` : ""}
      ${whatIf ? `<div class="banner">What-if simulation — only time, time-pattern, sun and calendar triggers are evaluated.
        ${meta.conditions_basis === "none" ? " Conditions weren't checked: the recorder isn't available." : " Conditions are checked against recorder history."}</div>` : ""}
      <div class="card hero">
        <div class="hero-main">
          <div class="label">${whatIf ? "Simulated window" : "Last downtime"}</div>
          <div class="dur">${esc(w.duration)}</div>
          <div class="range">${esc(fmtTime(w.start))} <span class="arrow">→</span> ${esc(fmtTime(w.end))}</div>
        </div>
        <div class="hero-side">
          ${top ? `<span class="sev-badge lg" style="--s:${SEV[top]?.color}">Highest severity: ${esc(SEV[top]?.label || top)}</span>` : ""}
          ${whatIf ? "" : `<span class="chip ${w.clean_shutdown ? "ok" : "bad"}">
            <ha-icon icon="${w.clean_shutdown ? "mdi:check-circle-outline" : "mdi:flash-alert-outline"}"></ha-icon>
            ${w.clean_shutdown ? "Clean shutdown" : "Unclean stop"}</span>`}
          ${verBump ? `<span class="chip info"><ha-icon icon="mdi:update"></ha-icon>${esc(meta.ha_version_before)} → ${esc(meta.ha_version_after)}</span>` : ""}
          <div class="meta">
            <div><b>${esc(meta.automations_checked ?? "?")}</b> automations · <b>${esc(meta.triggers_checked ?? "?")}</b> triggers checked</div>
            ${whatIf ? "" : `<div>Window start: ${esc(w.start_basis)}</div>`}
            ${meta.baseline_captured_at ? `<div>Baseline from ${esc(fmtTime(meta.baseline_captured_at))}</div>` : ""}
            ${meta.baseline_available === false ? `<div class="warn">No baseline was available — state checks limited</div>` : ""}
            ${meta.window_truncated_to_days ? `<div class="warn">Window capped to ${esc(meta.window_truncated_to_days)} days</div>` : ""}
          </div>
        </div>
      </div>

      <div class="tiles">
        ${TYPES.map((t) => `<button class="tile ${STATE.types.includes(t.key) ? "on" : ""}" data-type="${t.key}" aria-pressed="${STATE.types.includes(t.key)}">
          <ha-icon icon="${t.icon}"></ha-icon>
          <div class="n">${counts[t.key] || 0}</div><div class="l">${t.label}</div></button>`).join("")}
      </div>

      ${this._timeline(rep, shown)}

      <div class="card">
        <div class="list-head">
          <h3>Findings <span class="muted">(${shown.length} of ${findings.length})</span></h3>
          <input class="search" type="search" placeholder="Search name, entity, summary…" value="${esc(STATE.search)}">
        </div>
        ${this._filterBar(noneCount)}
        ${!findings.length ? `<div class="all-good"><ha-icon icon="mdi:shield-check-outline"></ha-icon>
            Nothing was missed or interrupted${whatIf ? " in this window" : ""}.</div>` : ""}
        ${shown.map((f) => this._row(f, this._key(rep, f), { canRate: !rep.legacy })).join("")}
        ${findings.length && !shown.length ? `<div class="empty">No findings match the current filter.</div>` : ""}
        ${skipped ? `<div class="foot muted">${esc(skipped)} automation(s) were turned off and were skipped.</div>` : ""}
        ${this._lateChecks(rep)}
        ${whatIf ? `<div class="foot muted">What-if results are kept while this browser tab is open.</div>` : ""}
      </div>`;
  }

  // Entities that hadn't reported when the report was built: re-checked when they do.
  _lateChecks(rep) {
    const byEntity = (items) => {
      const m = new Map();
      for (const p of items || []) m.set(p.entity, [...(m.get(p.entity) || []), p.name || p.automation]);
      return [...m.entries()];
    };
    const pending = byEntity(rep.pending_checks), unchecked = byEntity(rep.unchecked);
    const list = (entries) => entries.map(([e, autos]) =>
      `<li><span class="mono">${esc(e)}</span> <span class="muted">— ${esc([...new Set(autos)].join(", "))}</span></li>`).join("");
    let out = "";
    if (pending.length) {
      const until = rep.recheck_until ? new Date(rep.recheck_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
      out += `<div class="late"><div><ha-icon icon="mdi:timer-sand"></ha-icon>
        <b>Waiting for ${pending.length} entit${pending.length === 1 ? "y" : "ies"} to report</b>
        <span class="muted">— they hadn't come back when this report was made. Their triggers are checked when they do${until ? ` (until ${esc(until)})` : ""};
        anything Home Assistant didn't act on is added here.</span></div><ul>${list(pending)}</ul></div>`;
    }
    const errors = rep.meta?.analysis_errors || [];
    if (errors.length) {
      out += `<div class="late"><div><ha-icon icon="mdi:bug-outline"></ha-icon>
        <b>Couldn't analyze ${errors.length} trigger${errors.length === 1 ? "" : "s"}</b>
        <span class="muted">— a Downtime Auditor problem, not a missed trigger.
        <a href="https://github.com/permster/ha-downtime-auditor/issues" target="_blank" rel="noopener">Please report it</a>
        with the warning from the Home Assistant log.</span></div>
        <ul>${errors.map((e) => `<li>${esc(e.name || e.automation)} <span class="muted">— ${
          e.trigger_index != null ? `trigger #${esc(e.trigger_index)}${e.platform ? ` (${esc(e.platform)})` : ""}: ` : ""}${esc(e.error)}</span></li>`).join("")}</ul></div>`;
    }
    if (unchecked.length) {
      out += `<div class="late"><div><ha-icon icon="mdi:help-circle-outline"></ha-icon>
        <b>Couldn't check ${unchecked.length} entit${unchecked.length === 1 ? "y" : "ies"}</b>
        <span class="muted">— no value after the restart, so their triggers couldn't be checked.</span></div><ul>${list(unchecked)}</ul></div>`;
    }
    return out;
  }

  _key(rep, f) {
    return [rep.generated_at, f.type, f.entity_id].join("|"); // one finding per automation and type
  }

  _filterBar(noneCount) {
    const showNone = this._showNone();
    return `<div class="filters">
      <label class="fl">Minimum severity
        <select data-act="min-severity">
          <option value="all" ${STATE.minSeverity === "all" ? "selected" : ""}>All</option>
          ${SEVERITIES.filter((s) => s.key !== "none").map((s) => `<option value="${s.key}" ${STATE.minSeverity === s.key ? "selected" : ""}>${s.label}${s.key === "critical" ? "" : " and up"}</option>`).join("")}
        </select>
      </label>
      <div class="fl">Confidence
        <div class="chips">${CONFIDENCES.map((c) => `<button class="chip-btn sm ${STATE.confidences.includes(c.key) ? "on" : ""}" data-conf="${c.key}" aria-pressed="${STATE.confidences.includes(c.key)}">${this._meter(c.key)} ${c.label}</button>`).join("")}</div>
      </div>
      <label class="fl toggle"><input type="checkbox" data-act="show-none" ${showNone ? "checked" : ""}>
        Show severity None <span class="muted">(${noneCount})</span></label>
    </div>`;
  }

  _meter(conf) {
    const lvl = CONF[conf]?.level ?? 0;
    return `<span class="meter" aria-hidden="true">${[1, 2, 3, 4].map((i) => `<i class="${i <= lvl ? "f" : ""}"></i>`).join("")}</span>`;
  }

  _chartWidth() {
    // Draw charts at their real pixel width so text and dots don't get stretched.
    const w = (this.clientWidth || 1000) - (this.clientWidth < 600 ? 16 : 32) - 34;
    return Math.max(280, Math.min(1134, w));
  }

  _timeline(rep, findings) {
    const w = rep.window;
    const t0 = new Date(w.start).getTime(), t1 = new Date(w.end).getTime();
    if (!(t1 > t0)) return "";
    const W = this._chartWidth(), H = 96, pad = 24, x = (t) => pad + ((t - t0) / (t1 - t0)) * (W - 2 * pad);
    const lanes = { interrupted: 24, missed: 46, fired_at_startup: 66 };
    const marks = [];
    // Each trigger of a finding gets its own marks (and its own severity color).
    const marked = findings.flatMap((g) => (g.triggers?.length ? g.triggers.map((c) => ({ ...c, name: g.name })) : [g]));
    for (const f of marked) {
      const color = SEV[f.severity]?.color || "var(--da-sev-none)";
      const y = lanes[f.type] ?? 46;
      if (f.type === "interrupted") {
        marks.push(`<g><title>${esc(f.name)} — interrupted · ${esc(SEV[f.severity]?.label)}</title><rect x="${x(t0) - 5}" y="${y - 5}" width="10" height="10" transform="rotate(45 ${x(t0)} ${y})" fill="${color}"/></g>`);
      } else if (f.type === "fired_at_startup") {
        marks.push(`<g><title>${esc(f.name)} — fired at startup</title><circle cx="${x(t1)}" cy="${y}" r="4" fill="none" stroke="${color}" stroke-width="2"/></g>`);
      } else if (f.occurrences?.length && (f.count || 0) > DENSE_OCCURRENCES) {
        // Frequent triggers (e.g. every 15 s): one line over the whole span, not a wall of dots.
        // Only the first 200 due times are stored, so a capped list runs to the window end.
        const iso = f.occurrences_iso || [];
        const first = iso.length ? new Date(iso[0]).getTime() : t0;
        const last = f.count > iso.length || !iso.length ? t1 : new Date(iso[iso.length - 1]).getTime();
        const xa = x(Math.max(t0, first)), xb = x(Math.min(t1, last));
        marks.push(`<g><title>${esc(f.name)} — due ${esc(f.count)} times · ${esc(SEV[f.severity]?.label)}</title>
          <line x1="${xa}" x2="${xb}" y1="${y}" y2="${y}" stroke="${color}" stroke-width="3" stroke-dasharray="2 3" stroke-linecap="round"/>
          <text x="${xa}" y="${y - 6}" class="count">×${esc(f.count)}</text></g>`);
      } else if (f.occurrences?.length) {
        const occ = f.occurrences_iso?.length ? f.occurrences_iso : f.occurrences;
        for (const o of occ.slice(0, 200)) {
          const d = f.occurrences_iso?.length ? new Date(o) : parseLocal(o);
          if (!d) continue;
          const t = d.getTime();
          if (t < t0 - 1000 || t > t1 + 1000) continue;
          marks.push(`<circle cx="${x(t)}" cy="${y}" r="4.5" fill="${color}"><title>${esc(f.name)} — due ${esc(fmtTime(d.toISOString()))} · ${esc(SEV[f.severity]?.label)}</title></circle>`);
        }
      } else {
        marks.push(`<g><title>${esc(f.name)} — changed during downtime · ${esc(SEV[f.severity]?.label)}</title><line x1="${x(t1) - 3}" x2="${x(t1) + 3}" y1="${y - 5}" y2="${y + 5}" stroke="${color}" stroke-width="3"/></g>`);
      }
    }
    const ticks = [];
    const n = W < 560 ? 2 : W < 900 ? 4 : 6;
    for (let i = 0; i <= n; i++) {
      const t = t0 + ((t1 - t0) * i) / n;
      ticks.push(`<text x="${x(t)}" y="${H - 2}" text-anchor="${i === 0 ? "start" : i === n ? "end" : "middle"}">${esc(fmtShort(new Date(t).toISOString()))}</text>`);
    }
    return `<div class="card">
      <h3>Timeline <span class="muted">(shown findings)</span></h3>
      <svg class="timeline" viewBox="0 0 ${W} ${H}" role="img" aria-label="Downtime timeline">
        <rect x="${pad}" y="10" width="${W - 2 * pad}" height="${H - 34}" rx="6" class="band"/>
        <line x1="${pad}" x2="${W - pad}" y1="${H - 24}" y2="${H - 24}" class="axis"/>
        ${ticks.join("")}
        ${marks.join("")}
      </svg>
      <div class="legend">
        <span>◆ Interrupted (at shutdown)</span><span>● Missed — scheduled time</span>
        <span>| State change (seen at startup)</span><span>○ Fired at startup</span>
        <span class="sep"></span>
        ${SEVERITIES.map((s) => `<span><i style="background:${s.color}"></i>${s.label}</span>`).join("")}
      </div>
    </div>`;
  }

  _editorLinks(f) {
    const links = [];
    const isScript = String(f.entity_id || "").startsWith("script.");
    if (f.item_id) {
      const kind = isScript ? "script" : "automation";
      // Real links: a plain click navigates inside HA; Ctrl/middle-click opens a new tab.
      links.push(`<a href="/config/${kind}/edit/${encodeURIComponent(f.item_id)}" data-nav><ha-icon icon="mdi:pencil"></ha-icon>Edit</a>`);
      links.push(`<a href="/config/${kind}/trace/${encodeURIComponent(f.item_id)}" data-nav><ha-icon icon="mdi:timeline-text-outline"></ha-icon>Traces</a>`);
    }
    links.push(`<a href="#" data-more="${esc(f.entity_id)}"><ha-icon icon="mdi:information-outline"></ha-icon>Details</a>`);
    const d = f.details || {};
    if (d.entity) links.push(`<a href="#" data-more="${esc(d.entity)}"><ha-icon icon="mdi:radar"></ha-icon>${esc(d.entity)}</a>`);
    return links;
  }

  _row(f, key, { canRate = true, live = false } = {}) {
    const t = TYPE[f.type] || { label: f.type, icon: "mdi:alert" };
    const sev = SEV[f.severity];
    const open = STATE.open.includes(key);
    const cond = (f.details || {}).conditions || {};
    const trig = f.trigger_id ? `trigger “${esc(f.trigger_id)}”` : f.trigger_index != null ? `trigger #${f.trigger_index}` : "";
    return `<div class="row ${open ? "open" : ""} ${f.severity === "none" ? "dim" : ""}" style="--s:${sev ? sev.color : "var(--da-line)"}">
      <button class="row-head" data-toggle="${esc(key)}" aria-expanded="${open}">
        <ha-icon icon="${t.icon}" class="type-ico" title="${esc(t.label)}"></ha-icon>
        <div class="row-main">
          <div class="row-title">${esc(f.name)} <span class="muted mono">${esc(f.entity_id)}</span></div>
          <div class="row-sum">${esc(f.summary)}</div>
        </div>
        <div class="row-tags">
          ${sev ? `<span class="sev-badge" style="--s:${sev.color}">${sev.label}</span>` : live ? `<span class="tag">running</span>` : ""}
          ${f.confidence && CONF[f.confidence] ? `<span class="tag conf" title="Confidence: ${esc(CONF[f.confidence].label)}">${this._meter(f.confidence)} ${esc(CONF[f.confidence].label)}</span>` : ""}
          ${cond.likely === "fail" ? `<span class="tag" title="${esc(cond.why)}">conditions?</span>` : ""}
          ${f.platform ? `<span class="tag">${esc(f.platform)}</span>` : ""}
          ${(f.triggers?.length || 0) > 1 ? `<span class="tag">${f.triggers.length} ${f.type === "missed" ? "triggers" : "items"}</span>` : trig ? `<span class="tag">${trig}</span>` : ""}
          <ha-icon icon="${open ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
        </div>
      </button>
      ${open ? `<div class="row-body">${this._details(f, { canRate: canRate && !live })}<div class="links">${this._editorLinks(f).join("")}</div></div>` : ""}
    </div>`;
  }

  _severityPicker(f) {
    const rateable = /^(automation|script)\./.test(String(f.entity_id || ""));
    if (!rateable) return "";
    const current = f.severity_source === "label" ? (f.severity_base || f.severity) : "";
    return `<select class="sev-select" data-rate="${esc(f.entity_id)}" aria-label="Severity rating for ${esc(f.name)}">
      <option value="" ${current ? "" : "selected"}>Unrated (Medium)</option>
      ${SEVERITIES.map((s) => `<option value="${s.key}" ${current === s.key ? "selected" : ""}>${s.label}</option>`).join("")}
    </select>`;
  }

  // One trigger of a multi-trigger finding: its own severity, confidence and evidence.
  _triggerItem(c, canRate = false) {
    const sev = SEV[c.severity];
    const trig = c.trigger_id ? `trigger “${esc(c.trigger_id)}”` : c.trigger_index != null ? `trigger #${c.trigger_index}` : "";
    return `<div class="trig">
      <div class="trig-head">
        ${sev ? `<span class="sev-badge" style="--s:${sev.color}">${sev.label}</span>` : ""}
        ${c.confidence && CONF[c.confidence] ? `<span class="conf-inline">${this._meter(c.confidence)} ${esc(CONF[c.confidence].label)}</span>` : ""}
        ${c.platform ? `<span class="tag">${esc(c.platform)}</span>` : ""}${trig ? `<span class="tag">${trig}</span>` : ""}
      </div>
      <div class="trig-sum">${esc(c.summary)}</div>
      ${canRate ? this._triggerPicker(c) : ""}
      ${this._details(c, { nested: true })}
    </div>`;
  }

  _details(f, { canRate = true, nested = false } = {}) {
    const d = f.details || {};
    const parts = [];
    const children = f.triggers || [];
    if (nested) {
      // severity/confidence are in the trigger's header
    } else if (f.severity) {
      parts.push(`<div class="kv"><div class="k">Severity</div><div class="v sev-line">
        <span class="sev-badge" style="--s:${SEV[f.severity]?.color}">${esc(SEV[f.severity]?.label || f.severity)}</span>
        <span class="muted">${esc(f.severity_reason || SOURCE_TEXT[f.severity_source] || "")}</span></div></div>`);
      if (canRate && this._severityPicker(f)) {
        parts.push(`<div class="kv"><div class="k">Rate this ${String(f.entity_id).startsWith("script.") ? "script" : "automation"}</div>
          <div class="v">${this._severityPicker(f)} <span class="muted small-inline">Saved as a <code>downtime_auditor_sev</code> label.</span></div></div>`);
      }
    }
    if (f.confidence && !nested) {
      parts.push(`<div class="kv"><div class="k">Confidence</div><div class="v">${this._meter(f.confidence)} ${esc(CONF[f.confidence]?.label || f.confidence)}${
        children.length > 1 ? ` <span class="muted">(the most certain of its triggers)</span>` : ""}</div></div>`);
    }
    if (!nested && children.length > 1) {
      parts.push(`<div class="kv"><div class="k">Triggers (${children.length})</div>
        <div class="v trig-list">${children.map((c) => this._triggerItem(c, canRate)).join("")}</div></div>`);
      parts.push(`<details class="raw"><summary>Raw finding</summary><pre>${esc(JSON.stringify(f, null, 2))}</pre></details>`);
      return parts.join("");
    }
    if (d.conditions) parts.push(this._conditions(d.conditions));
    if (f.occurrences?.length) {
      parts.push(`<div class="kv"><div class="k">Due at${f.count > f.occurrences.length ? ` (showing ${f.occurrences.length} of ${f.count})` : ""}</div>
        <div class="v chips">${f.occurrences.map((o) => `<span class="mini">${esc(o)}</span>`).join("")}</div></div>`);
    }
    if ("before" in d || "after" in d) {
      parts.push(`<div class="kv"><div class="k">Value</div><div class="v ba">
        <span class="mini">${esc(JSON.stringify(d.before))}</span><span class="arrow">→</span><span class="mini">${esc(JSON.stringify(d.after))}</span>
        ${d.attribute ? `<span class="muted">attribute ${esc(d.attribute)}</span>` : ""}</div></div>`);
    }
    if (d.for != null) parts.push(`<div class="kv"><div class="k">for:</div><div class="v">${esc(fmtDur(d.for))}</div></div>`);
    if (d.above != null || d.below != null) parts.push(`<div class="kv"><div class="k">Range</div><div class="v">${d.above != null ? `&gt; ${esc(d.above)}` : ""} ${d.below != null ? `&lt; ${esc(d.below)}` : ""}</div></div>`);
    if (d.template) parts.push(`<div class="kv"><div class="k">Template</div><div class="v"><code>${esc(d.template)}</code></div></div>`);
    if (d.reason) parts.push(`<div class="kv"><div class="k">Why</div><div class="v">${esc(d.reason)}</div></div>`);
    if (d.events?.length) parts.push(`<div class="kv"><div class="k">Events</div><div class="v">${d.events.map(esc).join(", ")}</div></div>`);
    if (d.note) parts.push(`<div class="kv"><div class="k">Note</div><div class="v">${esc(d.note)}</div></div>`);
    if (d.mode) parts.push(`<div class="kv"><div class="k">Mode</div><div class="v">${esc(d.mode)}</div></div>`);
    for (const run of d.runs || []) {
      if (run.last_step || run.timestamp) {
        const res = run.last_step_result || {};
        parts.push(`<div class="run">
          <div><b>Run</b> <span class="mono">${esc(String(run.run_id || "").slice(0, 8))}</span> started ${esc(fmtTime(run.timestamp?.start))}</div>
          ${run.trigger ? `<div>Triggered by ${esc(run.trigger)}</div>` : ""}
          <div>Stopped at <span class="mono">${esc(run.last_step)}</span>
            ${run.last_step_config ? `<code>${esc(JSON.stringify(run.last_step_config))}</code>` : ""}</div>
          ${res.delay != null ? `<div>Delay ${esc(fmtDur(res.delay))}, done: ${esc(res.done)}</div>` : ""}
          ${res.wait ? `<div>Waiting: ${esc(JSON.stringify(res.wait))}</div>` : ""}
        </div>`);
      } else if (run.time) {
        parts.push(`<div class="run">Fired ${esc(fmtTime(run.time))}${run.source ? ` — ${esc(run.source)}` : ""}</div>`);
      }
    }
    if (!nested) parts.push(`<details class="raw"><summary>Raw finding</summary><pre>${esc(JSON.stringify(f, null, 2))}</pre></details>`);
    return parts.join("");
  }

  _conditions(c) {
    const ICON = { pass: "mdi:check-circle-outline", fail: "mdi:close-circle-outline", unknown: "mdi:help-circle-outline" };
    const basis = { baseline: "pre-downtime values", history: "recorder history", none: "no recorded states" }[c.basis] || c.basis;
    const step = (s) => `<li class="cs ${esc(s.result)}"><ha-icon icon="${ICON[s.result] || ICON.unknown}"></ha-icon>
      <b>${esc(s.condition)}</b>${s.why ? ` — ${esc(s.why)}` : ""}${s.basis ? ` <span class="muted">(${esc({ baseline: "pre-downtime value", history: "history" }[s.basis] || s.basis)})</span>` : ""}
      ${s.conditions?.length ? `<ul>${s.conditions.map(step).join("")}</ul>` : ""}</li>`;
    const headline = c.likely === "fail"
      ? "Probably failed — based on pre-downtime values, which may have changed while HA was down"
      : { pass: "Would have passed", fail: "Would have failed", unknown: "Couldn't be checked" }[c.result] || c.result;
    return `<div class="kv"><div class="k">Conditions</div><div class="v">
      <div class="cond-head ${esc(c.likely === "fail" ? "unknown" : c.result)}">${esc(headline)}</div>
      <div class="muted">${c.checked > 1 ? `Checked at ${esc(c.checked)} due times; breakdown for ${esc(fmtTime(c.shown_for))}. ` : ""}From ${esc(basis)}.</div>
      <ul class="cond-tree">${(c.steps || []).map(step).join("")}</ul></div></div>`;
  }

  _historyView() {
    const h = this._history;
    if (!h) return `<div class="empty">Loading history…</div>`;
    if (!h.length) return `<div class="card empty-card"><h2>No history yet</h2><p>Each restart adds an entry here (requires “Write JSON reports”).</p></div>`;
    const withReport = h.filter((i) => i.available).length;
    const items = h.slice(0, 40).slice().reverse();
    const max = Math.max(...items.map((i) => i.window?.duration_seconds || 0), 1);
    const W = this._chartWidth(), H = 160, bw = Math.min(56, (W - 40) / items.length);
    const bars = items.map((it, i) => {
      const v = it.window?.duration_seconds || 0;
      const bh = Math.max(2, (Math.sqrt(v) / Math.sqrt(max)) * (H - 40));
      const act = (it.counts?.interrupted || 0) + (it.counts?.missed || 0);
      const fill = it.window?.clean_shutdown ? "var(--da-bar-clean)" : "var(--da-bar-unclean)";
      const top = it.highest_severity && it.highest_severity !== "none" ? SEV[it.highest_severity] : null;
      return `<g class="bar-g" data-file="${esc(it.available ? it.file : "")}">
        <title>${esc(fmtTime(it.window?.start))}: ${esc(it.window?.duration)} · ${act} missed/interrupted${top ? ` · highest ${top.label}` : ""}</title>
        <rect x="${20 + i * bw + bw * 0.15}" y="${H - 20 - bh}" width="${bw * 0.7}" height="${bh}" rx="3" fill="${fill}"/>
        ${top ? `<circle cx="${20 + i * bw + bw / 2}" cy="${H - 26 - bh}" r="4" fill="${top.color}"/>` : ""}
      </g>`;
    }).join("");
    return `<div class="card">
        <h3>Downtime durations <span class="muted">(last ${items.length}, √ scale)</span></h3>
        <svg class="hist" viewBox="0 0 ${W} ${H}">${bars}
          <line x1="20" x2="${W - 20}" y1="${H - 20}" y2="${H - 20}" class="axis"/></svg>
        <div class="legend"><span><i style="background:var(--da-bar-clean)"></i>Clean</span><span><i style="background:var(--da-bar-unclean)"></i>Unclean</span>
          <span>Dot = highest severity</span></div>
      </div>
      <div class="card">
        <p class="muted small">${h.length} downtime${h.length === 1 ? "" : "s"} recorded · ${withReport} with a saved report.
          Full reports are kept only when something was found, for the retention period set in the integration's options.</p>
        <table class="tbl">
        <thead><tr><th>Went down</th><th>Duration</th><th>Shutdown</th><th>Highest</th>${TYPES.map((t) => `<th title="${t.label}"><ha-icon icon="${t.icon}"></ha-icon></th>`).join("")}<th></th></tr></thead>
        <tbody>${h.map((it) => `<tr>
          <td>${esc(fmtTime(it.window?.start))}${it.legacy ? ` <span class="tag" title="Recorded by v0.4, before severity ratings existed">v0.4</span>` : ""}</td><td>${esc(it.window?.duration)}</td>
          <td><span class="chip sm ${it.window?.clean_shutdown ? "ok" : "bad"}">${it.window?.clean_shutdown ? "clean" : "unclean"}</span></td>
          <td>${it.highest_severity ? `<span class="sev-badge" style="--s:${SEV[it.highest_severity]?.color}">${esc(SEV[it.highest_severity]?.label)}</span>` : `<span class="muted">—</span>`}</td>
          ${TYPES.map((t) => `<td class="${it.counts?.[t.key] ? "hot" : "muted"}">${it.counts?.[t.key] || 0}</td>`).join("")}
          <td>${it.available ? `<a href="#" data-file="${esc(it.file)}">Open</a>`
            : it.file ? `<span class="muted" title="Older than the report retention period">expired</span>`
            : `<span class="muted" title="Nothing worth keeping was found, so only this summary was kept">nothing found</span>`}</td></tr>`).join("")}
        </tbody></table></div>`;
  }

  _liveView() {
    const s = this._status;
    if (!s) return `<div class="empty">Loading…</div>`;
    const hbAge = s.last_heartbeat ? (Date.now() - new Date(s.last_heartbeat).getTime()) / 1000 : Infinity;
    const healthy = s.tracking && hbAge < (s.heartbeat_interval || 60) * 2.5;
    const sevLabel = (k) => SEV[k]?.label || k;
    return `<div class="card hero">
        <div class="hero-main"><div class="label">Tracking</div>
          <div class="dur ${healthy ? "" : "warn"}">${s.tracking ? (healthy ? "Healthy" : "Stale") : "Waiting for startup"}</div>
          <div class="range">Last heartbeat ${esc(fmtAgo(s.last_heartbeat))} · every ${esc(s.heartbeat_interval)}s</div></div>
        <div class="hero-side meta">
          <div>Session started ${esc(fmtTime(s.session_started))}</div>
          <div><b>${s.baseline_automations}</b> automations · <b>${s.baseline_entities}</b> trigger/condition entities · <b>${s.baseline_templates}</b> templates in baseline</div>
          <div>Startup settle delay ${esc(s.startup_delay)}s · JSON reports ${s.json_enabled ? "on" : "off"}</div>
        </div></div>
      <div class="card"><h3>Severity ratings</h3>
        <p class="muted">Rate automations and scripts with the <code>downtime_auditor_sev: …</code> labels (Critical, High, Medium, Low, None), or from a finding's details.
          Unrated means Medium. Repairs: <b>${esc(sevLabel(s.repairs_min_severity))}</b> and up · push: <b>${esc(sevLabel(s.push_min_severity))}</b> and up (change these in the integration's options).</p>
        <button class="btn secondary" data-act="create-labels">Create severity labels</button>
        <span class="muted small-inline">Only labels that are missing are created.</span>
      </div>
      <div class="card"><h3>Running right now <span class="muted">(${s.running_now.length})</span>
        <span class="live-dot" title="Updates as automations and scripts start and finish"></span></h3>
        <p class="muted">These would be reported as interrupted if Home Assistant stopped this instant.
          Most automations finish in milliseconds, so only ones in a <code>delay</code> or <code>wait</code> stay here.</p>
        ${s.running_now.length ? s.running_now.map((r, i) => this._row({
          type: "interrupted", name: r.name, entity_id: r.entity_id, item_id: r.item_id, platform: r.domain,
          summary: (r.runs || []).map((x) => {
            const started = x.timestamp?.start ? new Date(x.timestamp.start).getTime() : null;
            const step = x.last_step_config && typeof x.last_step_config === "object"
              ? (x.last_step_config.delay != null ? ` (delay ${JSON.stringify(x.last_step_config.delay)})`
                : x.last_step_config.wait_template || x.last_step_config.wait_for_trigger ? " (waiting)" : "")
              : "";
            return `${started ? `running ${fmtDur((Date.now() - started) / 1000)} · ` : ""}at ${x.last_step || "?"}${step}${x.trigger ? ` · ${x.trigger}` : ""}`;
          }).join(" | ") || `${r.current_runs} run(s)`,
          details: { mode: r.mode, runs: r.runs },
        }, `live|${i}|${r.entity_id}`, { live: true })).join("") : `<div class="all-good"><ha-icon icon="mdi:sleep"></ha-icon>Nothing is running.</div>`}
      </div>`;
  }

  _whatIfView() {
    const pad = (n) => String(n).padStart(2, "0");
    const toInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const now = new Date();
    const lastNight = new Date(now); lastNight.setHours(1, 0, 0, 0); if (lastNight > now) lastNight.setDate(lastNight.getDate() - 1);
    const s = STATE.wiStart || toInput(new Date(now.getTime() - 4 * 3600e3));
    const e = STATE.wiEnd || toInput(now);
    return `<div class="card">
        <h3>What would a downtime miss?</h3>
        <p class="muted">Pick a window to see which time, time-pattern, sun and calendar triggers would be skipped if Home Assistant were down.</p>
        <div class="wi">
          <label>From <input type="datetime-local" id="wi-start" value="${esc(s)}"></label>
          <label>To <input type="datetime-local" id="wi-end" value="${esc(e)}"></label>
          <button class="btn" data-act="whatif">Run</button>
        </div>
        <div class="presets">
          <button class="chip-btn" data-preset="${toInput(lastNight)}|${toInput(new Date(lastNight.getTime() + 6 * 3600e3))}">Last night 1–7am</button>
          <button class="chip-btn" data-preset="${toInput(new Date(now.getTime() - 3600e3))}|${toInput(now)}">Last hour</button>
          <button class="chip-btn" data-preset="${toInput(new Date(now.getTime() - 24 * 3600e3))}|${toInput(now)}">Last 24h</button>
          <button class="chip-btn" data-preset="${toInput(now)}|${toInput(new Date(now.getTime() + 2 * 3600e3))}">Next 2 hours</button>
        </div>
      </div>
      ${this._wiBusy ? `<div class="empty">Analyzing…</div>` : STATE.whatif ? this._reportView(STATE.whatif, { whatIf: true }) : ""}`;
  }

  // ---------------------------------------------------------------- events

  _bind() {
    const r = this.shadowRoot;
    const on = (sel, evt, fn) => r.querySelectorAll(sel).forEach((el) => el.addEventListener(evt, (ev) => fn(el, ev)));
    on("[data-tab]", "click", (el, ev) => { ev.preventDefault(); this._setTab(el.dataset.tab); });
    on("[data-type]", "click", (el) => { this._toggle(STATE.types, el.dataset.type); this._render(); });
    on("[data-conf]", "click", (el) => { this._toggle(STATE.confidences, el.dataset.conf); this._render(); });
    on("[data-toggle]", "click", (el) => { this._toggle(STATE.open, el.dataset.toggle); this._render(); });
    on("a[data-nav]", "click", (el, ev) => {
      // Let the browser handle new-tab gestures; navigate in-app on a plain click.
      if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      saveState();
      this._navigate(el.getAttribute("href"));
    });
    on("[data-more]", "click", (el, ev) => { ev.preventDefault(); this._moreInfo(el.dataset.more); });
    on("[data-file]", "click", (el, ev) => {
      ev.preventDefault();
      if (!el.dataset.file) return;
      STATE.tab = "report";
      this._loadReport(el.dataset.file);
    });
    on("[data-preset]", "click", (el) => {
      [STATE.wiStart, STATE.wiEnd] = el.dataset.preset.split("|");
      this._runWhatIf();
    });
    on("select[data-act=min-severity]", "change", (el) => { STATE.minSeverity = el.value; this._render(); });
    on("input[data-act=show-none]", "change", (el) => { STATE.showNone = el.checked; this._render(); });
    on("select[data-rate]", "change", (el) => this._rate(el.dataset.rate, el.value || null));
    on("select[data-rate-trigger]", "change", (el) => this._rateTrigger(JSON.parse(el.dataset.rateTrigger), el.value || null));
    const search = r.querySelector(".search");
    if (search) search.addEventListener("input", (ev) => {
      STATE.search = ev.target.value;
      clearTimeout(this._st);
      this._st = setTimeout(() => { this._render(); const s = this.shadowRoot.querySelector(".search"); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }, 200);
    });
    on("button[data-act], a[data-act]", "click", (el, ev) => {
      ev.preventDefault();
      const a = el.dataset.act;
      if (a === "refresh") {
        this._history = null;
        this._notice = null;
        if (STATE.tab === "history") this._loadHistory();
        else if (STATE.tab === "live") this._loadStatus();
        else this._loadReport(STATE.viewingFile || undefined);
      } else if (a === "latest") this._loadReport();
      else if (a === "whatif") {
        STATE.wiStart = r.querySelector("#wi-start").value;
        STATE.wiEnd = r.querySelector("#wi-end").value;
        this._runWhatIf();
      } else if (a === "create-labels") this._createLabels();
      else if (a === "reload") window.location.reload();
    });
  }

  _toggle(list, key) {
    const i = list.indexOf(key);
    if (i >= 0) list.splice(i, 1); else list.push(key);
  }

  // Rate one trigger of an automation: overrides the automation's rating for that trigger only.
  _triggerPicker(c) {
    if (c.type !== "missed" || !String(c.entity_id || "").startsWith("automation.")) return "";
    if (c.trigger_id == null && c.trigger_index == null) return "";
    const which = { entity_id: c.entity_id, item_id: c.item_id ?? null, trigger_id: c.trigger_id ?? null,
                    trigger_index: c.trigger_index ?? null, platform: c.platform ?? null };
    const current = c.severity_source === "trigger" ? c.severity_base : "";
    return `<label class="trig-rate">Rate this trigger
      <select class="sev-select" data-rate-trigger="${esc(JSON.stringify(which))}" aria-label="Severity rating for this trigger">
        <option value="" ${current ? "" : "selected"}>Automation's rating</option>
        ${SEVERITIES.map((s) => `<option value="${s.key}" ${current === s.key ? "selected" : ""}>${s.label}</option>`).join("")}
      </select></label>`;
  }

  async _rateTrigger(which, severity) {
    try {
      await this._ws("set_trigger_severity", { ...which, severity });
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    if (STATE.tab === "whatif" && STATE.whatif && STATE.wiStart && STATE.wiEnd) await this._runWhatIf();
    else if (!STATE.viewingFile) await this._loadReport();
    else this._render();
  }

  async _rate(entityId, severity) {
    try {
      await this._ws("set_severity", { entity_id: entityId, severity });
      this._error = null;
      this._notice = STATE.tab === "whatif" || STATE.viewingFile
        ? `Rating saved for ${entityId}. It applies to the latest report and new reports.`
        : null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    if (STATE.tab === "whatif" && STATE.whatif && STATE.wiStart && STATE.wiEnd) await this._runWhatIf();
    else if (!STATE.viewingFile) await this._loadReport();
    else this._render();
  }

  async _createLabels() {
    try {
      const res = await this._ws("create_severity_labels");
      this._notice = res.created?.length ? `Created: ${res.created.join(", ")}` : "All severity labels already exist.";
      this._error = null;
    } catch (e) { this._error = e.message || String(e); }
    this._render();
  }

  async _runWhatIf() {
    this._wiBusy = true; this._render();
    try {
      STATE.whatif = await this._ws("what_if", { start: STATE.wiStart.replace("T", " ") + ":00", end: STATE.wiEnd.replace("T", " ") + ":00" });
      this._error = null;
    } catch (e) { this._error = e.message || String(e); }
    this._wiBusy = false; this._render();
  }
}

const STYLE = `
:host {
  display: block; height: 100%;
  /* Severity is the only colored attribute. */
  --da-sev-critical: #d64545; --da-sev-high: #e8833a; --da-sev-medium: #c99a12; --da-sev-low: #607d8b; --da-sev-none: #9aa0a6;
  --da-bar-clean: #7a8794; --da-bar-unclean: #a1584f;
  --da-ok: #2e9d57; --da-bad: #d64545; --da-info: #3b82c4;
  --da-bg: var(--primary-background-color, #f5f6f8);
  --da-card: var(--card-background-color, #fff);
  --da-text: var(--primary-text-color, #1f2328);
  --da-muted: var(--secondary-text-color, #6b7280);
  --da-line: var(--divider-color, rgba(0,0,0,.12));
  --da-accent: var(--primary-color, #03a9f4);
  --da-radius: var(--ha-card-border-radius, 12px);
  background: var(--da-bg); color: var(--da-text);
  font-family: var(--paper-font-body1_-_font-family, Roboto, system-ui, sans-serif);
}
* { box-sizing: border-box; }
a { color: var(--da-accent); text-decoration: none; }
.toolbar { display: flex; align-items: center; gap: 8px; height: 56px; padding: 0 12px;
  background: var(--app-header-background-color, var(--da-card)); color: var(--app-header-text-color, var(--da-text));
  border-bottom: 1px solid var(--app-header-border-bottom, var(--da-line)); }
.title { flex: 1; font-size: 20px; font-weight: 400; margin-left: 4px; }
.icon-btn { background: none; border: 0; color: inherit; cursor: pointer; padding: 8px; border-radius: 50%; }
.icon-btn:hover { background: var(--da-line); }
.tabs { display: flex; gap: 4px; padding: 0 12px; background: var(--da-card); border-bottom: 1px solid var(--da-line); overflow-x: auto; }
.tab { display: flex; align-items: center; gap: 6px; background: none; border: 0; border-bottom: 2px solid transparent;
  color: var(--da-muted); padding: 12px 14px; font: inherit; font-size: 14px; cursor: pointer; white-space: nowrap; }
.tab.active { color: var(--da-accent); border-bottom-color: var(--da-accent); }
.tab ha-icon { --mdc-icon-size: 18px; }
.content { height: calc(100% - 105px); overflow-y: auto; padding: 16px; max-width: 1200px; margin: 0 auto; }
.card { background: var(--da-card); border-radius: var(--da-radius); padding: 16px; margin-bottom: 16px;
  box-shadow: var(--ha-card-box-shadow, 0 1px 2px rgba(0,0,0,.08)); border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--da-line)); }
h2, h3 { margin: 0 0 12px; font-weight: 500; }
h3 { font-size: 16px; }
.muted { color: var(--da-muted); font-weight: 400; }
.mono, code, pre { font-family: var(--code-font-family, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 12px; }
code { background: var(--da-bg); padding: 1px 5px; border-radius: 4px; word-break: break-all; }
.banner { background: color-mix(in srgb, var(--da-accent) 12%, var(--da-card)); border-radius: 8px; padding: 10px 14px; margin-bottom: 16px; font-size: 14px; }
.banner.err { background: color-mix(in srgb, var(--da-bad) 14%, var(--da-card)); }
.hero { display: flex; flex-wrap: wrap; gap: 16px 32px; align-items: center; }
.hero-main { flex: 1 1 280px; }
.hero-side { flex: 1 1 280px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.label { text-transform: uppercase; font-size: 11px; letter-spacing: .08em; color: var(--da-muted); }
.dur { font-size: 40px; font-weight: 300; line-height: 1.15; }
.dur.warn, .warn { color: var(--da-sev-high); }
.range { color: var(--da-muted); font-size: 14px; }
.arrow { color: var(--da-muted); margin: 0 4px; }
.meta { font-size: 13px; color: var(--da-muted); display: flex; flex-direction: column; gap: 3px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; font-size: 13px; font-weight: 500; }
.chip ha-icon { --mdc-icon-size: 16px; }
.chip.ok { background: color-mix(in srgb, var(--da-ok) 15%, transparent); color: var(--da-ok); }
.chip.bad { background: color-mix(in srgb, var(--da-bad) 15%, transparent); color: var(--da-bad); }
.chip.info { background: color-mix(in srgb, var(--da-info) 15%, transparent); color: var(--da-info); }
.chip.sm { padding: 1px 8px; font-size: 12px; }
.sev-badge { display: inline-block; font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; white-space: nowrap;
  color: var(--s); background: color-mix(in srgb, var(--s) 16%, transparent); border: 1px solid color-mix(in srgb, var(--s) 45%, transparent); }
.sev-badge.lg { font-size: 13px; padding: 4px 10px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile { text-align: left; background: var(--da-card); border: 1px solid var(--da-line); border-radius: var(--da-radius); padding: 12px 14px;
  cursor: pointer; color: var(--da-text); font: inherit; opacity: .55; transition: opacity .15s; }
.tile.on { opacity: 1; border-color: var(--da-accent); }
.tile ha-icon { color: var(--da-muted); --mdc-icon-size: 20px; }
.tile .n { font-size: 28px; font-weight: 400; margin-top: 2px; }
.tile .l { font-size: 13px; color: var(--da-muted); }
.timeline, .hist { width: 100%; height: auto; display: block; }
.hist .bar-g { cursor: pointer; }
.band { fill: color-mix(in srgb, var(--da-muted) 7%, transparent); stroke: color-mix(in srgb, var(--da-muted) 25%, transparent); }
.axis { stroke: var(--da-line); }
svg text { fill: var(--da-muted); font-size: 11px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--da-muted); margin-top: 8px; align-items: center; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }
.legend .sep { width: 1px; height: 14px; background: var(--da-line); }
.list-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.list-head h3 { margin: 0; }
.search { flex: 0 1 320px; min-width: 180px; padding: 8px 12px; border: 1px solid var(--da-line); border-radius: 8px; background: var(--da-bg); color: var(--da-text); font: inherit; font-size: 14px; }
.filters { display: flex; flex-wrap: wrap; gap: 10px 20px; align-items: flex-end; padding: 8px 0 4px; border-bottom: 1px solid var(--da-line); margin-bottom: 4px; }
.fl { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--da-muted); }
.fl.toggle { flex-direction: row; align-items: center; gap: 6px; color: var(--da-text); font-size: 13px; }
.fl select, .sev-select { padding: 6px 8px; border: 1px solid var(--da-line); border-radius: 8px; background: var(--da-bg); color: var(--da-text); font: inherit; font-size: 13px; }
.meter { display: inline-flex; gap: 2px; vertical-align: middle; }
.meter i { width: 6px; height: 6px; border-radius: 50%; border: 1px solid var(--da-muted); }
.meter i.f { background: var(--da-muted); }
.row { border: 1px solid var(--da-line); border-left: 4px solid var(--s); border-radius: 8px; margin: 8px 0; overflow: hidden; }
.row.dim { opacity: .7; }
.row-head { display: flex; gap: 12px; align-items: flex-start; width: 100%; background: none; border: 0; padding: 10px 12px; text-align: left; cursor: pointer; color: var(--da-text); font: inherit; }
.row-head:hover { background: color-mix(in srgb, var(--s) 6%, transparent); }
.type-ico { color: var(--da-muted); --mdc-icon-size: 20px; margin-top: 2px; flex: none; }
.row-main { flex: 1; min-width: 0; }
.row-title { font-weight: 500; font-size: 14px; overflow-wrap: anywhere; }
svg text.count { font-size: 10px; }
.late { margin-top: 12px; padding: 10px 12px; border: 1px dashed var(--da-line); border-radius: 8px; font-size: 13px; }
.late ha-icon { --mdc-icon-size: 18px; vertical-align: -4px; margin-right: 4px; color: var(--da-muted); }
.late ul { margin: 6px 0 0; padding-left: 22px; }
.late li { margin: 2px 0; overflow-wrap: anywhere; }
.trig-list { display: flex; flex-direction: column; gap: 8px; }
.trig { border: 1px solid var(--da-line); border-radius: 8px; padding: 8px 10px; }
.trig-head { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.trig-sum { margin: 4px 0 2px; overflow-wrap: anywhere; }
.trig-rate { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; color: var(--da-muted); }
.trig .kv { grid-template-columns: 100px 1fr; }
.conf-inline { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--da-muted); }
.live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-left: 6px; vertical-align: 2px;
  background: var(--da-ok); box-shadow: 0 0 0 0 color-mix(in srgb, var(--da-ok) 60%, transparent); animation: da-pulse 2s infinite; }
@keyframes da-pulse { 70% { box-shadow: 0 0 0 6px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
.banner { display: flex; gap: 10px; align-items: center; }
.banner ha-icon { --mdc-icon-size: 20px; flex: none; }
.banner span { flex: 1; }
.warn-banner { background: color-mix(in srgb, var(--da-sev-high) 16%, var(--da-card)); }
.btn.sm { padding: 5px 14px; font-size: 13px; }
.row-title .mono { margin-left: 6px; }
.row-sum { font-size: 13px; color: var(--da-muted); margin-top: 2px; overflow-wrap: anywhere; }
.row-tags { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; justify-content: flex-end; flex: none; max-width: 45%; }
.tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--da-bg); color: var(--da-muted); white-space: nowrap; }
.tag.conf { display: inline-flex; align-items: center; gap: 4px; }
.row-body { padding: 4px 16px 12px 44px; font-size: 13px; }
.kv { display: grid; grid-template-columns: 130px 1fr; gap: 8px; padding: 6px 0; border-bottom: 1px dashed var(--da-line); }
.k { color: var(--da-muted); }
.sev-line { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.small-inline { font-size: 12px; margin-left: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.mini { background: var(--da-bg); border-radius: 4px; padding: 1px 6px; font-family: var(--code-font-family, monospace); font-size: 12px; }
.ba { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.cond-head { font-weight: 500; }
.cond-tree, .cond-tree ul { list-style: none; margin: 4px 0 0; padding-left: 0; }
.cond-tree ul { padding-left: 22px; }
.cs { margin: 3px 0; }
.cs ha-icon { --mdc-icon-size: 16px; vertical-align: -3px; margin-right: 4px; color: var(--da-muted); }
.run { background: var(--da-bg); border-radius: 6px; padding: 8px 10px; margin: 8px 0; display: flex; flex-direction: column; gap: 3px; }
.links { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; }
.links a { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; }
.links ha-icon { --mdc-icon-size: 16px; }
.raw summary { cursor: pointer; color: var(--da-muted); margin-top: 8px; }
.raw pre { background: var(--da-bg); padding: 8px; border-radius: 6px; overflow: auto; max-height: 320px; }
.all-good { display: flex; align-items: center; gap: 8px; color: var(--da-ok); padding: 12px 4px; }
.empty { color: var(--da-muted); padding: 24px; text-align: center; }
.empty-card { text-align: center; padding: 40px 24px; }
.empty-card .big { --mdc-icon-size: 48px; color: var(--da-muted); }
.foot { font-size: 12px; margin-top: 10px; }
.small { font-size: 12px; margin: 0 0 8px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 8px; border-bottom: 1px solid var(--da-line); }
.tbl th { color: var(--da-muted); font-weight: 500; }
.tbl th ha-icon { --mdc-icon-size: 16px; }
.tbl td.hot { font-weight: 600; }
.wi { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.wi label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--da-muted); }
.wi input { padding: 8px 10px; border: 1px solid var(--da-line); border-radius: 8px; background: var(--da-bg); color: var(--da-text); font: inherit; }
.btn { background: var(--da-accent); color: var(--text-primary-color, #fff); border: 0; border-radius: 8px; padding: 9px 20px; font: inherit; font-weight: 500; cursor: pointer; }
.btn.secondary { background: var(--da-bg); color: var(--da-text); border: 1px solid var(--da-line); }
.presets { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip-btn { background: var(--da-bg); border: 1px solid var(--da-line); color: var(--da-text); border-radius: 999px; padding: 5px 12px; font: inherit; font-size: 13px; cursor: pointer; }
.chip-btn.sm { padding: 3px 10px; font-size: 12px; opacity: .55; display: inline-flex; align-items: center; gap: 4px; }
.chip-btn.sm.on { opacity: 1; border-color: var(--da-accent); }
@media (max-width: 600px) {
  .content { padding: 8px; }
  .row-tags .tag:not(.conf) { display: none; }
  .row-tags { max-width: 40%; }
  .row-body { padding-left: 12px; }
  .kv { grid-template-columns: 1fr; gap: 2px; }
  .dur { font-size: 32px; }
  .tbl th:nth-child(n+5), .tbl td:nth-child(n+5):not(:last-child) { display: none; }
}
`;

customElements.define("downtime-auditor-panel", DowntimeAuditorPanel);
