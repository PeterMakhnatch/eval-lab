from __future__ import annotations

import base64
import hashlib
import re

from ._multiagent_io import sanitize_utf8_text

_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)

_CSS = """\
:root {
  --bg: #0d1117; --panel: #161b22; --panel-2: #1f2733; --border: #2d3848;
  --text: #e6edf3; --muted: #8b9bb0; --accent: #4ea1ff; --ok: #3fb950;
  --warn: #d29922; --err: #f85149; --info: #58a6ff; --purple: #bc8cff;
}
[data-theme="light"] {
  --bg: #f6f8fa; --panel: #ffffff; --panel-2: #f0f3f7; --border: #d0d7de;
  --text: #1f2328; --muted: #59636e; --accent: #0969da; --ok: #1a7f37;
  --warn: #9a6700; --err: #cf222e; --info: #0969da; --purple: #8250df;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.45; font-size: 14px;
}
header {
  display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between;
  padding: 14px 24px; background: var(--panel); border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 20;
}
header h1 { margin: 0; font-size: 18px; font-weight: 600; }
.subtitle { color: var(--muted); font-size: 12px; margin-top: 2px; }
main { max-width: 1480px; margin: 0 auto; padding: 18px 24px 48px; }
.grid-kpi { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 16px; }
.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 14px; margin-bottom: 16px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; margin-bottom: 14px; }
.kpi-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
.kpi-val { font-size: 20px; font-weight: 600; margin-top: 4px; }
.kpi-sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
h2 { font-size: 15px; margin: 0 0 10px; }
h3 { font-size: 13px; margin: 0 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
select, button, input {
  background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 10px; font-size: 13px;
}
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
.filter-bar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 12px;
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 12px;
}
.filter-bar label { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.table-wrap { overflow-x: auto; max-height: 460px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 600; background: var(--panel-2); position: sticky; top: 0; }
tr.clickable { cursor: pointer; }
tr.clickable:hover td, tr.selected td { background: var(--panel-2); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.badge {
  display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 11px;
  font-weight: 600; border: 1px solid var(--border); background: var(--panel-2);
}
.badge.ok { color: var(--ok); border-color: var(--ok); }
.badge.warn { color: var(--warn); border-color: var(--warn); }
.badge.error { color: var(--err); border-color: var(--err); }
.badge.info { color: var(--info); border-color: var(--info); }
.muted { color: var(--muted); }
.detail-box {
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px;
  padding: 12px; white-space: pre-wrap; word-break: break-word;
}
.hidden { display: none !important; }
"""

_JS = """\
(function () {
  "use strict";
  const rawEl = document.getElementById("bridge-data");
  const DATA = JSON.parse(rawEl ? rawEl.textContent : "{}");
  let filteredTrials = DATA.trials || [];
  let selectedKey = "";

  function esc(val) {
    return String(val == null ? "" : val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtNum(v, digits) {
    if (typeof v !== "number" || !Number.isFinite(v)) return "n/a";
    return v.toFixed(digits == null ? 3 : digits);
  }

  function badge(text, cls) {
    return '<span class="badge ' + esc(cls || "") + '">' + esc(text) + "</span>";
  }

  function initTheme() {
    const btn = document.getElementById("theme-toggle");
    const root = document.documentElement;
    if (!btn) return;
    btn.addEventListener("click", function () {
      const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      btn.textContent = next === "light" ? "Dark Theme" : "Light Theme";
    });
  }

  function renderHeader() {
    const titleEl = document.getElementById("bridge-title");
    const subEl = document.getElementById("bridge-subtitle");
    if (titleEl) titleEl.textContent = DATA.title || "Unified Multi-Agent Forensics Bridge";
    if (subEl) {
      const eco = Object.keys(DATA.ecosystems || {}).map(function (k) {
        return k + ":" + DATA.ecosystems[k];
      }).join(", ");
      subEl.textContent = "Schema: " + (DATA.schema_version || "") +
        " | Trials: " + (DATA.n_trials || 0) +
        " | Ecosystems: " + (eco || "none") +
        " | Target: " + (DATA.target_path || "");
    }
  }

  function renderKpis() {
    const f = DATA.forensics || {};
    const c = DATA.cda || {};
    const v = DATA.validity || {};
    const t = DATA.tidegauge_audit || {};
    const m = DATA.muster_regrade || {};
    const dSum = (DATA.detectors || {}).summary || {};
    const firedRules = Object.keys(dSum).filter(function (k) { return (dSum[k].trials_flagged || 0) > 0; }).length;
    const totalHits = Object.keys(dSum).reduce(function (acc, k) { return acc + (dSum[k].total_hits || 0); }, 0);
    const tCnt = t.counts || {};
    const cards = [
      ["Trials Joined", String(DATA.n_trials || 0), "logs " + (f.logs_complete || 0) + " / cost " + (f.cost_complete || 0)],
      ["WorldWatch Validity", String(v.measured || 0) + " / " + String(v.total_trials || 0), "unmeasured " + (v.unmeasured || 0) + " (" + fmtNum((v.unmeasured_rate || 0) * 100, 1) + "%)"],
      ["Forensics Funnel", String(f.messages_posted || 0) + " posted", "ctx " + (f.messages_in_context || 0) + " / act " + (f.messages_acted_on || 0) + " / unread " + (f.unread_at_action || 0)],
      ["CDA Attribution", String(c.avoided_duplicate_targets || 0) + " avoided", "triggered " + (c.triggered_actions || 0) + " / locks " + (c.claim_locks_granted || 0) + " / saved " + (c.claim_rejections_saved_dups || 0)],
      ["F1-F22 Detectors", String(firedRules) + " active rule(s)", String(totalHits) + " total detector hit(s)"],
      ["Tidegauge Audit", t.available ? String(t.worst || "clean") : "unavailable", "err " + (tCnt.error || 0) + " / warn " + (tCnt.warn || 0) + " / note " + (tCnt.note || 0)],
      ["Muster Regrade", m.available ? String(m.trials || 0) + " trial(s)" : "unavailable", m.available ? ("chain " + (m.chain_checked || 0) + " / fail " + (m.chain_failures || 0) + " / late " + (m.trials_with_late_actions || 0)) : String(m.reason || "not applicable")],
    ];
    document.getElementById("bridge-kpis").innerHTML = cards.map(function (it) {
      return '<div class="card" style="margin:0"><div class="kpi-label">' + esc(it[0]) +
        '</div><div class="kpi-val">' + esc(it[1]) +
        '</div><div class="kpi-sub mono">' + esc(it[2]) + "</div></div>";
    }).join("");
  }

  function renderDetectors() {
    const det = DATA.detectors || {};
    const names = det.detector_names || [];
    const summary = det.summary || {};
    const tbody = document.getElementById("detector-rows");
    tbody.innerHTML = names.map(function (name) {
      const s = summary[name] || {};
      const flagged = s.trials_flagged || 0;
      return "<tr><td class=\"mono\">" + esc(name) + "</td>" +
        '<td class="mono">' + badge(String(flagged), flagged > 0 ? "warn" : "ok") + "</td>" +
        '<td class="mono">' + esc(s.total_hits || 0) + "</td>" +
        '<td class="mono">' + fmtNum((s.rate || 0) * 100, 1) + "%</td></tr>";
    }).join("");
  }

  function renderAuditAndDecomp() {
    const t = DATA.tidegauge_audit || {};
    const findings = t.findings || [];
    const tbody = document.getElementById("audit-rows");
    if (!findings.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">' + (t.available ? "No Tidegauge findings." : esc(t.error || "Tidegauge unavailable")) + "</td></tr>";
    } else {
      tbody.innerHTML = findings.map(function (f) {
        const cls = f.severity === "error" ? "error" : (f.severity === "warn" ? "warn" : "info");
        return "<tr><td>" + badge(f.severity || "", cls) + "</td>" +
          '<td class="mono">' + esc(f.code || "") + "</td>" +
          "<td>" + esc(f.arm || "") + "</td>" +
          "<td>" + esc(f.message || "") + "</td></tr>";
      }).join("");
    }
    const decomps = DATA.decompositions || [];
    const dSec = document.getElementById("decomp-card");
    const dRows = document.getElementById("decomp-rows");
    if (!decomps.length) {
      dSec.classList.add("hidden");
      return;
    }
    dSec.classList.remove("hidden");
    dRows.innerHTML = decomps.map(function (d) {
      const eff = (d.seat_decompose || {}).effects || {};
      const tgEff = (d.tidegauge_decompose || {}).effects || {};
      const hc = (eff.headcount || {}).difference;
      const co = (eff.coordination || {}).difference;
      const tot = (eff.total || {}).difference;
      const tTot = (tgEff.total || {}).difference;
      return "<tr><td><strong>" + esc(d.label || "") + "</strong></td>" +
        '<td class="mono">' + fmtNum(hc, 4) + "</td>" +
        '<td class="mono">' + fmtNum(co, 4) + "</td>" +
        '<td class="mono">' + fmtNum(tot, 4) + "</td>" +
        '<td class="mono">' + fmtNum(tTot, 4) + "</td></tr>";
    }).join("");
  }

  function populateFilters() {
    const trials = DATA.trials || [];
    const ecos = Array.from(new Set(trials.map(function (t) { return t.ecosystem; }))).sort();
    const dets = ((DATA.detectors || {}).detector_names || []).slice();
    const ecoSel = document.getElementById("f-eco");
    ecoSel.innerHTML = '<option value="ALL">ALL</option>' + ecos.map(function (e) {
      return '<option value="' + esc(e) + '">' + esc(e) + "</option>";
    }).join("");
    const detSel = document.getElementById("f-det");
    detSel.innerHTML = '<option value="ALL">ALL</option>' + dets.map(function (d) {
      return '<option value="' + esc(d) + '">' + esc(d) + "</option>";
    }).join("");
    ["f-eco", "f-val", "f-det"].forEach(function (id) {
      document.getElementById(id).addEventListener("change", applyFilters);
    });
    document.getElementById("f-search").addEventListener("input", applyFilters);
  }

  function applyFilters() {
    const eco = document.getElementById("f-eco").value;
    const val = document.getElementById("f-val").value;
    const det = document.getElementById("f-det").value;
    const q = (document.getElementById("f-search").value || "").trim().toLowerCase();
    filteredTrials = (DATA.trials || []).filter(function (t) {
      if (eco !== "ALL" && t.ecosystem !== eco) return false;
      if (val === "measured" && t.validity_measured !== true) return false;
      if (val === "unmeasured" && t.validity_measured !== false) return false;
      if (det !== "ALL" && (t.detectors_triggered || []).indexOf(det) < 0) return false;
      if (q) {
        const hay = [t.trial_name, t.task_name, t.validity_reason, (t.detectors_triggered || []).join(" ")].join(" ").toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
    document.getElementById("f-count").textContent = filteredTrials.length + " / " + (DATA.trials || []).length + " trial(s)";
    renderTrialsTable();
  }

  function renderTrialsTable() {
    const tbody = document.getElementById("trial-rows");
    if (!filteredTrials.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="muted">No matching trials.</td></tr>';
      document.getElementById("trial-detail").textContent = "Select a trial to inspect joined details.";
      return;
    }
    if (!filteredTrials.some(function (t) { return t.trial_key === selectedKey; })) {
      selectedKey = filteredTrials[0].trial_key;
    }
    tbody.innerHTML = filteredTrials.map(function (t) {
      const valCls = t.validity_measured ? "ok" : "error";
      const detStr = (t.detectors_triggered || []).map(function (d) { return badge(d, "warn"); }).join(" ") || '<span class="muted">none</span>';
      const fm = Object.keys(t.failure_mode_counts || {}).map(function (k) { return k + ":" + t.failure_mode_counts[k]; }).join(", ") || "none";
      const reg = t.muster_regrade ? (t.muster_regrade.status + (t.muster_regrade.chain_ok === false ? " (chain!)" : "")) : "n/a";
      const sel = t.trial_key === selectedKey ? "selected" : "";
      return '<tr class="clickable ' + sel + '" data-key="' + esc(t.trial_key) + '">' +
        '<td class="mono"><strong>' + esc(t.trial_name) + "</strong></td>" +
        '<td class="mono">' + esc(t.task_name || "") + "</td>" +
        '<td class="mono">' + esc(t.ecosystem) + "</td>" +
        '<td class="mono">' + fmtNum(t.reward, 3) + "</td>" +
        "<td>" + badge(t.validity_reason || "unknown", valCls) + "</td>" +
        '<td class="mono">' + esc(fm) + "</td>" +
        "<td>" + detStr + "</td>" +
        '<td class="mono">' + esc(reg) + "</td></tr>";
    }).join("");
    tbody.querySelectorAll("tr.clickable").forEach(function (row) {
      row.addEventListener("click", function () {
        selectedKey = row.getAttribute("data-key") || "";
        renderTrialsTable();
        renderDetail();
      });
    });
    renderDetail();
  }

  function renderDetail() {
    const box = document.getElementById("trial-detail");
    const found = filteredTrials.find(function (t) { return t.trial_key === selectedKey; });
    if (!found) {
      box.textContent = "Select a trial to inspect joined details.";
      return;
    }
    box.textContent = JSON.stringify(found, null, 2);
  }

  initTheme();
  renderHeader();
  renderKpis();
  renderDetectors();
  renderAuditAndDecomp();
  populateFilters();
  applyFilters();
})();
""".strip()

_JS_SHA256 = base64.b64encode(hashlib.sha256(_JS.encode("utf-8")).digest()).decode("ascii")
_CSP = (
    "default-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "style-src 'unsafe-inline'; "
    f"script-src 'sha256-{_JS_SHA256}'; "
    "img-src data:;"
)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _escape_json_for_script(payload_json: str) -> str:
    return (
        payload_json.replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _html_main_sections() -> str:
    return """<main>
  <div id="bridge-kpis" class="grid-kpi"></div>
  <div class="grid-2">
    <section class="card">
      <h2>WorldWatch F1-F22 Detectors</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Detector</th><th>Trials Flagged</th><th>Total Hits</th><th>Rate</th></tr></thead>
          <tbody id="detector-rows"></tbody>
        </table>
      </div>
    </section>
    <section class="card">
      <h2>Tidegauge Audit Findings</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Severity</th><th>Code</th><th>Arm</th><th>Message</th></tr></thead>
          <tbody id="audit-rows"></tbody>
        </table>
      </div>
    </section>
  </div>
  <section id="decomp-card" class="card hidden">
    <h2>Team Decomposition (Seat Decompose vs Tidegauge)</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Label</th><th>Headcount</th><th>Coordination</th><th>Seat Total</th><th>Tidegauge Total</th></tr></thead>
        <tbody id="decomp-rows"></tbody>
      </table>
    </div>
  </section>
  <section class="card">
    <h2>Joined Trials (Forensics + CDA + WorldWatch + Tidegauge + Muster)</h2>
    <div class="filter-bar">
      <label>Ecosystem <select id="f-eco"></select></label>
      <label>Validity <select id="f-val"><option value="ALL">ALL</option><option value="measured">measured</option><option value="unmeasured">unmeasured</option></select></label>
      <label>Detector <select id="f-det"></select></label>
      <label>Search <input id="f-search" type="search" placeholder="trial, task, reason..."></label>
      <span id="f-count" class="muted mono"></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Trial</th><th>Task</th><th>Ecosystem</th><th>Reward</th><th>Validity</th><th>Failure Modes</th><th>WorldWatch Detectors</th><th>Muster Regrade</th></tr></thead>
        <tbody id="trial-rows"></tbody>
      </table>
    </div>
    <h3 style="margin-top:12px">Selected Trial Joined Record</h3>
    <pre id="trial-detail" class="detail-box mono"></pre>
  </section>
</main>"""


def build_bridge_html_document(title: str, payload_json: str) -> str:
    safe_title = _escape_html(sanitize_utf8_text(title))
    safe_json = _escape_json_for_script(sanitize_utf8_text(payload_json))
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en" data-theme="dark">\n<head>\n'
        '<meta charset="utf-8" />\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"<title>{safe_title}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        "<header>\n"
        f'  <div><h1 id="bridge-title">{safe_title}</h1>'
        '<div id="bridge-subtitle" class="subtitle mono"></div></div>\n'
        '  <button id="theme-toggle" type="button">Light Theme</button>\n'
        "</header>\n"
        f"{_html_main_sections()}\n"
        f'<script id="bridge-data" type="application/json">{safe_json}</script>\n'
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )


def extract_bridge_inline_js(html: str) -> str:
    matches = _SCRIPT_RE.findall(html)
    if not matches:
        raise ValueError("Bridge HTML report contains no inline <script> block")
    return matches[-1]
