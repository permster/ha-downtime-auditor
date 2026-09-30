/* Downtime Auditor sidebar dashboard. Plain web component, no build step. */

const CATS = [
  { key: "interrupted", label: "Interrupted", icon: "mdi:motion-pause-outline", color: "var(--da-red)" },
  { key: "missed", label: "Missed", icon: "mdi:calendar-remove-outline", color: "var(--da-orange)" },
  { key: "possibly_missed", label: "Possibly missed", icon: "mdi:help-circle-outline", color: "var(--da-amber)" },
  { key: "fired_during_startup", label: "Fired at startup", icon: "mdi:rocket-launch-outline", color: "var(--da-blue)" },
  { key: "unverifiable", label: "Unverifiable", icon: "mdi:eye-off-outline", color: "var(--da-grey)" },
];
const CAT = Object.fromEntries(CATS.map((c) => [c.key, c]));
const TABS = [
  { key: "report", label: "Last report", icon: "mdi:clipboard-text-clock-outline" },
  { key: "history", label: "History", icon: "mdi:history" },
  { key: "live", label: "Live status", icon: "mdi:heart-pulse" },
  { key: "whatif", label: "What-if", icon: "mdi:flask-outline" },
];

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

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

class DowntimeAuditorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "report";
    this._filter = new Set(["interrupted", "missed", "possibly_missed", "fired_during_startup"]);
    this._search = "";
    this._open = new Set();
    this._report = null;
    this._viewingFile = null;
    this._history = null;
    this._status = null;
    this._whatif = null;
    this._error = null;
    this._loaded = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.hass = hass;
  }
  get hass() { return this._hass; }
  set narrow(v) {
    this._narrow = v;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.narrow = v;
  }
  set panel(p) { this._panel = p; }

  async _ws(type, extra = {}) {
    return this._hass.connection.sendMessagePromise({ type: `downtime_auditor/${type}`, ...extra });
  }

  async _init() {
    this._render();
    await this._loadReport();
    try {
      this._unsub = await this._hass.connection.subscribeEvents(() => {
        if (!this._viewingFile) this._loadReport();
        this._history = null;
        if (this._tab === "history") this._loadHistory();
      }, "downtime_auditor_report");
    } catch (e) { /* non-admin or older HA */ }
    this._tick = setInterval(() => { if (this._tab === "live") this._loadStatus(); }, 10000);
  }

  disconnectedCallback() {
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    if (this._unsub) this._unsub();
    clearInterval(this._tick);
    this._unsub = null;
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
      this._viewingFile = file || null;
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._loaded = true;
    this._render();
  }
  async _loadHistory() {
    try { this._history = await this._ws("history", { limit: 200 }); } catch (e) { this._error = e.message; }
    this._render();
  }
  async _loadStatus() {
    try { this._status = await this._ws("status"); } catch (e) { this._error = e.message; }
    this._render();
  }

  _setTab(t) {
    this._tab = t;
    if (t === "history" && !this._history) this._loadHistory();
    if (t === "live") this._loadStatus();
    this._render();
  }

  _navigate(path) {
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }
  _moreInfo(entityId) {
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
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
        ${TABS.map((t) => `<button role="tab" class="tab ${this._tab === t.key ? "active" : ""}" data-tab="${t.key}">
          <ha-icon icon="${t.icon}"></ha-icon><span>${t.label}</span></button>`).join("")}
      </div>
      <div class="content">${this._error ? `<div class="banner err">${esc(this._error)}</div>` : ""}${this._body()}</div>`;
    const mb = r.querySelector("ha-menu-button");
    if (mb) { mb.hass = this._hass; mb.narrow = this._narrow; }
    r.querySelector(".content").scrollTop = scrollY;
    this._bind();
  }

  _body() {
    if (!this._loaded) return `<div class="empty">Loading…</div>`;
    switch (this._tab) {
      case "history": return this._historyView();
      case "live": return this._liveView();
      case "whatif": return this._whatIfView();
      default: return this._reportView(this._report, { viewingFile: this._viewingFile });
    }
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
    const findings = (rep.findings || []).filter((f) => f.category !== "skipped");
    const skipped = (rep.findings || []).filter((f) => f.category === "skipped").length;
    const shown = findings.filter((f) => this._filter.has(f.category) && this._matches(f));
    const verBump = meta.ha_version_before && meta.ha_version_before !== meta.ha_version_after;

    return `
      ${viewingFile ? `<div class="banner">Viewing an older report from ${esc(fmtTime(rep.generated_at))}.
        <a href="#" data-act="latest">Back to latest</a></div>` : ""}
      ${whatIf ? `<div class="banner">What-if simulation — only time, time-pattern, sun and calendar triggers are evaluated.</div>` : ""}
      <div class="card hero">
        <div class="hero-main">
          <div class="label">${whatIf ? "Simulated window" : "Last downtime"}</div>
          <div class="dur">${esc(w.duration)}</div>
          <div class="range">${esc(fmtTime(w.start))} <span class="arrow">→</span> ${esc(fmtTime(w.end))}</div>
        </div>
        <div class="hero-side">
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
        ${CATS.map((c) => `<button class="tile ${this._filter.has(c.key) ? "on" : ""}" data-filter="${c.key}" style="--c:${c.color}">
          <ha-icon icon="${c.icon}"></ha-icon>
          <div class="n">${counts[c.key] || 0}</div><div class="l">${c.label}</div></button>`).join("")}
      </div>

      ${this._timeline(rep, findings)}

      <div class="card">
        <div class="list-head">
          <h3>Findings <span class="muted">(${shown.length} of ${findings.length})</span></h3>
          <input class="search" type="search" placeholder="Search name, entity, summary…" value="${esc(this._search)}">
        </div>
        ${!findings.length ? `<div class="all-good"><ha-icon icon="mdi:shield-check-outline"></ha-icon>
            Nothing was missed or interrupted${whatIf ? " in this window" : ""}.</div>` : ""}
        ${shown.map((f, i) => this._row(f, `${rep.generated_at}|${i}|${f.entity_id}|${f.trigger_index}|${f.category}`)).join("")}
        ${findings.length && !shown.length ? `<div class="empty">No findings match the current filter.</div>` : ""}
        ${skipped ? `<div class="foot muted">${skipped} automation(s) were off before the downtime and were skipped.</div>` : ""}
        <div class="foot muted">Conditions are not evaluated — a missed trigger may not have passed its conditions.</div>
      </div>`;
  }

  _chartWidth() {
    // Draw charts at their real pixel width so text and dots don't get stretched.
    const w = (this.clientWidth || 1000) - (this.clientWidth < 600 ? 16 : 32) - 34;
    return Math.max(280, Math.min(1134, w));
  }

  _matches(f) {
    if (!this._search) return true;
    const q = this._search.toLowerCase();
    return [f.name, f.entity_id, f.summary, f.platform, f.trigger_id].some((v) => String(v ?? "").toLowerCase().includes(q));
  }

  _timeline(rep, findings) {
    const w = rep.window;
    const t0 = new Date(w.start).getTime(), t1 = new Date(w.end).getTime();
    if (!(t1 > t0)) return "";
    const W = this._chartWidth(), H = 96, pad = 24, x = (t) => pad + ((t - t0) / (t1 - t0)) * (W - 2 * pad);
    const lanes = { interrupted: 22, missed: 44, possibly_missed: 60, fired_during_startup: 76 };
    const marks = [];
    for (const f of findings) {
      if (f.category === "interrupted") {
        marks.push(`<g><title>${esc(f.name)} — interrupted</title><rect x="${x(t0) - 5}" y="${lanes.interrupted - 5}" width="10" height="10" transform="rotate(45 ${x(t0)} ${lanes.interrupted})" fill="var(--da-red)"/></g>`);
      } else if ((f.category === "missed" || f.category === "possibly_missed") && f.occurrences?.length) {
        const y = lanes[f.category];
        const occ = f.occurrences_iso?.length ? f.occurrences_iso : f.occurrences;
        for (const o of occ.slice(0, 200)) {
          const d = f.occurrences_iso?.length ? new Date(o) : parseLocal(o);
          if (!d) continue;
          const t = d.getTime();
          if (t < t0 - 1000 || t > t1 + 1000) continue;
          marks.push(`<circle cx="${x(t)}" cy="${y}" r="4.5" fill="${CAT[f.category].color}"><title>${esc(f.name)} — due ${esc(fmtTime(d.toISOString()))}</title></circle>`);
        }
      } else if (f.category === "missed" || f.category === "possibly_missed") {
        marks.push(`<g><title>${esc(f.name)} — changed during downtime</title><line x1="${x(t1) - 3}" x2="${x(t1) + 3}" y1="${lanes[f.category] - 5}" y2="${lanes[f.category] + 5}" stroke="${CAT[f.category].color}" stroke-width="3"/></g>`);
      }
    }
    const ticks = [];
    const n = W < 560 ? 2 : W < 900 ? 4 : 6;
    for (let i = 0; i <= n; i++) {
      const t = t0 + ((t1 - t0) * i) / n;
      ticks.push(`<text x="${x(t)}" y="${H - 2}" text-anchor="${i === 0 ? "start" : i === n ? "end" : "middle"}">${esc(fmtShort(new Date(t).toISOString()))}</text>`);
    }
    return `<div class="card">
      <h3>Timeline</h3>
      <svg class="timeline" viewBox="0 0 ${W} ${H}" role="img" aria-label="Downtime timeline">
        <rect x="${pad}" y="10" width="${W - 2 * pad}" height="${H - 34}" rx="6" class="band"/>
        <line x1="${pad}" x2="${W - pad}" y1="${H - 24}" y2="${H - 24}" class="axis"/>
        ${ticks.join("")}
        ${marks.join("")}
      </svg>
      <div class="legend">
        <span><i style="background:var(--da-red)"></i>Interrupted (at shutdown)</span>
        <span><i style="background:var(--da-orange)"></i>Missed — scheduled time</span>
        <span><i style="background:var(--da-amber)"></i>Possibly missed</span>
        <span><i class="bar" style="background:var(--da-orange)"></i>State change (seen at startup)</span>
      </div>
    </div>`;
  }

  _row(f, key) {
    const c = CAT[f.category] || { color: "var(--da-grey)", label: f.category, icon: "mdi:alert" };
    const open = this._open.has(key);
    const d = f.details || {};
    const isScript = String(f.entity_id || "").startsWith("script.");
    const links = [];
    if (f.item_id) {
      links.push(`<a href="#" data-nav="/config/${isScript ? "script" : "automation"}/edit/${esc(f.item_id)}"><ha-icon icon="mdi:pencil"></ha-icon>Edit</a>`);
      links.push(`<a href="#" data-nav="/config/${isScript ? "script" : "automation"}/trace/${esc(f.item_id)}"><ha-icon icon="mdi:timeline-text-outline"></ha-icon>Traces</a>`);
    }
    links.push(`<a href="#" data-more="${esc(f.entity_id)}"><ha-icon icon="mdi:information-outline"></ha-icon>Details</a>`);
    if (d.entity) links.push(`<a href="#" data-more="${esc(d.entity)}"><ha-icon icon="mdi:radar"></ha-icon>${esc(d.entity)}</a>`);
    const trig = f.trigger_id ? `trigger “${esc(f.trigger_id)}”` : f.trigger_index != null ? `trigger #${f.trigger_index}` : "";
    return `<div class="row ${open ? "open" : ""}" style="--c:${c.color}">
      <button class="row-head" data-toggle="${esc(key)}" aria-expanded="${open}">
        <ha-icon icon="${c.icon}" class="cat-ico"></ha-icon>
        <div class="row-main">
          <div class="row-title">${esc(f.name)} <span class="muted mono">${esc(f.entity_id)}</span></div>
          <div class="row-sum">${esc(f.summary)}</div>
        </div>
        <div class="row-tags">
          ${f.platform ? `<span class="tag">${esc(f.platform)}</span>` : ""}
          ${trig ? `<span class="tag">${trig}</span>` : ""}
          <span class="tag conf-${esc(f.confidence)}">${esc(f.confidence)}</span>
          <ha-icon icon="${open ? "mdi:chevron-up" : "mdi:chevron-down"}"></ha-icon>
        </div>
      </button>
      ${open ? `<div class="row-body">${this._details(f)}<div class="links">${links.join("")}</div></div>` : ""}
    </div>`;
  }

  _details(f) {
    const d = f.details || {};
    const parts = [];
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
    parts.push(`<details class="raw"><summary>Raw finding</summary><pre>${esc(JSON.stringify(f, null, 2))}</pre></details>`);
    return parts.join("");
  }

  _historyView() {
    const h = this._history;
    if (!h) return `<div class="empty">Loading history…</div>`;
    if (!h.length) return `<div class="card empty-card"><h2>No history yet</h2><p>Each restart adds an entry here (requires “Write JSON reports”).</p></div>`;
    const items = h.slice(0, 40).slice().reverse();
    const max = Math.max(...items.map((i) => i.window?.duration_seconds || 0), 1);
    const W = this._chartWidth(), H = 160, bw = Math.min(56, (W - 40) / items.length);
    const bars = items.map((it, i) => {
      const v = it.window?.duration_seconds || 0;
      const bh = Math.max(2, (Math.sqrt(v) / Math.sqrt(max)) * (H - 40));
      const act = (it.counts?.interrupted || 0) + (it.counts?.missed || 0);
      const fill = it.window?.clean_shutdown ? "var(--da-blue)" : "var(--da-red)";
      return `<g class="bar-g" data-file="${esc(it.available ? it.file : "")}">
        <title>${esc(fmtTime(it.window?.start))}: ${esc(it.window?.duration)} · ${act} missed/interrupted</title>
        <rect x="${20 + i * bw + bw * 0.15}" y="${H - 20 - bh}" width="${bw * 0.7}" height="${bh}" rx="3" fill="${fill}"/>
        ${act ? `<circle cx="${20 + i * bw + bw / 2}" cy="${H - 26 - bh}" r="4" fill="var(--da-orange)"/>` : ""}
      </g>`;
    }).join("");
    return `<div class="card">
        <h3>Downtime durations <span class="muted">(last ${items.length}, √ scale)</span></h3>
        <svg class="hist" viewBox="0 0 ${W} ${H}">${bars}
          <line x1="20" x2="${W - 20}" y1="${H - 20}" y2="${H - 20}" class="axis"/></svg>
        <div class="legend"><span><i style="background:var(--da-blue)"></i>Clean</span><span><i style="background:var(--da-red)"></i>Unclean</span><span><i class="dot" style="background:var(--da-orange)"></i>Had missed / interrupted</span></div>
      </div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Went down</th><th>Duration</th><th>Shutdown</th>${CATS.slice(0, 4).map((c) => `<th title="${c.label}"><ha-icon icon="${c.icon}"></ha-icon></th>`).join("")}<th></th></tr></thead>
        <tbody>${h.map((it) => `<tr>
          <td>${esc(fmtTime(it.window?.start))}</td><td>${esc(it.window?.duration)}</td>
          <td><span class="chip sm ${it.window?.clean_shutdown ? "ok" : "bad"}">${it.window?.clean_shutdown ? "clean" : "unclean"}</span></td>
          ${CATS.slice(0, 4).map((c) => `<td class="${it.counts?.[c.key] ? "hot" : "muted"}">${it.counts?.[c.key] || 0}</td>`).join("")}
          <td>${it.available ? `<a href="#" data-file="${esc(it.file)}">Open</a>` : `<span class="muted">pruned</span>`}</td></tr>`).join("")}
        </tbody></table></div>`;
  }

  _liveView() {
    const s = this._status;
    if (!s) return `<div class="empty">Loading…</div>`;
    const hbAge = s.last_heartbeat ? (Date.now() - new Date(s.last_heartbeat).getTime()) / 1000 : Infinity;
    const healthy = s.tracking && hbAge < (s.heartbeat_interval || 60) * 2.5;
    return `<div class="card hero">
        <div class="hero-main"><div class="label">Tracking</div>
          <div class="dur ${healthy ? "" : "warn"}">${s.tracking ? (healthy ? "Healthy" : "Stale") : "Waiting for startup"}</div>
          <div class="range">Last heartbeat ${esc(fmtAgo(s.last_heartbeat))} · every ${esc(s.heartbeat_interval)}s</div></div>
        <div class="hero-side meta">
          <div>Session started ${esc(fmtTime(s.session_started))}</div>
          <div><b>${s.baseline_automations}</b> automations · <b>${s.baseline_entities}</b> trigger entities · <b>${s.baseline_templates}</b> templates in baseline</div>
          <div>Startup settle delay ${esc(s.startup_delay)}s · JSON reports ${s.json_enabled ? "on" : "off"}</div>
        </div></div>
      <div class="card"><h3>Running right now <span class="muted">(${s.running_now.length})</span></h3>
        <p class="muted">These would be reported as interrupted if Home Assistant stopped this instant.</p>
        ${s.running_now.length ? s.running_now.map((r, i) => this._row({
          category: "interrupted", confidence: "live", name: r.name, entity_id: r.entity_id, item_id: r.item_id, platform: r.domain,
          summary: (r.runs || []).map((x) => `at ${x.last_step || "?"}${x.trigger ? ` · ${x.trigger}` : ""}`).join(" | ") || `${r.current_runs} run(s)`,
          details: { mode: r.mode, runs: r.runs },
        }, `live|${i}|${r.entity_id}`)).join("") : `<div class="all-good"><ha-icon icon="mdi:sleep"></ha-icon>Nothing is running.</div>`}
      </div>`;
  }

  _whatIfView() {
    const pad = (n) => String(n).padStart(2, "0");
    const toInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const now = new Date();
    const lastNight = new Date(now); lastNight.setHours(1, 0, 0, 0); if (lastNight > now) lastNight.setDate(lastNight.getDate() - 1);
    const s = this._wiStart || toInput(new Date(now.getTime() - 4 * 3600e3));
    const e = this._wiEnd || toInput(now);
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
      ${this._wiBusy ? `<div class="empty">Analysing…</div>` : this._whatif ? this._reportView(this._whatif, { whatIf: true }) : ""}`;
  }

  // ---------------------------------------------------------------- events

  _bind() {
    const r = this.shadowRoot;
    r.querySelectorAll("[data-tab]").forEach((el) => el.addEventListener("click", (ev) => { ev.preventDefault(); this._setTab(el.dataset.tab); }));
    r.querySelectorAll("[data-filter]").forEach((el) => el.addEventListener("click", () => {
      const k = el.dataset.filter;
      this._filter.has(k) ? this._filter.delete(k) : this._filter.add(k);
      this._render();
    }));
    r.querySelectorAll("[data-toggle]").forEach((el) => el.addEventListener("click", () => {
      const k = el.dataset.toggle;
      this._open.has(k) ? this._open.delete(k) : this._open.add(k);
      this._render();
    }));
    r.querySelectorAll("[data-nav]").forEach((el) => el.addEventListener("click", (ev) => { ev.preventDefault(); this._navigate(el.dataset.nav); }));
    r.querySelectorAll("[data-more]").forEach((el) => el.addEventListener("click", (ev) => { ev.preventDefault(); this._moreInfo(el.dataset.more); }));
    r.querySelectorAll("[data-file]").forEach((el) => el.addEventListener("click", (ev) => {
      ev.preventDefault();
      if (!el.dataset.file) return;
      this._tab = "report";
      this._loadReport(el.dataset.file);
    }));
    r.querySelectorAll("[data-preset]").forEach((el) => el.addEventListener("click", () => {
      [this._wiStart, this._wiEnd] = el.dataset.preset.split("|");
      this._runWhatIf();
    }));
    const search = r.querySelector(".search");
    if (search) search.addEventListener("input", (ev) => {
      this._search = ev.target.value;
      clearTimeout(this._st);
      this._st = setTimeout(() => { this._render(); const s = this.shadowRoot.querySelector(".search"); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }, 200);
    });
    r.querySelectorAll("[data-act]").forEach((el) => el.addEventListener("click", (ev) => {
      ev.preventDefault();
      const a = el.dataset.act;
      if (a === "refresh") {
        this._history = null;
        if (this._tab === "history") this._loadHistory();
        else if (this._tab === "live") this._loadStatus();
        else this._loadReport(this._viewingFile);
      } else if (a === "latest") this._loadReport();
      else if (a === "whatif") {
        this._wiStart = r.querySelector("#wi-start").value;
        this._wiEnd = r.querySelector("#wi-end").value;
        this._runWhatIf();
      }
    }));
  }

  async _runWhatIf() {
    this._wiBusy = true; this._render();
    try {
      this._whatif = await this._ws("what_if", { start: this._wiStart.replace("T", " ") + ":00", end: this._wiEnd.replace("T", " ") + ":00" });
      this._error = null;
    } catch (e) { this._error = e.message || String(e); }
    this._wiBusy = false; this._render();
  }
}

const STYLE = `
:host {
  display: block; height: 100%;
  --da-red: #d64545; --da-orange: #e8833a; --da-amber: #d9a520; --da-blue: #3b82c4; --da-grey: #8a8f98;
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
.banner.err { background: color-mix(in srgb, var(--da-red) 14%, var(--da-card)); }
.hero { display: flex; flex-wrap: wrap; gap: 16px 32px; align-items: center; }
.hero-main { flex: 1 1 280px; }
.hero-side { flex: 1 1 280px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.label { text-transform: uppercase; font-size: 11px; letter-spacing: .08em; color: var(--da-muted); }
.dur { font-size: 40px; font-weight: 300; line-height: 1.15; }
.dur.warn, .warn { color: var(--da-orange); }
.range { color: var(--da-muted); font-size: 14px; }
.arrow { color: var(--da-muted); margin: 0 4px; }
.meta { font-size: 13px; color: var(--da-muted); display: flex; flex-direction: column; gap: 3px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; font-size: 13px; font-weight: 500; }
.chip ha-icon { --mdc-icon-size: 16px; }
.chip.ok { background: color-mix(in srgb, #2e9d57 15%, transparent); color: #2e9d57; }
.chip.bad { background: color-mix(in srgb, var(--da-red) 15%, transparent); color: var(--da-red); }
.chip.info { background: color-mix(in srgb, var(--da-blue) 15%, transparent); color: var(--da-blue); }
.chip.sm { padding: 1px 8px; font-size: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 16px; }
.tile { text-align: left; background: var(--da-card); border: 1px solid var(--da-line); border-radius: var(--da-radius); padding: 12px 14px;
  cursor: pointer; color: var(--da-text); font: inherit; position: relative; overflow: hidden; opacity: .55; transition: opacity .15s; }
.tile.on { opacity: 1; border-color: var(--c); }
.tile::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--c); }
.tile ha-icon { color: var(--c); --mdc-icon-size: 20px; }
.tile .n { font-size: 28px; font-weight: 400; margin-top: 2px; }
.tile .l { font-size: 13px; color: var(--da-muted); }
.timeline, .hist { width: 100%; height: auto; display: block; }
.hist .bar-g { cursor: pointer; }
.band { fill: color-mix(in srgb, var(--da-red) 7%, transparent); stroke: color-mix(in srgb, var(--da-red) 25%, transparent); }
.axis { stroke: var(--da-line); }
svg text { fill: var(--da-muted); font-size: 11px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--da-muted); margin-top: 8px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }
.legend i.bar { width: 3px; height: 12px; border-radius: 1px; }
.list-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.list-head h3 { margin: 0; }
.search { flex: 0 1 320px; min-width: 180px; padding: 8px 12px; border: 1px solid var(--da-line); border-radius: 8px; background: var(--da-bg); color: var(--da-text); font: inherit; font-size: 14px; }
.row { border: 1px solid var(--da-line); border-left: 4px solid var(--c); border-radius: 8px; margin: 8px 0; overflow: hidden; }
.row-head { display: flex; gap: 12px; align-items: flex-start; width: 100%; background: none; border: 0; padding: 10px 12px; text-align: left; cursor: pointer; color: var(--da-text); font: inherit; }
.row-head:hover { background: color-mix(in srgb, var(--c) 6%, transparent); }
.cat-ico { color: var(--c); --mdc-icon-size: 20px; margin-top: 2px; flex: none; }
.row-main { flex: 1; min-width: 0; }
.row-title { font-weight: 500; font-size: 14px; }
.row-title .mono { margin-left: 6px; }
.row-sum { font-size: 13px; color: var(--da-muted); margin-top: 2px; overflow-wrap: anywhere; }
.row-tags { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; justify-content: flex-end; flex: none; max-width: 40%; }
.tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--da-bg); color: var(--da-muted); white-space: nowrap; }
.tag.conf-high { color: var(--da-red); }
.tag.conf-medium { color: var(--da-orange); }
.row-body { padding: 4px 16px 12px 44px; font-size: 13px; }
.kv { display: grid; grid-template-columns: 110px 1fr; gap: 8px; padding: 6px 0; border-bottom: 1px dashed var(--da-line); }
.k { color: var(--da-muted); }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.mini { background: var(--da-bg); border-radius: 4px; padding: 1px 6px; font-family: var(--code-font-family, monospace); font-size: 12px; }
.ba { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.run { background: var(--da-bg); border-radius: 6px; padding: 8px 10px; margin: 8px 0; display: flex; flex-direction: column; gap: 3px; }
.links { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; }
.links a { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; }
.links ha-icon { --mdc-icon-size: 16px; }
.raw summary { cursor: pointer; color: var(--da-muted); margin-top: 8px; }
.raw pre { background: var(--da-bg); padding: 8px; border-radius: 6px; overflow: auto; max-height: 320px; }
.all-good { display: flex; align-items: center; gap: 8px; color: #2e9d57; padding: 12px 4px; }
.empty { color: var(--da-muted); padding: 24px; text-align: center; }
.empty-card { text-align: center; padding: 40px 24px; }
.empty-card .big { --mdc-icon-size: 48px; color: var(--da-muted); }
.foot { font-size: 12px; margin-top: 10px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 8px; border-bottom: 1px solid var(--da-line); }
.tbl th { color: var(--da-muted); font-weight: 500; }
.tbl th ha-icon { --mdc-icon-size: 16px; }
.tbl td.hot { color: var(--da-orange); font-weight: 600; }
.wi { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.wi label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--da-muted); }
.wi input { padding: 8px 10px; border: 1px solid var(--da-line); border-radius: 8px; background: var(--da-bg); color: var(--da-text); font: inherit; }
.btn { background: var(--da-accent); color: var(--text-primary-color, #fff); border: 0; border-radius: 8px; padding: 9px 20px; font: inherit; font-weight: 500; cursor: pointer; }
.presets { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip-btn { background: var(--da-bg); border: 1px solid var(--da-line); color: var(--da-text); border-radius: 999px; padding: 5px 12px; font: inherit; font-size: 13px; cursor: pointer; }
@media (max-width: 600px) {
  .content { padding: 8px; }
  .row-tags { display: none; }
  .row-body { padding-left: 12px; }
  .kv { grid-template-columns: 1fr; gap: 2px; }
  .dur { font-size: 32px; }
  .tbl th:nth-child(n+4), .tbl td:nth-child(n+4):not(:last-child) { display: none; }
}
`;

customElements.define("downtime-auditor-panel", DowntimeAuditorPanel);
