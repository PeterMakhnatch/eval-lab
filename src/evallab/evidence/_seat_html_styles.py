from __future__ import annotations

HTML_STYLES = """\
:root {
  --bg: #0d1117; --panel: #161b22; --panel-2: #1f2733; --border: #2d3848;
  --text: #e6edf3; --muted: #8b9bb0; --accent: #4ea1ff; --ok: #3fb950;
  --warn: #d29922; --err: #f85149; --info: #58a6ff; --purple: #bc8cff;
  --chip-bg: #222e3d;
}
[data-theme="light"] {
  --bg: #f6f8fa; --panel: #ffffff; --panel-2: #f0f3f7; --border: #d0d7de;
  --text: #1f2328; --muted: #59636e; --accent: #0969da; --ok: #1a7f37;
  --warn: #9a6700; --err: #cf222e; --info: #0969da; --purple: #8250df;
  --chip-bg: #eaeef2;
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
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
select, button, input {
  background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 10px; font-size: 13px;
}
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
main { max-width: 1480px; margin: 0 auto; padding: 18px 24px 56px; }
.grid-kpi { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 14px; margin-bottom: 16px; }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px;
}
.kpi-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
.kpi-val { font-size: 22px; font-weight: 600; margin-top: 4px; }
.kpi-sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
section { margin-bottom: 20px; }
section h2 { font-size: 15px; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }
section h3 { font-size: 13px; margin: 12px 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.badge {
  display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 11px;
  font-weight: 600; background: var(--chip-bg); color: var(--text);
}
.badge.ok { color: var(--ok); border: 1px solid var(--ok); }
.badge.warn { color: var(--warn); border: 1px solid var(--warn); }
.badge.error { color: var(--err); border: 1px solid var(--err); }
.badge.info { color: var(--info); border: 1px solid var(--info); }
.badge.purple { color: var(--purple); border: 1px solid var(--purple); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 600; background: var(--panel-2); position: sticky; top: 0; }
tr:hover td { background: var(--panel-2); }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }
.pill-bar { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.pill {
  padding: 4px 9px; border-radius: 999px; font-size: 12px; border: 1px solid var(--border);
  background: var(--panel-2); color: var(--text); cursor: pointer;
}
.pill.active { border-color: var(--accent); background: var(--accent); color: #ffffff; }
.filter-bar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 12px;
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 14px;
}
.filter-bar label { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 5px; }
.swimlane-box { overflow-x: auto; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; padding: 12px; }
.lane-row { display: grid; grid-template-columns: 180px 1fr; align-items: center; min-height: 44px; border-bottom: 1px dashed var(--border); }
.lane-row:last-child { border-bottom: none; }
.lane-label { font-weight: 600; padding-right: 10px; word-break: break-word; }
.lane-track { position: relative; height: 38px; min-width: 720px; }
.ev-node {
  position: absolute; top: 7px; transform: translateX(-50%);
  padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600;
  white-space: nowrap; cursor: pointer; border: 1px solid var(--border); background: var(--panel);
}
.ev-node.post { border-color: var(--info); color: var(--info); }
.ev-node.fetch { border-color: var(--purple); color: var(--purple); }
.ev-node.action { border-color: var(--ok); color: var(--ok); }
.ev-node.warn { border-color: var(--warn); color: var(--warn); }
.ev-node.err { border-color: var(--err); color: var(--err); }
.eop-line { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--err); opacity: 0.7; }
.detail-box { margin-top: 10px; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; }
.trace-list { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.trace-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 6px 8px; border-radius: 6px; background: var(--panel-2); border: 1px solid var(--border); }
.arrow { color: var(--muted); font-weight: 700; }
.muted { color: var(--muted); }
.svg-chart { width: 100%; height: auto; display: block; }
.pos { color: var(--ok); font-weight: 600; }
.neg { color: var(--err); font-weight: 600; }
.hidden { display: none !important; }
"""

_HTML_MAIN_BODY = """\
<main>
  <section>
    <h2>Executive Corpus &amp; Ecosystem Overview</h2>
    <div id="overview-kpis" class="grid-kpi"></div>
    <div id="corpora-panel" class="card" style="margin-bottom:14px"></div>
    <div class="grid-2">
      <div class="card">
        <h3>Message Delivery Funnel</h3>
        <div id="funnel-chart"></div>
      </div>
      <div class="card">
        <h3>Failure-Mode Breakdown (Click Mode to Filter Trials)</h3>
        <div id="modes-chart"></div>
        <div id="overview-modes" style="margin-top:8px"></div>
      </div>
    </div>
  </section>
  <section id="cda-section">
    <h2>Causal Message Attribution (CDA) &amp; Server Guard Impact</h2>
    <div id="cda-kpis" class="grid-kpi"></div>
    <div id="cda-corpora-panel" class="card" style="margin-bottom:14px"></div>
    <div class="grid-2">
      <div class="card">
        <h3>Sender Causal Credit Share</h3>
        <div id="cda-credit-chart"></div>
      </div>
      <div class="card">
        <h3>Causal Influence &amp; Guard Enforcement Counts</h3>
        <div id="cda-guard-chart"></div>
      </div>
    </div>
  </section>
  <section id="decomp-section" class="hidden">
    <h2>Coordination Decomposition (S_1 vs S_N^0 vs S_N) &amp; PACE Efficiency</h2>
    <div id="decomp-panels"></div>
  </section>
  <section id="bespoke-section" class="hidden">
    <h2>Bespoke MAC Bench Public Rollout Decomposition &amp; Checkpoint Curves</h2>
    <div id="bespoke-panels"></div>
  </section>
  <section>
    <h2>Interactive Trial Forensics Inspector</h2>
    <div class="filter-bar">
      <label>Corpus <select id="filter-corpus"></select></label>
      <label>Ecosystem <select id="filter-eco"></select></label>
      <label>Task <select id="filter-task"></select></label>
      <label>Failure Mode <select id="filter-mode"></select></label>
      <label>Integrity <select id="filter-integrity"></select></label>
      <label>Trial <select id="trial-select"></select></label>
      <span id="trial-match-count" class="muted mono"></span>
    </div>
    <div id="trial-section">
      <section class="card">
        <h2>Cross-Seat Swimlane Timeline (posted -&gt; fetched -&gt; in_context -&gt; acted_on)</h2>
        <div id="swimlane-container" class="swimlane-box"></div>
        <div id="event-detail" class="detail-box"></div>
        <h2 style="margin-top:14px">Message Causal Funnel Traces</h2>
        <div id="message-traces" class="trace-list"></div>
      </section>
      <section class="card" id="trial-cda-card">
        <h2>Trial Causal Message Attribution &amp; Server Guard Impact</h2>
        <div id="trial-cda-summary" style="margin-bottom:10px"></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Msg #</th><th>Sender</th><th>Fetched / In-Ctx</th><th>Influence Score</th><th>Avoided Dup Targets</th><th>Triggered Actions</th><th>Contradictions / Dups</th><th>Outcome</th></tr></thead>
            <tbody id="trial-cda-messages"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <h2>Failure-Mode Episodes (Deterministic Provenance)</h2>
        <div class="controls" style="margin-bottom:10px">
          <div id="episode-pills" class="pill-bar" style="margin:0"></div>
          <select id="actor-filter"></select>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Kind</th><th>Actor</th><th>Step</th><th>Event Seq</th><th>Timestamp</th><th>Target</th><th>Summary</th></tr></thead>
            <tbody id="episode-rows"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <h2>Per-Seat Completeness, Cost, and Funnel</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Member</th><th>Harness / Model</th><th>Trajectory</th><th>Steps / Linked</th><th>Cost (Status)</th><th>Post / Fetch / Ctx / Act</th><th>Unread / Never</th><th>Status</th></tr></thead>
            <tbody id="seat-rows"></tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <h2>Reward and Integrity Flags</h2>
        <div id="integrity-panel"></div>
      </section>
    </div>
  </section>
</main>"""


def build_html_shell(title: str, payload_json: str, script_body: str) -> str:
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
    safe_json = (
        payload_json.replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{safe_title}</title>
<style>
{HTML_STYLES}
</style>
</head>
<body>
<header>
  <div>
    <h1 id="report-title">{safe_title}</h1>
    <div id="report-subtitle" class="subtitle mono"></div>
  </div>
  <div class="controls">
    <button id="theme-toggle" type="button">Light Theme</button>
  </div>
</header>
{_HTML_MAIN_BODY}
<script id="forensics-data" type="application/json">
{safe_json}
</script>
<script>
{script_body}
</script>
</body>
</html>
"""
