"""Self-contained, read-only-by-default browser surface for the production journal."""

DASHBOARD: str = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Eval Lab · Live Production</title>
<style>
:root{color-scheme:dark;--bg:#0b1018;--panel:#111a26;--raised:#172332;--line:#29394e;--text:#edf3fa;--muted:#a3b2c6;--accent:#6ce4b8;--warn:#f1c16d;--red:#ff929c;--blue:#94baff;--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 20% 0%,#162434 0,transparent 44%),var(--bg);color:var(--text);font:13px/1.55 system-ui,sans-serif}button,input,select,textarea{font:inherit}button,input,select,textarea,summary{outline-offset:4px}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid var(--accent)}button{border:1px solid var(--line);background:var(--raised);color:var(--text);border-radius:7px;padding:7px 11px;cursor:pointer;font-weight:600}button:hover:not(:disabled){border-color:var(--accent);background:#1c3040}button:disabled{opacity:.45;cursor:not-allowed}button.primary{background:#164536;border-color:#3b8e71;color:#b6ffe0}button.danger{color:var(--red)}button.link{border:0;background:none;padding:0;text-align:left;color:var(--blue);overflow-wrap:anywhere}button.small{font-size:11px;padding:4px 8px}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:7px;background:#0d1520;color:var(--text);padding:9px 10px}textarea{min-height:68px;resize:vertical}input[type=checkbox]{width:auto;accent-color:var(--accent)}input[type=range]{padding:0;accent-color:var(--accent)}label{display:block;color:var(--muted);font-size:11px;font-weight:650;margin-bottom:5px}main{max-width:1640px;margin:auto;padding:24px 28px}header,.row,.panel-head,.actions{display:flex;align-items:center;gap:12px}.row,.panel-head{justify-content:space-between}.actions{flex-wrap:wrap;gap:6px}.topbar{justify-content:space-between;align-items:flex-start;padding-bottom:20px;border-bottom:1px solid var(--line)}h1,h2,h3,h4{line-height:1.3;margin:0}h1{font-size:25px;letter-spacing:-.6px}h2{font-size:18px}h3{font-size:14px}h4{font-size:12px}p{margin:7px 0;color:var(--muted)}small{font-size:11px;color:var(--muted)}.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.12em;font-size:10px;font-weight:750;margin-bottom:5px}.top-meta{display:flex;align-items:center;gap:24px}.token-field{width:265px}.token-field input{padding:7px 9px}.connection-box{text-align:right;max-width:260px}.status{color:var(--accent)}.disconnected,.warning{color:var(--warn)}.muted{color:var(--muted)}.mono{font-family:var(--mono);font-size:11px;overflow-wrap:anywhere}.panel,.system{border:1px solid var(--line);border-radius:11px;padding:18px;background:var(--panel)}section{margin-top:18px}.panel-head{margin-bottom:14px;align-items:flex-start}.chips{display:flex;gap:6px;flex-wrap:wrap}.chip,.badge{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:5px;padding:3px 7px;font-size:10px;color:var(--muted);white-space:normal;overflow-wrap:anywhere}.badge.good{border-color:#2f6c57;color:var(--accent);background:#123127}.badge.warn{border-color:#765c33;color:var(--warn);background:#2c251b}.badge.bad{border-color:#79434e;color:var(--red);background:#301d27}.badge.blue{border-color:#3b567d;color:var(--blue);background:#1a273c}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;padding:16px 0 0}.metric{display:flex;flex-direction:column;gap:2px}.metric strong{font-family:var(--mono);font-size:27px;font-weight:550}.metric span{font-size:11px;color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px}.workspace{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(360px,1fr);gap:18px;align-items:start}.workspace section{min-width:0}.systems-section .panel-head{margin-bottom:10px}.system{padding:14px;min-width:0}.system .cards{margin-top:10px;max-height:160px;overflow:auto}.system-traces{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}.cards{display:grid;gap:8px}.card{border:1px solid var(--line);border-left:2px solid #56759a;border-radius:7px;background:#0e1723;padding:11px;min-width:0;overflow-wrap:anywhere}.card strong,.card small{display:block}.card.confirmed,.card.applied{border-left-color:var(--accent)}.card.provisional,.card.requested{border-left-color:var(--warn)}.card.refuted,.card.expired{border-left-color:var(--muted)}.card-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:7px}.empty{padding:15px 0;color:var(--muted);font-size:12px}.run-toolbar{display:flex;align-items:center;gap:12px;margin-bottom:12px}.run-toolbar input{max-width:280px}.run-list{display:grid;gap:7px;max-height:500px;overflow:auto;padding:2px}.run-row{border:1px solid var(--line);border-radius:8px;background:#0e1723;padding:12px}.run-main{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.run-main .link{font-family:var(--mono);font-size:12px;font-weight:650}.run-meta{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-top:6px}.run-bottom{display:flex;justify-content:space-between;gap:8px;align-items:center;margin-top:9px}.run-controls{display:flex;gap:5px;flex-shrink:0}.progress{display:flex;gap:5px;align-items:center;flex-wrap:wrap;font-size:10px;color:var(--muted)}.progress .current{color:var(--accent);font-weight:700}.progress .done{color:var(--accent)}.progress .pending{opacity:.65}.control-summary{font-size:10px;color:var(--muted);margin-top:7px}.quick-control{border:1px solid #476b84;border-radius:8px;padding:12px;margin:12px 0;background:#122333}.quick-control[hidden]{display:none}.quick-control label{margin-top:8px}.quick-control .actions{margin-top:8px}.notice{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.notice.error{color:var(--red)}.notice.success{color:var(--accent)}.intervention-fields{display:grid;gap:12px}.intervention-fields .two{gap:10px}.controls-list{margin-top:15px;max-height:300px;overflow:auto}.assets-panel{margin-top:18px}.asset-heading{display:flex;align-items:center;gap:18px}.asset-heading .metric strong{font-size:24px}.asset-filters{margin-bottom:12px}.asset-filters button[aria-pressed=true]{color:var(--accent);border-color:#3b8e71;background:#123127}.asset-list{display:grid;gap:9px;max-height:650px;overflow:auto;padding:2px}.asset-row{border:1px solid var(--line);border-radius:8px;background:#0e1723;padding:12px}.asset-row .asset-id{font-size:12px;font-weight:650}.asset-row p{font-size:11px}.asset-row .actions{margin-top:10px}.asset-result{margin-top:8px;font-family:var(--mono);font-size:10px;white-space:pre-wrap;overflow-wrap:anywhere}.asset-result:empty{display:none}footer{font-size:11px;color:var(--muted);margin:24px 0 4px;max-width:1000px}.seal{color:var(--warn)}dialog{width:min(1350px,94vw);height:min(900px,92vh);max-width:none;max-height:94vh;margin:auto;padding:0;border:1px solid #40536c;border-radius:12px;background:var(--bg);color:var(--text);box-shadow:0 24px 100px #000b}dialog::backdrop{background:#030812c9;backdrop-filter:blur(3px)}.dialog-layout{display:flex;flex-direction:column;height:100%}.dialog-header{padding:18px 22px;border-bottom:1px solid var(--line);background:var(--panel);flex-shrink:0;display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.dialog-header h2{overflow-wrap:anywhere;font-family:var(--mono);font-size:16px}.dialog-header .actions{justify-content:flex-end;flex-shrink:0}.dialog-body{padding:18px 22px;overflow:auto;min-height:0;flex:1}.dialog-status{padding:12px 22px;margin:0;border-bottom:1px solid var(--line)}.dialog-status:empty{display:none}.scrubber{padding:13px 22px;background:var(--panel);border-bottom:1px solid var(--line);flex-shrink:0}.scrubber-top{display:flex;align-items:center;justify-content:space-between;gap:12px}.scrubber-top label{display:flex;gap:7px;align-items:center;margin:0;color:var(--text)}.scrubber input[type=range]{margin:12px 0 4px}.timeline-grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(285px,1fr);gap:18px;align-items:start}.timeline-grid h3{margin-bottom:10px}.trace-steps{display:grid;gap:10px}.step{border:1px solid var(--line);border-radius:8px;background:var(--panel);scroll-margin-top:15px}.step-header{padding:10px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}.step-content{padding:10px 12px}.step-content p{white-space:pre-wrap;font-size:12px}.step.focus-step{outline:2px solid var(--accent)}.tool-call{border:1px solid var(--line);border-radius:6px;margin-top:9px;overflow:hidden}.tool-call summary{padding:7px 9px;cursor:pointer;color:var(--blue);font-family:var(--mono);font-size:11px;background:#152132}.tool-call pre{margin:0;padding:9px}.trace-rail{display:grid;gap:16px}.trace-rail .cards{gap:7px}.prefix-score{font-family:var(--mono);font-size:12px;color:var(--blue)}.citation{border-radius:4px;padding:3px 6px;font-family:var(--mono);font-size:10px}.citation-list{display:flex;gap:4px;flex-wrap:wrap;margin-top:8px}pre{font:11px/1.6 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;tab-size:4;margin:8px 0;color:#c6d5e7}details summary{cursor:pointer}.reward-header{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:16px;align-items:center;margin-bottom:16px}.reward-box{background:var(--panel);border:1px solid var(--line);padding:15px;border-radius:8px;min-width:0}.reward-value{font:32px/1.2 var(--mono);margin:5px 0;color:var(--red)}.reward-box.repaired .reward-value{color:var(--accent)}.reward-arrow{font-size:23px;color:var(--muted)}.reward-box p{font:10px/1.6 var(--mono);overflow-wrap:anywhere}.control-rewards{display:grid;grid-template-columns:100px repeat(3,minmax(0,1fr));gap:5px;margin-bottom:18px;max-width:600px}.control-rewards .reward-cell{text-align:center;border:1px solid var(--line);border-radius:4px;padding:6px;font:12px var(--mono);background:var(--panel)}.control-rewards .reward-label{padding:6px;font-size:11px;color:var(--muted)}.diff-layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:16px;align-items:start}.diff-files{display:grid;gap:5px;position:sticky;top:0}.diff-file{display:flex;justify-content:space-between;gap:8px;text-align:left;align-items:flex-start;font:11px/1.5 var(--mono);overflow-wrap:anywhere}.diff-file[aria-selected=true]{border-color:var(--accent);background:#143129}.diff-view{min-width:0;border:1px solid var(--line);border-radius:7px;overflow:hidden;background:#0e1723}.diff-title{padding:10px 12px;border-bottom:1px solid var(--line);font-family:var(--mono);overflow-wrap:anywhere}.diff-scroll{overflow:auto;max-height:480px}.diff-table{width:100%;border-collapse:collapse;table-layout:fixed;font:10px/1.6 var(--mono)}.diff-table th{font:11px system-ui,sans-serif;color:var(--muted);padding:8px;background:var(--panel);text-align:left;position:sticky;top:0}.diff-table .line-number{width:38px;text-align:right;vertical-align:top;user-select:none;color:#6d829d;padding:1px 7px;border-right:1px solid #26344955}.diff-table .code-line{white-space:pre-wrap;overflow-wrap:anywhere;vertical-align:top;padding:1px 8px;tab-size:4}.diff-table .right{border-left:1px solid var(--line)}.diff-table .removed{background:#53233169;color:#ffc0c7}.diff-table .added{background:#154a3869;color:#adf3d1}.diff-table .hunk{color:var(--blue);background:#1b2a3f;padding:5px 8px}.diff-table .absent{background:#131c28;color:#526478}.diff-note{padding:14px;color:var(--muted)}.exploit-panel{border-top:1px solid var(--line);margin-top:18px;padding-top:15px}.exploit-panel details{margin-top:9px;border:1px solid var(--line);padding:10px;border-radius:6px}.preview-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(230px,.55fr);gap:18px}.preview-layout pre{background:var(--panel);padding:14px;border:1px solid var(--line);border-radius:7px;margin:0}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}[hidden]{display:none!important}
.diff-layout{margin-top:12px}.diff-file:focus-visible{outline-offset:1px}
.run-toolbar,.run-bottom,.run-controls{flex-wrap:wrap}.run-campaign{margin:5px 0}.run-facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;margin:10px 0}.run-fact{border-top:1px solid var(--line);padding-top:6px;min-width:0}.run-fact dt{font-size:10px;color:var(--muted)}.run-fact dd{margin:2px 0 0;font:13px var(--mono);overflow-wrap:anywhere}.run-outcome{font-size:11px;overflow-wrap:anywhere}.campaign-controls{display:flex;align-items:center;flex-wrap:wrap;gap:6px}.campaign-controls>small{color:var(--warn)}.execution-summary{border:1px solid var(--line);border-radius:8px;padding:12px;margin:14px 0;background:var(--panel)}.execution-summary>small{display:block;margin-top:4px}.dialog-header>div:first-child{min-width:0}
.run-main>.badge,.diff-file>.badge{flex-shrink:0;white-space:nowrap}
.view-tabs{display:flex;flex-wrap:wrap;gap:7px;margin:18px 0 0}.view-tabs [aria-selected=true]{border-color:var(--accent);background:#143129;color:var(--accent)}.cloud-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px}.cloud-query{grid-column:1/-1}.cloud-filters .actions{grid-column:1/-1}.cloud-filters .check-label{display:flex;align-items:center;gap:8px;margin:0}.cloud-grid{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(260px,1fr);gap:18px;margin-top:18px}.cloud-grid h3{margin-bottom:10px}.cloud-results{max-height:680px;overflow:auto}.cloud-table-wrap{overflow:auto;border:1px solid var(--line);border-radius:7px;margin:12px 0}.cloud-table{border-collapse:collapse;width:100%;font-size:11px}.cloud-table th,.cloud-table td{padding:10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}.cloud-table th{color:var(--muted);background:var(--raised);white-space:nowrap}.cloud-table td{min-width:85px;overflow-wrap:anywhere}.cloud-table td small{display:block}.cloud-report{border-top:1px solid var(--line);padding-top:16px;margin-top:18px}.cloud-report h3{overflow-wrap:anywhere}.cloud-report summary{color:var(--blue)}.cloud-provenance{margin:8px 0}.cloud-issue-list{margin-top:8px;padding-left:18px;color:var(--muted)}@media(max-width:900px){.cloud-grid{grid-template-columns:1fr}}
@media(max-width:1200px){main{padding:20px}.workspace{grid-template-columns:minmax(0,1.2fr) minmax(345px,1fr)}.top-meta{gap:14px}.token-field{width:230px}.panel{padding:15px}.timeline-grid{grid-template-columns:minmax(0,1.4fr) minmax(270px,1fr)}.run-main{flex-wrap:wrap}.diff-layout{grid-template-columns:195px minmax(0,1fr)}}
@media(max-width:900px){.topbar{flex-wrap:wrap}.top-meta{width:100%;justify-content:space-between}.workspace,.timeline-grid,.preview-layout{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.system .cards{max-height:130px}.system-traces{display:none}.workspace section{margin-top:14px}.metrics{gap:10px}.metric strong{font-size:24px}.diff-layout{grid-template-columns:1fr}.diff-files{position:static;display:flex;flex-wrap:wrap}.diff-file{max-width:100%}.dialog-header{padding:14px}.dialog-body{padding:14px}.scrubber{padding:12px 14px}.reward-header{gap:8px}.reward-box{padding:10px}.reward-value{font-size:25px}}
@media(prefers-reduced-motion:no-preference){button{transition:background .12s,border-color .12s}.focus-step{animation:step-focus 1.5s ease-out}@keyframes step-focus{from{box-shadow:0 0 0 5px #6ce4b844}to{box-shadow:0 0 0 0 transparent}}}
</style>
</head>
<body>
<main>
<header class="topbar">
 <div><div class="eyebrow">Always-on production / operator console</div><h1>Eval Lab</h1><p>Public data → continuous work → verified assets</p></div>
 <div class="top-meta">
  <div class="token-field"><label for="token">Operator token · kept only in this page</label><input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="Read-only until authenticated" aria-describedby="token-help"><span id="token-help" class="sr-only">Shared by intervention and export actions. Never saved to browser storage.</span></div>
  <div class="connection-box"><div id="connection" class="status" role="status">Connecting to journal…</div><div id="cursor" class="mono">Waiting for durable cursor</div><small>DEV work only · <span class="seal">TEST sealed</span></small></div>
 </div>
</header>
<nav id="view-tabs" class="view-tabs" role="tablist" aria-label="Production views">
 <button id="live-tab" type="button" role="tab" aria-selected="true" aria-controls="live-view">Live Production</button>
 <button id="trace-db-tab" type="button" role="tab" aria-selected="false" aria-controls="trace-db-view" tabindex="-1">Trace DB Search</button>
 <button id="benchmark-tab" type="button" role="tab" aria-selected="false" aria-controls="benchmark-view" tabindex="-1">Opus vs Swarm</button>
 <button id="corpus-tab" type="button" role="tab" aria-selected="false" aria-controls="corpus-view" tabindex="-1">Corpus</button>
 <button id="seeker-tab" type="button" role="tab" aria-selected="false" aria-controls="seeker-view" tabindex="-1">Reward Seeker</button>
</nav>
<div id="live-view" role="tabpanel" aria-labelledby="live-tab">
<section class="panel" aria-labelledby="queue-title">
 <div class="panel-head"><div><div class="eyebrow">01 Fuel + 02 Engine</div><h2 id="queue-title">Public Corpus Queue</h2></div><div class="chips"><span class="chip">TB4 DEV</span><span class="chip">MiMo 567</span><span class="chip">Terminal Wrench</span><span class="chip">Goodfire</span><span class="chip">CheatBench</span></div></div>
 <div class="metrics"><div class="metric"><strong id="queue-depth">—</strong><span>Queue depth (queued + leased)</span></div><div class="metric"><strong id="leased">—</strong><span>Leased jobs</span></div><div class="metric"><strong id="completed">—</strong><span>Completed jobs</span></div><div class="metric"><strong id="events">—</strong><span>Journal sequence</span></div></div>
 <p>Durable journal → three independent consumers. Opening this surface never launches execution or paid model calls.</p>
</section>
<section class="systems-section" aria-labelledby="systems-title"><div class="panel-head"><div><div class="eyebrow">03 Independent consumers</div><h2 id="systems-title">Three Real-Time Systems</h2></div><span class="badge blue">Provisional ≠ confirmed</span></div><div class="grid" id="systems"></div></section>
<div class="workspace">
 <div>
  <section class="panel" aria-labelledby="runs-title"><div class="panel-head"><div><div id="runs-context" class="eyebrow">Live execution / causal evidence</div><h2 id="runs-title">Run Explorer</h2></div><span id="run-count" class="badge">0 runs</span></div>
   <div class="run-toolbar"><label class="sr-only" for="run-search">Filter task executions</label><input id="run-search" type="search" placeholder="Filter tasks, campaigns, or systems…" autocomplete="off"><small id="runs-hint">Select a task to inspect its execution.</small></div>
   <form id="quick-control" class="quick-control" hidden><div class="row"><strong id="quick-title"></strong><button id="quick-dismiss" type="button" class="small">Dismiss</button></div><label for="quick-reason">Reason (required, recorded in the journal)</label><input id="quick-reason" required maxlength="2000" autocomplete="off"><div class="actions"><button id="quick-submit" class="primary" type="submit">Request</button><span id="quick-result" class="notice" role="status"></span></div></form>
   <div id="runs" class="run-list"><p class="empty">Waiting for admitted task executions…</p></div>
  </section>
  <section class="panel" aria-labelledby="interventions-title"><div class="panel-head"><div><div class="eyebrow">Human control gate / campaign-wide</div><h2 id="interventions-title">Live Interventions</h2></div><span class="badge">Audited + idempotent</span></div><p>Campaign-wide: affects every task execution sharing the campaign ID. Requested → acknowledged → applied. A request alone never changes execution state.</p>
   <form id="intervention" class="intervention-fields"><div class="two"><div><label for="action">Campaign-wide action</label><select id="action"><option value="pause">Pause</option><option value="cancel">Cancel</option><option value="quarantine">Quarantine</option></select></div><div><label for="run-id">Campaign ID (run_id)</label><input id="run-id" required maxlength="256" autocomplete="off"></div></div><div><label for="reason">Reason</label><textarea id="reason" required maxlength="2000"></textarea></div><div class="actions"><button id="submit" type="submit">Request campaign-wide intervention</button><span id="result" class="notice" role="status"></span></div></form><div class="cards controls-list" id="controls"></div>
  </section>
 </div>
 <section class="panel assets-panel" aria-labelledby="assets-title"><div class="panel-head"><div><div class="eyebrow">04 Output / verified evidence</div><h2 id="assets-title">Verified Asset Store</h2></div><div class="metric"><strong id="asset-total">—</strong><span>Distinct verified assets</span></div></div><p>Exports are automatically checked against the effective <span class="seal">tb4-audit-v1 TEST seal</span>, including lineage.</p><div id="asset-kinds" class="chips asset-filters" role="group" aria-label="Filter assets by kind"></div><p id="assets-status" class="notice" role="status"></p><div class="cards asset-list" id="assets"></div><button id="assets-refresh" type="button" class="small">Refresh assets</button></section>
</div>
</div>
<section id="trace-db-view" class="panel" role="tabpanel" aria-labelledby="trace-db-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Retained evidence / DEV only</div><h2>Cloud Trace DB Search</h2></div><span class="badge blue">Read only · no model calls</span></div>
 <p>Search retained DEV traces and task issues. Statuses, outcomes, and verified receipts are source claims, not new verification. TEST and heldout records are omitted.</p>
 <form id="cloud-search" class="cloud-filters">
  <div class="cloud-query"><label for="cloud-query">Search retained text</label><input id="cloud-query" type="search" autocomplete="off" placeholder="Task, trace, issue, or tool-step text…"></div>
  <div><label for="cloud-task">Task ID</label><input id="cloud-task" autocomplete="off" placeholder="Exact task ID"></div>
  <div><label for="cloud-defect">Defect class</label><input id="cloud-defect" autocomplete="off" placeholder="Exact defect class"></div>
  <div><label for="cloud-tool">Tool name (traces only)</label><input id="cloud-tool" autocomplete="off" placeholder="Exact tool name"></div>
  <div><label for="cloud-direction">Direction</label><select id="cloud-direction"><option value="">Any direction</option><option value="wrong_pass">wrong_pass</option><option value="unfair_fail">unfair_fail</option></select></div>
  <div class="actions"><label for="cloud-verified" class="check-label"><input id="cloud-verified" type="checkbox"> Only verified receipts / confirmed issues</label><button id="cloud-search-submit" type="submit" class="primary">Search DEV evidence</button></div>
 </form>
 <p id="cloud-search-status" class="notice" role="status"></p>
 <div class="cloud-grid"><div><h3>Agent traces</h3><div id="cloud-traces" class="cards cloud-results"></div></div><div><h3>Task issues</h3><div id="cloud-issues" class="cards cloud-results"></div></div></div>
</section>
<section id="benchmark-view" class="panel" role="tabpanel" aria-labelledby="benchmark-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Retained benchmark comparison / provenance</div><h2>Opus vs Swarm</h2></div><button id="cloud-bench-refresh" type="button" class="small">Refresh retained reports</button></div>
 <p class="seal">Retained DEV-intended studies, not held-out performance</p>
 <p>Named comparisons preserve their own protocol and coverage. Requested models are not evidence of execution; actual served identities and missing results remain explicit. Historical A/B/C and swarm replay reports retain their original meaning.</p>
 <p id="cloud-bench-status" class="notice" role="status"></p>
 <div id="cloud-benchmarks"></div>
 <details id="cloud-bench-rejections" hidden><summary id="cloud-bench-rejection-title">Rejected retained reports</summary><div id="cloud-bench-rejected" class="cards"></div></details>
</section>
<section id="corpus-view" class="panel" role="tabpanel" aria-labelledby="corpus-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Retained corpus census / counts only</div><h2>Goodfire DEV corpus and splits</h2></div><button id="cloud-corpus-refresh" type="button" class="small">Refresh census</button></div>
 <p class="seal">Only rows admitted by the shared DEV gate are counted as DEV. Withheld and gate-denied rows are counted, never opened.</p>
 <p id="cloud-corpus-status" class="notice" role="status"></p>
 <div id="cloud-corpus-goodfire"></div>
 <div id="cloud-corpus-table"></div>
</section>
<section id="seeker-view" class="panel" role="tabpanel" aria-labelledby="seeker-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Retained grader-weakness discovery / provenance</div><h2>Reward-Seeker model comparison</h2></div><button id="cloud-seeker-refresh" type="button" class="small">Refresh retained reports</button></div>
 <p class="seal">Retained DEV-intended studies, not held-out performance</p>
 <p>A verified differential is a submission that scored 1.0 on the original grader and 0.0 on the repaired grader. Only outputs whose sha256 matches the attested model response count as attested. Rates stay unavailable until every cell is graded; no model winner is inferred, and a requested model is not evidence that it served.</p>
 <p id="cloud-seeker-status" class="notice" role="status"></p>
 <div id="cloud-seeker-roster"></div>
 <div id="cloud-seeker-reports"></div>
</section>
<footer>All live views are projections of the durable journal. Provisional suspicion is not a confirmed verdict. TEST and heldout splits are sealed; only verified DEV evidence is exportable. Credentials stay in this page’s memory.</footer>
</main>
<dialog id="timeline-drawer" aria-labelledby="timeline-title" aria-describedby="timeline-provenance"><div class="dialog-layout">
 <div class="dialog-header"><div><div id="timeline-context" class="eyebrow">Live trace / causal-prefix inspector</div><h2 id="timeline-title">Run timeline</h2><p id="timeline-meta" class="mono"></p><div id="timeline-provenance" class="chips" aria-live="polite"></div></div><div class="actions"><button id="timeline-diff" type="button" class="small">Grader diff</button><button type="button" class="small" data-close="timeline-drawer" aria-label="Close timeline">Close · Esc</button></div></div>
 <p id="timeline-status" class="dialog-status notice" role="status"></p>
 <div class="scrubber"><div class="scrubber-top"><div><label for="prefix-scrubber">Causal prefix · ← / → to scrub</label><span id="prefix-position" class="mono"></span></div><label for="follow-live"><input id="follow-live" type="checkbox" checked> Follow live</label></div><input id="prefix-scrubber" type="range" min="0" max="0" value="0" step="1" disabled aria-describedby="prefix-position"><div class="row"><small id="prefix-boundary">Only evidence at or before the selected journal sequence is shown.</small><span id="timeline-state" class="badge"></span></div></div>
 <div class="dialog-body"><div class="row"><h3>Campaign-wide controls</h3><div id="timeline-actions" class="actions"></div></div><p>Actions affect all task executions sharing this campaign ID, not just the selected item. Application is shown only after a control.applied event is recorded.</p><form id="timeline-control" class="quick-control" hidden><strong id="timeline-control-title"></strong><label for="timeline-reason">Reason (required, recorded in the journal)</label><input id="timeline-reason" required maxlength="2000" autocomplete="off"><div class="actions"><button id="timeline-submit" type="submit" class="primary">Request</button><button id="timeline-control-dismiss" type="button">Dismiss</button><span id="timeline-control-result" class="notice" role="status"></span></div></form><div id="timeline-control-progress" class="cards" style="margin:12px 0"></div><div id="timeline-execution" class="execution-summary" hidden aria-label="Latest task execution"></div><div class="timeline-grid"><div><h3>Trace steps</h3><div id="trace-steps" class="trace-steps"></div></div><aside class="trace-rail" aria-label="Evidence at selected prefix"><div><h3>Prefix assessments</h3><div id="prefix-assessments" class="cards"></div></div><div><h3>Suspicion + findings</h3><div id="timeline-cards" class="cards"></div></div><div><h3>Gate decisions</h3><div id="timeline-gates" class="cards"></div></div><div><h3>Campaign-wide control journal</h3><div id="timeline-controls" class="cards"></div></div></aside></div></div>
</div></dialog>
<dialog id="diff-drawer" aria-labelledby="diff-title"><div class="dialog-layout"><div class="dialog-header"><div><div class="eyebrow">Verified evidence / grader comparison</div><h2 id="diff-title">Original vs repaired</h2><p id="diff-meta" class="mono"></p></div><button type="button" class="small" data-close="diff-drawer" aria-label="Close grader diff">Close · Esc</button></div><p id="diff-status" class="dialog-status notice" role="status"></p><div id="diff-content" class="dialog-body"></div></div></dialog>
<dialog id="asset-drawer" aria-labelledby="asset-title"><div class="dialog-layout"><div class="dialog-header"><div><div class="eyebrow">Verified Asset Store / asset inspection</div><h2 id="asset-title">Verified asset</h2><p id="asset-meta" class="mono"></p></div><button type="button" class="small" data-close="asset-drawer" aria-label="Close asset inspection">Close · Esc</button></div><p id="asset-status" class="dialog-status notice" role="status"></p><div id="asset-content" class="dialog-body"></div></div></dialog>
<script>
'use strict';
const $ = id => document.getElementById(id);
const names = {task_quality:'Task Quality',reward_seeker:'Reward Seeker',misalignment:'Misalignment Monitor'};
const kinds = {repaired_task:'Repaired tasks',exploit_witness:'Exploit witnesses',labeled_trace:'Labeled traces'};
const replayLabel='RECORDED REPLAY - monitoring computed now';
const recordedContext='Recorded trajectory, causal prefix computed live';
const isReplay=sourceKind=>sourceKind==='public_replay';
function addReplayBadge(parent,sourceKind){
 if(isReplay(sourceKind))parent.append(node('span',replayLabel,'badge warn replay-badge'));
}
const list = value => Array.isArray(value) ? value : [];
const text = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value,null,2) : String(value);
const node = (tag,value,cls) => {const el=document.createElement(tag);if(value!==undefined)el.textContent=text(value);if(cls)el.className=cls;return el;};
const badge = (value,tone='') => node('span',value,'badge '+tone);
const button = (label,handler,cls='small') => {const el=node('button',label,cls);el.type='button';el.addEventListener('click',handler);return el;};
const tone = value => ['confirmed','applied','pass','dev','completed'].includes(value) ? 'good' : ['refuted','blocked','quarantined','cancelled','failed','expired'].includes(value) ? 'bad' : ['provisional','requested','paused'].includes(value) ? 'warn' : 'blue';
const stateClass = value => ['confirmed','applied','provisional','requested','refuted','expired'].includes(value) ? value : '';
function notice(id,message,error=false){const el=$(id);el.textContent=message;el.className=(el.classList.contains('dialog-status')?'dialog-status ':'')+'notice'+(error?' error':'');}
function fillCards(parent,items,render,empty){parent.replaceChildren();if(!items.length){parent.append(node('p',empty,'empty'));return;}for(const item of items)parent.append(render(item));}
function basicCard(title,detail,status){const el=node('div',undefined,'card '+stateClass(status));el.append(node('strong',title),node('small',detail));return el;}
class ApiError extends Error{constructor(status,detail){super(typeof detail==='string'?detail:text(detail?.message||detail));this.status=status;this.detail=detail;}}
async function api(path,options={}){const response=await fetch(path,{cache:'no-store',...options});let data;try{data=await response.json();}catch{throw new ApiError(response.status,'Server returned an unreadable response.');}if(!response.ok)throw new ApiError(response.status,data.detail??data);return data;}
const exportDisabledMessage='Export disabled: the server has no configured export directory.';
function errorMessage(error){
 if(error.status===401)return 'Unauthorized: check the operator token in the top bar.';
 if(error.detail?.code==='export_disabled')return exportDisabledMessage;
 if(error.detail?.code==='export_invalid')return 'Export invalid: '+error.message;
 if(error.detail?.code==='ambiguous_run')
  return 'This campaign contains multiple tasks. Close this inspector and select a task execution in Run Explorer to inspect its timeline or grader diff.';
 if(error.detail?.code==='split_sealed')
  return 'Sealed: this evidence belongs to a TEST or heldout split. Inspection and export are not allowed.';
 if(error.detail?.code==='path_outside_roots')
  return 'Artifact path is outside the server’s approved evidence roots.';
 return error.message||'Request failed.';
}
function cloudApi(path){
 const token=$('token').value;
 return api(path,{headers:token?{Authorization:'Bearer '+token}:{}});
}
function cloudError(error,label){
 if(error.status===404)return label+' is unavailable on this control plane (404). Open the authenticated cloud dashboard to use retained cloud data.';
 if(error.status===503)return label+' is temporarily unavailable (503). Retry when the cloud store is ready.';
 if(error.status===401)return label+' requires authentication (401). Sign in again or enter the cloud operator token above, then retry.';
 return label+': '+errorMessage(error);
}
function cloudIssueLine(issue){
 return text(issue.issue_id)+' · '+text(issue.defect_class)+' · '+text(issue.direction)+' · status: '+text(issue.status);
}
function cloudTraceCard(trace){
 const card=basicCard(trace.task_id+' · '+trace.model_id,'Trace: '+trace.trace_id);
 const meta=node('div',undefined,'chips');
 meta.append(badge('Outcome: '+text(trace.outcome),tone(trace.outcome)),
  badge('Reward: '+text(trace.reward)),badge('is_exploit: '+text(trace.is_exploit)),
  badge(text(trace.step_count)+' steps'));
 const receipt=trace.verified_receipt_json,receiptStatus=receipt?.status;
 meta.append(badge('Verified receipt: '+(receiptStatus??'not recorded'),
  ['verified','confirmed'].includes(receiptStatus)?'good':'warn'));
 const issues=node('ul',undefined,'cloud-issue-list');
 for(const issue of list(trace.issues))issues.append(node('li',cloudIssueLine(issue)));
 card.append(meta);
 if(issues.children.length)card.append(issues);
 else card.append(node('small','No task issues retained.'));
 return card;
}
function cloudIssueCard(issue){
 const card=basicCard(issue.task_id,cloudIssueLine(issue),issue.status);
 card.append(node('small','Original reward: '+text(issue.original_reward)+
  ' · repaired reward: '+text(issue.repaired_reward)));
 return card;
}
let cloudSearchVersion=0;
async function searchCloudDb(event){
 event?.preventDefault();
 const version=++cloudSearchVersion,params=new URLSearchParams({split:'dev'});
 for(const [id,key] of [['cloud-query','q'],['cloud-task','task_id'],['cloud-defect','defect_class'],['cloud-direction','direction']]){
  const value=$(id).value.trim();if(value)params.set(key,value);
 }
 params.set('only_verified',String($('cloud-verified').checked));
 const traceParams=new URLSearchParams(params),tool=$('cloud-tool').value.trim();
 if(tool)traceParams.set('tool_name',tool);
 $('cloud-search-submit').disabled=true;
 $('cloud-traces').replaceChildren();$('cloud-issues').replaceChildren();
 notice('cloud-search-status','Searching retained DEV evidence…');
 try{
  const results=await Promise.allSettled([
   cloudApi('/api/cloud-db/traces?'+traceParams),cloudApi('/api/cloud-db/issues?'+params)
  ]);
  if(version!==cloudSearchVersion)return;
  const messages=[];
  for(const [index,[key,render,label]] of [
   ['traces',cloudTraceCard,'Trace DB search'],['issues',cloudIssueCard,'Task issue search']
  ].entries()){
   const result=results[index],target=$('cloud-'+key);
   if(result.status==='rejected'){
    const message=cloudError(result.reason,label);
    target.append(node('p',message,'empty'));messages.push(message);continue;
   }
   const retained=list(result.value?.[key]);
   const rows=retained.filter(row=>typeof row?.task?.split==='string'&&row.task.split.trim().toLowerCase()==='dev');
   fillCards(target,rows,render,'No retained DEV '+key+' match these filters.');
   messages.push(rows.length+' DEV '+key+(rows.length<retained.length?' · non-DEV records omitted':''));
  }
  notice('cloud-search-status',messages.join('\n'),results.some(result=>result.status==='rejected'));
 }catch(error){
  if(version===cloudSearchVersion)notice('cloud-search-status',cloudError(error,'Cloud Trace DB Search'),true);
 }finally{
  if(version===cloudSearchVersion)$('cloud-search-submit').disabled=false;
 }
}
function cloudTable(headers,rows){
 const wrap=node('div',undefined,'cloud-table-wrap'),table=node('table',undefined,'cloud-table');
 const head=node('thead'),heading=node('tr'),body=node('tbody');
 for(const label of headers){const cell=node('th',label);cell.scope='col';heading.append(cell);}
 head.append(heading);
 for(const values of rows){
  const row=node('tr');
  for(const value of values){const cell=node('td');cell.append(value instanceof Node?value:node('span',value));row.append(cell);}
  body.append(row);
 }
 table.append(head,body);wrap.append(table);return wrap;
}
const cloudFraction=value=>typeof value==='number'&&Number.isFinite(value)?(value*100).toFixed(2)+'%':'Not recorded';
const cloudSpend=value=>typeof value==='number'&&Number.isFinite(value)?'$'+value.toFixed(4):'Not recorded';
function cloudDigest(value){
 const digest=node('code',typeof value==='string'?value.slice(0,12):'Not recorded','mono');
 if(typeof value==='string')digest.title=value;
 return digest;
}
function cloudComparison(report,parent){
 const meanings={A:'Single strong model + harness',B:'Many-cheap-agents swarm',C:'Hybrid swarm + Opus adjudicator'};
 parent.append(node('p',report.cohort.dataset_id+' · DEV · '+report.cohort.task_count+
  ' tasks · generated '+report.generated_at));
 const rows=['A','B','C'].map(id=>{
  const arm=list(report.arms).find(value=>value?.arm===id);
  const label=node('div',id+' · '+meanings[id]);
  if(!arm)return [label,'No retained result','—','—','—','—','—','—','—','—'];
  label.append(node('small',arm.label));
  const harness=node('div',arm.harness);
  harness.append(node('small',arm.retrieval?'Retrieval enabled':'Retrieval disabled'));
  return [label,list(arm.served_models).join(', '),harness,
   arm.metrics.primary_hits_n+'/'+arm.metrics.primary_total_n,
   cloudFraction(arm.metrics.precision),cloudFraction(arm.metrics.recall),cloudFraction(arm.metrics.f1),
   cloudSpend(arm.spend_usd),arm.model_calls,cloudDigest(arm.artifact_sha256)];
 });
 parent.append(cloudTable(['Arm','Actually served models','Harness / retrieval','Primary hits n/N',
  'Precision','Recall','F1','Spend USD','Model calls','Artifact SHA-256'],rows));
}
function cloudNamedComparison(report,parent){
 const cohort=report.cohort,total=cohort.task_count;
 parent.append(node('p','Protocol / input stratum: '+report.protocol),
  node('p',cohort.dataset_id+' · reported cohort: '+total+' intended-DEV identities · eligible '+
   cohort.eligible_n+' · blocked '+cohort.blocked_n+' · generated '+report.generated_at),
  node('p','Coverage status: '+report.status.toUpperCase(),'seal'),
  node('p',report.status==='complete'?
   'Reported answer and judgment coverage is complete; undefined metrics remain unavailable.':
   'Full-comparison metrics are unavailable until every required answer and judgment is retained.'));
 parent.append(node('small','Coverage, clearance, model identities, and artifacts are retained source claims, not independent verification by this page. Protocol strata are not pooled; no model winner is inferred.'));
 const fraction=value=>value===null?'Not available':cloudFraction(value);
 const rows=list(report.arms).map(arm=>{
  const label=node('div',arm.arm);label.append(node('small',arm.label));
  const coverage=arm.coverage;
  const counts=node('div','Answered '+coverage.answered_n+'/'+total+' · judged '+coverage.judged_n+'/'+total);
  counts.append(node('small','Blocked '+coverage.blocked_n+' · missing '+coverage.missing_n),
   node('small','Refused '+coverage.refused_n+' · unavailable '+coverage.unavailable_n),
   node('small','Malformed '+coverage.malformed_n+' · unattested '+coverage.unattested_n));
  const harness=node('div',arm.harness);
  harness.append(node('small',arm.retrieval?'Retrieval enabled':'Retrieval disabled'));
  const metrics=arm.metrics;
  const hits=metrics.primary_hits_n===null||metrics.primary_total_n===null?
   'Not available':metrics.primary_hits_n+'/'+metrics.primary_total_n;
  return [label,counts,list(arm.requested_models).join(', ')||'Not recorded',
   list(arm.served_models).join(', ')||'Not recorded',harness,hits,
   fraction(metrics.precision),fraction(metrics.recall),fraction(metrics.f1),
   cloudSpend(arm.spend_usd),arm.model_calls===null?'Not recorded':arm.model_calls,
   cloudDigest(arm.artifact_sha256)];
 });
 parent.append(cloudTable(['Named arm','Reported coverage','Requested models','Actually served models',
  'Harness / retrieval','Primary hits n/N','Precision','Recall','F1','Spend USD','Model calls',
  'Artifact SHA-256'],rows));
}
function cloudSwarm(report,parent){
 parent.append(node('p','Swarm selection replay · not a measured A/B/C comparison. Budget per task: '+
  report.budget_per_task+' · replay API spend: '+cloudSpend(report.paid_api_spend_usd)+
  ' · replay model calls: '+report.model_calls));
 parent.append(node('small','Replay spend is not total upstream worker cost. Off-key predictions are unverified, not certified false positives.'));
 const rows=Object.entries(report.strategies||{}).map(([name,score])=>[
  name,score.selected_count,score.primary_hits_n+'/'+score.primary_total_n,
  score.secondary_hits_n+'/'+score.secondary_total_n,cloudFraction(score.unique_key_precision),
  cloudFraction(score.secondary_f1),score.within_task_budget?'Within budget':'Unbudgeted / not equal-budget'
 ]);
 parent.append(cloudTable(['Strategy','Selected','Primary hits n/N','Secondary hits n/N',
  'Unique-key precision','Secondary F1','Task budget'],rows));
 const telemetry=node('details');
 telemetry.append(node('summary','Retained worker telemetry (source claims)'),
  node('pre',report.worker_telemetry));
 parent.append(telemetry);
}
async function loadCloudBenchmarks(){
 $('cloud-bench-refresh').disabled=true;
 $('cloud-benchmarks').replaceChildren();$('cloud-bench-rejected').replaceChildren();
 $('cloud-bench-rejections').hidden=true;
 notice('cloud-bench-status','Loading retained DEV benchmark reports…');
 try{
  const data=await cloudApi('/api/cloud-db/benchmarks'),retained=list(data?.benchmarks);
  const reports=retained.filter(entry=>{
   const schema=entry?.report?.schema;
   return schema==='evallab.arm_comparison/v1'||schema==='evallab.arm_comparison/v2'?
    entry.report.cohort?.split==='dev':schema==='evallab.swarm_bench/v1'&&
    entry.report.split==='dev'&&(!entry.report.cohort||entry.report.cohort.split==='dev');
  });
  for(const entry of reports){
   const report=entry.report,section=node('article',undefined,'cloud-report');
   section.append(node('h3',entry.file),node('small',report.schema));
   cloudProvenance(entry,section);
   if(report.schema==='evallab.arm_comparison/v2')cloudNamedComparison(report,section);
   else{
    section.append(node('p','DEV-only key agreement, not held-out performance','seal'));
    if(report.schema==='evallab.arm_comparison/v1')cloudComparison(report,section);
    else cloudSwarm(report,section);
   }
   $('cloud-benchmarks').append(section);
  }
  const rejected=list(data?.rejected);
  $('cloud-bench-rejections').hidden=!rejected.length;
  $('cloud-bench-rejection-title').textContent='Rejected retained reports ('+rejected.length+')';
  fillCards($('cloud-bench-rejected'),rejected,item=>basicCard(item.file,item.reason),'');
  notice('cloud-bench-status',(reports.length?reports.length+' retained DEV benchmark reports.':
   'No retained DEV benchmark reports. No benchmark results have been fabricated.')+
   (rejected.length?' '+rejected.length+' invalid or unsafe files rejected; expand details below.':'')+
   (reports.length<retained.length?' Non-DEV or unknown reports omitted.':''));
 }catch(error){
  $('cloud-benchmarks').replaceChildren();
  notice('cloud-bench-status',cloudError(error,'Retained benchmark comparison'),true);
 }finally{$('cloud-bench-refresh').disabled=false;}
}
const cloudCount=value=>Number.isInteger(value)&&value>=0?value.toLocaleString('en-US'):'Not recorded';
function cloudTile(value,label){
 const tile=node('div',undefined,'metric');
 tile.append(node('strong',value),node('span',label));
 return tile;
}
async function loadCloudCorpus(){
 $('cloud-corpus-refresh').disabled=true;
 $('cloud-corpus-goodfire').replaceChildren();$('cloud-corpus-table').replaceChildren();
 notice('cloud-corpus-status','Loading retained corpus census…');
 try{
  const data=await cloudApi('/api/cloud-db/corpus'),datasets=list(data?.datasets);
  const goodfire=datasets.filter(item=>/goodfire/i.test(text(item.dataset_id)));
  for(const item of goodfire){
   const box=node('article',undefined,'cloud-report'),tiles=node('div',undefined,'metrics');
   tiles.append(cloudTile(cloudCount(item.admitted_dev.tasks),'DEV tasks'),
    cloudTile(cloudCount(item.admitted_dev.traces),'DEV traces'),
    cloudTile(cloudCount(item.admitted_dev.steps),'DEV steps'),
    cloudTile(cloudCount(item.enacted_exploit_traces),'Enacted-hack traces (source label)'),
    cloudTile(cloudCount(item.withheld.tasks),'Withheld tasks (not served to agents)'),
    cloudTile(cloudCount(item.withheld.traces),'Withheld traces'));
   box.append(node('h3',item.dataset_id),tiles);
   if(item.dev_not_admitted.tasks)box.append(node('small',cloudCount(item.dev_not_admitted.tasks)+
    ' DEV-labelled tasks are denied by the shared gate and are not counted as DEV.'));
   $('cloud-corpus-goodfire').append(box);
  }
  const rows=datasets.map(item=>[item.dataset_id,cloudCount(item.admitted_dev.tasks),
   cloudCount(item.admitted_dev.traces),cloudCount(item.admitted_dev.steps),
   cloudCount(item.enacted_exploit_traces),
   cloudCount(item.dev_not_admitted.tasks)+' / '+cloudCount(item.dev_not_admitted.traces),
   cloudCount(item.withheld.tasks)+' / '+cloudCount(item.withheld.traces)]);
  $('cloud-corpus-table').append(cloudTable(['Dataset','Admitted DEV tasks','Admitted DEV traces',
   'Admitted DEV steps','Enacted-hack traces (source label)','DEV-labelled, gate-denied (tasks / traces)',
   'Withheld (tasks / traces)'],rows));
  notice('cloud-corpus-status',(datasets.length?datasets.length+' datasets retained. ':
   'No retained corpus rows. ')+(goodfire.length||!datasets.length?'':'No Goodfire dataset is retained. ')+
   text(data?.notice));
 }catch(error){
  notice('cloud-corpus-status',cloudError(error,'Corpus census'),true);
 }finally{$('cloud-corpus-refresh').disabled=false;}
}
const seekerRoster=['meta/muse-spark-open','meta/muse-spark-1.3-contributor',
 'meta/muse-wonder-1.0-contributor','kimi-code/k3.1-pro-highspeed',
 'openrouter/deepseek/deepseek-v4.1-flash','anthropic/claude-sonnet-5-5'];
const modelBase=value=>text(value).toLowerCase().replace(/:[^:/]+$/,'');
function cloudProvenance(entry,section){
 const provenance=node('details',undefined,'cloud-provenance');
 provenance.append(node('summary','Document SHA-256: '+text(entry.sha256).slice(0,12)),node('pre',entry.sha256));
 section.append(provenance);
}
function cloudSeekerReport(report,parent){
 const cohort=report.cohort,total=cohort.task_count,complete=report.status==='complete';
 parent.append(node('p','Protocol: '+report.protocol),
  node('p',cohort.dataset_id+' · reported cohort: '+total+' intended-DEV identities · eligible '+
   cohort.eligible_n+' · blocked '+cohort.blocked_n+' · generated '+report.generated_at),
  node('p','Coverage status: '+report.status.toUpperCase(),'seal'),
  node('p',complete?'Every cell is attested and graded; rates are per cell and conditions are not pooled.':
   'Rates are unavailable until every arm and condition is attested and graded.'));
 parent.append(node('small','Coverage, served identities, attestation and grading are retained source claims, not independent verification by this page. No model winner is inferred.'));
 const rows=[];
 for(const arm of list(report.arms)){
  for(const cell of list(arm.cells)){
   const label=node('div',arm.arm);label.append(node('small',arm.label),node('small',arm.requested_model));
   const states=node('div','Attested '+cell.attested_n+'/'+total);
   states.append(node('small','Unattested '+cell.unattested_n+' · refused '+cell.refused_n+' · unavailable '+cell.unavailable_n),
    node('small','Malformed '+cell.malformed_n+' · blocked '+cell.blocked_n+' · missing '+cell.missing_n));
   const served=list(cell.served_models),shown=node('div',served.join(', ')||'Not recorded');
   if(served.length&&!(served.length===1&&modelBase(served[0])===modelBase(arm.requested_model)))
    shown.append(badge('Differs from requested','warn'));
   const funnel=node('div','Graded '+cell.graded_n+'/'+total);
   funnel.append(node('small','Original grader 1.0: '+cell.original_pass_n));
   rows.push([label,cell.condition,states,funnel,cell.verified_differential_n+'/'+total,
    complete?cloudFraction(cell.verified_differential_n/total):'Not available',shown,
    cloudSpend(cell.spend_usd),cell.model_calls===null?'Not recorded':cell.model_calls,
    cloudDigest(cell.artifact_sha256),cloudDigest(cell.prompt_sha256)]);
  }
 }
 parent.append(cloudTable(['Named model (requested)','Prompt condition','Attested outputs','Graded',
  'Verified differential n/N','Differential rate','Actually served models','Spend USD','Model calls',
  'Artifact SHA-256','Prompt SHA-256'],rows));
}
async function loadSeekerComparison(){
 $('cloud-seeker-refresh').disabled=true;
 $('cloud-seeker-reports').replaceChildren();$('cloud-seeker-roster').replaceChildren();
 notice('cloud-seeker-status','Loading retained Reward-Seeker reports…');
 try{
  const data=await cloudApi('/api/cloud-db/benchmarks');
  const reports=list(data?.benchmarks).filter(entry=>
   entry?.report?.schema==='evallab.seeker_comparison/v1'&&entry.report.cohort?.split==='dev');
  const named=new Map();
  for(const entry of reports){
   const report=entry.report,section=node('article',undefined,'cloud-report');
   section.append(node('h3',entry.file),node('small',report.schema));
   cloudProvenance(entry,section);
   cloudSeekerReport(report,section);
   $('cloud-seeker-reports').append(section);
   for(const arm of list(report.arms)){
    const key=modelBase(arm.requested_model);named.set(key,(named.get(key)||0)+1);
   }
  }
  const roster=node('article',undefined,'cloud-report');
  roster.append(node('h3','Declared comparison lanes'),
   node('small','Declared by the Priority #2 contract. Presence here is a request, not a result.'),
   cloudTable(['Declared requested model','Retained reports naming it'],seekerRoster.map(id=>[id,
    named.get(modelBase(id))?'Named in '+named.get(modelBase(id))+' retained report(s)':
    'No retained report names this model'])));
  $('cloud-seeker-roster').append(roster);
  notice('cloud-seeker-status',reports.length?reports.length+' retained DEV Reward-Seeker comparison reports.':
   'No retained DEV Reward-Seeker comparison report. No results have been fabricated.');
 }catch(error){
  notice('cloud-seeker-status',cloudError(error,'Reward-Seeker comparison'),true);
 }finally{$('cloud-seeker-refresh').disabled=false;}
}
$('cloud-corpus-refresh').addEventListener('click',loadCloudCorpus);
$('cloud-seeker-refresh').addEventListener('click',loadSeekerComparison);
$('cloud-search').addEventListener('submit',searchCloudDb);
$('cloud-bench-refresh').addEventListener('click',loadCloudBenchmarks);
const viewTabs=[...$('view-tabs').children];
for(const tab of viewTabs){
 tab.addEventListener('click',()=>{
  for(const item of viewTabs){
   const selected=item===tab;
   item.setAttribute('aria-selected',String(selected));item.tabIndex=selected?0:-1;
   $(item.getAttribute('aria-controls')).hidden=!selected;
  }
  if(tab.id==='trace-db-tab')searchCloudDb();
  if(tab.id==='benchmark-tab')loadCloudBenchmarks();
  if(tab.id==='corpus-tab')loadCloudCorpus();
  if(tab.id==='seeker-tab')loadSeekerComparison();
 });
 tab.addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();
  let next=event.key==='Home'?0:event.key==='End'?viewTabs.length-1:
   (viewTabs.indexOf(tab)+(event.key==='ArrowRight'?1:-1)+viewTabs.length)%viewTabs.length;
  viewTabs[next].click();viewTabs[next].focus();
 });
}
function assetBadge(asset){
 return badge(asset.exportable?'Guard eligible':
  asset.blocked_reason==='export_disabled'?'Export disabled':'Guard blocked',
  asset.exportable?'good':'bad');
}
function blockedReason(asset){
 return asset.blocked_reason==='export_disabled'?exportDisabledMessage:asset.blocked_reason;
}
const dialogFocus=new Map();
function openDialog(id){const dialog=$(id);if(!dialog.open){dialogFocus.set(id,document.activeElement);dialog.showModal();dialog.querySelector('[data-close]')?.focus();}}
for(const el of document.querySelectorAll('[data-close]'))el.addEventListener('click',()=>$(el.dataset.close).close());
for(const dialog of document.querySelectorAll('dialog'))dialog.addEventListener('close',()=>{const previous=dialogFocus.get(dialog.id);if(previous?.isConnected)previous.focus();else if(!document.querySelector('dialog[open]'))$('run-search').focus();});
let currentState=null,assetSignature=null,assetData={assets:[],by_kind:{},total:0},assetFilter='all';
let runSignature='',assetListSignature='';
const controlRequests=new Map(),exportRequests=new Map(),exportResults=new Map();
let quickTarget=null,timelineTarget=null;
const drafts=new Map();
function controlsFor(runId){return list(currentState?.interventions).filter(c=>c.run_id===runId);}
function progression(control){const el=node('div',undefined,'progress');const order=['requested','acknowledged','applied'];const index=order.indexOf(control.state);order.forEach((state,i)=>{if(i)el.append(node('span','→'));el.append(node('span',state,i===index?'current':i<index?'done':'pending'));});if(index<0)el.append(badge(control.state,tone(control.state)));return el;}
function controlCard(control){const el=basicCard('Campaign-wide '+(control.action||'control')+' · '+control.state,'Campaign: '+(control.run_id||'')+' · '+(control.operator||'executor')+(control.reason?' · '+control.reason:''),control.state);el.append(progression(control),node('small',control.request_id));return el;}
function renderControls(){fillCards($('controls'),list(currentState?.interventions).slice(-10).reverse(),controlCard,'No intervention requests.');if(timelineRun)fillCards($('timeline-control-progress'),controlsFor(timelineRun).slice(-2).reverse(),controlCard,'No campaign-wide interventions.');}
function mergedRuns(state){
 const result=new Map();
 const add=(trace,system)=>{
  if(!trace?.run_id)return;
  const previous=result.get(trace.run_id)||{
   run_id:trace.run_id,systems:new Set(),prefix_step:null,run_state:'unknown',source_kind:null,steps:0
  };
  if(system||trace.system)previous.systems.add(system||trace.system);
  if(trace.prefix_step!=null)previous.prefix_step=Math.max(previous.prefix_step??0,trace.prefix_step);
  previous.steps=Math.max(previous.steps,list(trace.steps).length);
  if(trace.run_state)previous.run_state=trace.run_state;
  if(typeof trace.source_kind==='string')previous.source_kind=trace.source_kind;
  result.set(trace.run_id,previous);
 };
 for(const trace of list(state.partial_traces))add(trace);
 for(const [system,data] of Object.entries(state.systems||{})){
  for(const trace of list(data.partial_traces))add(trace,system);
  for(const worker of list(data.workers))if(worker.run_id)add(worker,system);
 }
 for(const asset of list(state.verified_assets?.recent))add(asset);
 for(const control of list(state.interventions))add(control);
 return [...result.values()];
}
const noTraceLabel='Worker lifecycle · no trace steps recorded';
const hasRunCatalog=state=>Object.prototype.hasOwnProperty.call(state,'run_items');
function runRows(state){
 return hasRunCatalog(state)?list(state.run_items?.recent):
  mergedRuns(state).map(run=>({...run,systems:[...run.systems]}));
}
function taskSelectors(value){
 return typeof value?.dataset_id==='string'&&value.dataset_id&&
  typeof value.item_id==='string'&&value.item_id?
  {dataset_id:value.dataset_id,item_id:value.item_id}:{};
}
const runKey=run=>run.key??JSON.stringify([run.run_id,run.dataset_id??null,run.item_id??null]);
function selectedRunItem(runId,selectors){
 const matches=runRows(currentState||{}).filter(run=>run.run_id===runId);
 return selectors.dataset_id?
  matches.find(run=>run.dataset_id===selectors.dataset_id&&run.item_id===selectors.item_id):
  matches.length===1?matches[0]:null;
}
function executionDetails(run){
 const details=node('div'),systems=node('div',undefined,'run-meta');
 addReplayBadge(systems,run.source_kind);
 for(const [system,state] of Object.entries(run.system_states||{}))
  systems.append(badge((names[system]||system)+' · '+state,tone(state)));
 if(!Object.keys(run.system_states||{}).length)
  systems.append(node('small','No per-system lifecycle recorded.'));
 const jobs=Object.entries(run.jobs_submitted||{});
 const submitted=jobs.reduce((sum,[,count])=>sum+Number(count),0);
 const facts=node('dl',undefined,'run-facts');
 for(const [label,value] of [
  ['Attempts',run.attempts??0],['Submitted jobs',submitted],
  ['Active leases',run.active_leases??0],['Verified assets',list(run.verified_asset_ids).length]
 ]){
  const fact=node('div',undefined,'run-fact');
  fact.append(node('dt',label),node('dd',value));
  facts.append(fact);
 }
 details.append(systems,facts,
  node('p','Last outcome: '+(run.outcome_status??'not recorded')+
   ' · original reward '+text(run.original_reward)+' → repaired reward '+text(run.repaired_reward),'run-outcome'));
 if(jobs.length)details.append(node('small','Submitted: '+jobs.map(([type,count])=>type+' '+count).join(' · ')));
 details.append(node('p',run.has_trace?'Trace steps recorded':noTraceLabel,'muted'),
  node('small',(run.last_event_type||'No event')+' · journal seq '+(run.last_seq??'—')));
 return details;
}
function runButtons(runId,inTimeline=false){
 const group=node('div',undefined,'campaign-controls'),actions=node('div',undefined,'run-controls');
 group.append(node('small','Campaign-wide'));
 for(const action of ['pause','quarantine','cancel']){
  const b=button(action[0].toUpperCase()+action.slice(1),
   ()=>beginControl(runId,action,inTimeline),'small'+(action==='cancel'?' danger':''));
  b.setAttribute('aria-label',action+' campaign '+runId+' · affects all task executions');
  actions.append(b);
 }
 group.append(actions);
 return group;
}
function renderRuns(){
 if(!currentState)return;
 const catalog=hasRunCatalog(currentState),all=runRows(currentState);
 const filter=$('run-search').value.trim().toLowerCase();
 const recorded=all.filter(run=>isReplay(run.source_kind)).length;
 const total=catalog?(currentState.run_items?.total??all.length):all.length;
 $('run-count').textContent=catalog?
  (currentState.run_items?.truncated?all.length+' / '+total:total)+' task executions':
  all.length+' runs';
 $('runs-context').textContent=recorded&&recorded===all.length?recordedContext:
  recorded?'Live execution + recorded replay / causal evidence':'Live execution / task evidence';
 $('runs-hint').textContent=catalog?
  'Select a task to inspect its worker lifecycle or trace. Controls are campaign-wide.'+
   (currentState.run_items?.truncated?' Showing the most recent retained tasks.':''):
  recorded?'Click a run to inspect its causal prefix.':'Click a run to inspect its live prefix.';
 const runs=all.filter(run=>[
  run.run_id,run.dataset_id,run.item_id,run.run_state,
  ...Object.entries(run.system_states||{}).flatMap(([system,state])=>[names[system]||system,state]),
  ...list(run.systems).map(system=>names[system]||system)
 ].join(' ').toLowerCase().includes(filter));
 const signature=JSON.stringify([catalog,runs,currentState.interventions,filter]);
 if(signature===runSignature)return;
 runSignature=signature;
 const active=document.activeElement;
 const focus=active?.closest('.run-row')?{
  key:active.closest('.run-row').dataset.key,label:active.getAttribute('aria-label')||active.textContent
 }:null;
 fillCards($('runs'),runs,run=>{
  const el=node('article',undefined,'run-row');
  el.dataset.run=run.run_id;
  el.dataset.key=runKey(run);
  if(catalog){el.dataset.dataset=run.dataset_id;el.dataset.item=run.item_id;}
  const head=node('div',undefined,'run-main');
  head.append(button(catalog?run.dataset_id+' / '+run.item_id:run.run_id,
   ()=>openTimeline(run.run_id,run),'link'),
   badge((isReplay(run.source_kind)?'Replay: ':'')+run.run_state,tone(run.run_state)));
  el.append(head);
  if(catalog){
   el.append(node('p','Campaign: '+run.run_id,'mono run-campaign'),executionDetails(run));
  }else{
   const meta=node('div',undefined,'run-meta');
   addReplayBadge(meta,run.source_kind);
   for(const system of run.systems)meta.append(badge(names[system]||system));
   meta.append(node('small','Prefix '+(run.prefix_step??'—')+' · '+run.steps+' projected steps'));
   el.append(meta);
  }
  const bottom=node('div',undefined,'run-bottom'),inspect=node('div',undefined,'actions');
  inspect.append(button(catalog?'Inspect task':'Inspect timeline',()=>openTimeline(run.run_id,run)),
   button('Grader diff',()=>openDiff({run_id:run.run_id,...taskSelectors(run)})));
  bottom.append(inspect,runButtons(run.run_id));
  el.append(bottom);
  const control=controlsFor(run.run_id).at(-1);
  if(control)el.append(node('div','Campaign-wide '+control.action+' · '+control.state+
   ' · '+(control.reason||''),'control-summary'),progression(control));
  return el;
 },filter?'No task executions match this filter.':catalog?
  'No DEV task executions yet. Admitted, submitted, and completed tasks appear here even without trace steps.':
  'No live traces yet. Runs appear as workers publish prefixes.');
 if(focus){
  for(const el of $('runs').querySelectorAll('button'))
   if(el.closest('.run-row').dataset.key===focus.key&&
    (el.getAttribute('aria-label')||el.textContent)===focus.label){
    el.focus({preventScroll:true});break;
   }
 }
}
function renderSystems(state){
 $('systems').replaceChildren();
 for(const [key,name] of Object.entries(names)){
  const data=state.systems?.[key]||{},el=node('article',undefined,'system');
  const partial=list(data.partial_traces),recorded=partial.filter(trace=>isReplay(trace.source_kind)).length;
  const head=node('div',undefined,'row');
  head.append(node('h3',name),badge((data.active_workers??0)+' workers',(data.active_workers??0)>0?'good':''));
  const counts=recorded?[
   ...(partial.length>recorded?[(partial.length-recorded)+' live traces']:[]),
   recorded+' recorded replays'
  ].join(' · '):partial.length+' live traces';
  el.append(head,node('p',counts+' · '+list(data.suspicion_cards).length+' findings'));
  const findings=node('div',undefined,'cards');
  fillCards(findings,list(data.suspicion_cards).slice(-2).reverse(),finding=>{
   const card=basicCard(finding.summary,finding.severity+' · '+finding.status,finding.status);
   card.append(button(finding.run_id,()=>openTimeline(finding.run_id,finding),'link'));
   return card;
  },'No suspicion cards yet.');
  const traces=node('div',undefined,'system-traces');
  for(const trace of partial.slice(-3)){
   const entry=button(trace.run_id+' · '+(trace.prefix_step??'—'),()=>openTimeline(trace.run_id,trace));
   addReplayBadge(entry,trace.source_kind);
   traces.append(entry);
  }
  el.append(findings,traces);
  $('systems').append(el);
 }
}
function render(state){if(currentState&&Number(state.last_seq)<Number(currentState.last_seq))return;currentState=state;$('cursor').textContent=state.cursor||'Journal is empty';$('events').textContent=state.last_seq??0;$('queue-depth').textContent=state.queue?.depth??0;$('leased').textContent=state.queue?.jobs?.leased??0;$('completed').textContent=state.queue?.jobs?.completed??0;renderSystems(state);renderRuns();renderControls();if(timelineRun&&$('timeline-drawer').open)renderTimelineExecution();const signature=JSON.stringify(state.verified_assets||{});if(signature!==assetSignature){assetSignature=signature;refreshAssets();}}
function beginControl(runId,action,inTimeline){const prefix=inTimeline?'timeline':'quick';const target={runId,action};if(inTimeline)timelineTarget=target;else quickTarget=target;$(inTimeline?'timeline-control':'quick-control').hidden=false;$(inTimeline?'timeline-control-title':'quick-title').textContent='Campaign-wide '+action+' · '+runId+' · all task executions';$(prefix+'-reason').value=drafts.get(runId+'|'+action)||'';$(prefix+'-submit').textContent='Request campaign-wide '+action;notice(inTimeline?'timeline-control-result':'quick-result','');$(prefix+'-reason').focus();if(!inTimeline)$('quick-control').scrollIntoView({block:'nearest',behavior:'instant'});}
function requestId(map,key){if(!map.has(key))map.set(key,crypto.randomUUID());return map.get(key);}
async function sendControl(body,resultId,submit){const token=$('token').value;if(!token){notice(resultId,'Enter the operator token in the top bar before requesting an intervention.',true);if(!document.querySelector('dialog[open]'))$('token').focus();return;}if(/[\r\n\x00]/.test(body.reason)){notice(resultId,'Use a single-line reason without control characters.',true);return;}const signature=JSON.stringify(body);body={...body,request_id:requestId(controlRequests,signature)};submit.disabled=true;notice(resultId,'Sending durable campaign-wide request…');try{const result=await api('/api/intervene',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+token},body:JSON.stringify(body)});notice(resultId,'Campaign-wide request recorded: '+result.request_id+' · awaiting executor acknowledgement; not yet applied');controlRequests.delete(signature);await refresh();if(timelineRun===body.run_id)scheduleTimeline();}catch(error){notice(resultId,errorMessage(error)+' Retry reuses request '+body.request_id+'.',true);}finally{submit.disabled=false;}}
$('intervention').addEventListener('submit',event=>{event.preventDefault();sendControl({action:$('action').value,run_id:$('run-id').value.trim(),reason:$('reason').value.trim()},'result',$('submit'));});
for(const [formId,inTimeline] of [['quick-control',false],['timeline-control',true]]){const prefix=inTimeline?'timeline':'quick';$(formId).addEventListener('submit',event=>{event.preventDefault();const target=inTimeline?timelineTarget:quickTarget;if(target)sendControl({action:target.action,run_id:target.runId,reason:$(prefix+'-reason').value.trim()},inTimeline?'timeline-control-result':'quick-result',$(prefix+'-submit'));});$(prefix+'-reason').addEventListener('input',()=>{const target=inTimeline?timelineTarget:quickTarget;if(target)drafts.set(target.runId+'|'+target.action,$(prefix+'-reason').value);});}
$('quick-dismiss').addEventListener('click',()=>{$('quick-control').hidden=true;});$('timeline-control-dismiss').addEventListener('click',()=>{$('timeline-control').hidden=true;});$('run-search').addEventListener('input',renderRuns);
let assetsLoading=false,assetsAgain=false;
async function refreshAssets(){if(assetsLoading){assetsAgain=true;return;}assetsLoading=true;try{assetData=await api('/api/assets');assetListSignature='';renderAssets();notice('assets-status','');}catch(error){notice('assets-status',errorMessage(error),true);}finally{assetsLoading=false;if(assetsAgain){assetsAgain=false;refreshAssets();}}}
function exportMessage(result){return 'Exported: '+result.export_path+'\nManifest: '+result.export_path+'/manifest.json\nSHA-256: '+result.manifest_sha256+'\nSplit guard: '+result.split_guard.result;}
function renderAssets(){
 const assets=list(assetData.assets).filter(asset=>assetFilter==='all'||asset.kind===assetFilter);
 $('asset-total').textContent=assetData.total??0;
 const signature=JSON.stringify([assetData,assetFilter,[...exportResults]]);
 if(signature===assetListSignature)return;
 assetListSignature=signature;
 $('asset-kinds').replaceChildren();
 for(const [kind,label] of [['all','All'],...Object.entries(kinds)]){
  const count=kind==='all'?assetData.total:assetData.by_kind?.[kind]||0;
  const filter=button(label+' · '+count,()=>{
   assetFilter=kind;assetListSignature='';renderAssets();
  });
  filter.setAttribute('aria-pressed',String(assetFilter===kind));
  $('asset-kinds').append(filter);
 }
 fillCards($('assets'),assets.slice().reverse(),asset=>{
  const el=node('article',undefined,'asset-row'),head=node('div',undefined,'card-head');
  head.append(node('strong',asset.asset_id,'asset-id mono'),badge(asset.split,tone(asset.split)));
  const chips=node('div',undefined,'chips');
  chips.append(badge(kinds[asset.kind]||asset.kind,'blue'),assetBadge(asset));
  addReplayBadge(chips,asset.source_kind);
  el.append(head,chips,
   node('p',(asset.dataset_id??'Unresolved dataset')+' / '+(asset.item_id??'unresolved item'),'mono'),
   node('p',asset.summary||asset.run_id));
  if(asset.blocked_reason)el.append(node('p',blockedReason(asset),'warning'));
  const actions=node('div',undefined,'actions');
  actions.append(button('Inspect',()=>openAsset(asset.asset_id)));
  if(asset.kind==='repaired_task'||asset.kind==='exploit_witness')
   actions.append(button('Grader diff',()=>openDiff({asset_id:asset.asset_id})));
  else actions.append(button('Timeline',()=>openTimeline(asset.run_id,asset)));
  const output=node('div',exportResults.get(asset.asset_id)||
   (asset.exported?'Previously exported: '+asset.exported.export_path:''),'asset-result');
  output.setAttribute('role','status');
  const exp=button(asset.exported?'Export again':'Export',
   ()=>exportAsset(asset,exp,output),'small primary');
  exp.disabled=!asset.exportable;
  exp.setAttribute('aria-label','Export asset '+asset.asset_id);
  if(!asset.exportable)exp.title=blockedReason(asset)||'Split guard blocks this asset';
  actions.append(exp);
  el.append(actions,output);
  return el;
 },'Verified assets will appear here as workers qualify them.');
}
async function exportAsset(asset,trigger,output){
 if(!$('token').value){
  output.textContent='Enter the operator token in the top bar before exporting.';
  if(!document.querySelector('dialog[open]'))$('token').focus();
  return;
 }
 trigger.disabled=true;
 output.textContent='Checking sealed-split lineage before export…';
 try{
  const result=await api('/api/assets/export',{
   method:'POST',
   headers:{'Content-Type':'application/json',Authorization:'Bearer '+$('token').value},
   body:JSON.stringify({
    asset_id:asset.asset_id,request_id:requestId(exportRequests,asset.asset_id)
   })
  });
  const message=exportMessage(result);
  exportResults.set(asset.asset_id,message);
  output.textContent=message;
  exportRequests.delete(asset.asset_id);
  refreshAssets();
 }catch(error){
  let message=errorMessage(error);
  if(error.status===409&&error.detail?.code==='split_leak'){
   message='Export blocked · split_leak. Nothing was exported.';
   // The HTTP boundary deliberately redacts sealed values. Only field + reason are public.
   for(const match of list(error.detail.matches))message+='\n'+match.field+' — '+match.reason;
  }
  exportResults.set(asset.asset_id,message);
  output.textContent=message;
 }finally{
  trigger.disabled=!asset.exportable;
 }
}
$('assets-refresh').addEventListener('click',refreshAssets);
let assetGeneration=0;
async function openAsset(assetId){
 const generation=++assetGeneration;
 $('asset-title').textContent=assetId;
 $('asset-meta').textContent='';
 $('asset-content').replaceChildren();
 notice('asset-status','Loading verified asset…');
 openDialog('asset-drawer');
 try{
  const asset=await api('/api/assets/detail?asset_id='+encodeURIComponent(assetId));
  if(generation!==assetGeneration)return;
  $('asset-meta').textContent=asset.kind+' · '+(asset.dataset_id??'Unresolved dataset')+
   ' / '+(asset.item_id??'unresolved item')+' · '+(asset.split??'unknown split');
  notice('asset-status',blockedReason(asset)||'Verified event '+asset.event_id,Boolean(asset.blocked_reason));
  const layout=node('div',undefined,'preview-layout');
  layout.append(node('pre',text(asset.preview)));
  const side=node('div');
  side.append(node('h3','Verification + provenance'),node('p',asset.summary),
   node('p','Run: '+asset.run_id,'mono'),node('p','Verified: '+asset.verified_at,'mono'),
   assetBadge(asset));
  addReplayBadge(side,asset.source_kind);
  const actions=node('div',undefined,'actions');
  actions.append(button('Open timeline',()=>openTimeline(asset.run_id,asset)));
  if(asset.kind!=='labeled_trace')
   actions.append(button('Grader diff',()=>openDiff({asset_id:asset.asset_id})));
  const output=node('div',exportResults.get(asset.asset_id)||
   (asset.exported?'Previously exported: '+asset.exported.export_path:''),'asset-result');
  output.setAttribute('role','status');
  const exp=button(asset.exported?'Export again':'Export',
   ()=>exportAsset(asset,exp,output),'small primary');
  exp.disabled=!asset.exportable;
  actions.append(exp);
  side.append(actions,output);
  layout.append(side);
  $('asset-content').append(layout);
 }catch(error){
  if(generation===assetGeneration)notice('asset-status',errorMessage(error),true);
 }
}
let timelineRun=null,timelineData=null,timelineGeneration=0,timelineTimer=null,timelineLoading=false,timelineAgain=false;
let timelineSelectors={};
let points=[],selectedSeq=null;
// Coalesce bursts without waiting for a quiet journal: a busy replay must stay live.
function scheduleTimeline(){
 if(timelineTimer!==null)return;
 timelineTimer=setTimeout(()=>{timelineTimer=null;loadTimeline();},150);
}
function clearTimeline(){
 timelineData=null;selectedSeq=null;points=[];
 for(const id of ['trace-steps','prefix-assessments','timeline-cards','timeline-gates',
  'timeline-controls','timeline-control-progress'])$(id).replaceChildren();
 const slider=$('prefix-scrubber');
 slider.disabled=true;slider.max='0';slider.value='0';
 $('prefix-position').textContent='';
 $('prefix-boundary').textContent='Only evidence at or before the selected journal sequence is shown.';
 $('timeline-meta').textContent='';
 $('timeline-state').textContent='';
 $('timeline-execution').replaceChildren();
 $('timeline-execution').hidden=true;
 $('timeline-diff').disabled=true;
}
function setTimelineProvenance(sourceKind,hasTrace=null){
 $('timeline-context').textContent=isReplay(sourceKind)?recordedContext:
  hasTrace===false?noTraceLabel:'Live trace / causal-prefix inspector';
 $('timeline-provenance').replaceChildren();
 addReplayBadge($('timeline-provenance'),sourceKind);
 if(isReplay(sourceKind)&&hasTrace===false)$('timeline-provenance').append(badge(noTraceLabel));
}
function renderTimelineExecution(){
 const parent=$('timeline-execution');
 const run=hasRunCatalog(currentState||{})?selectedRunItem(timelineRun,timelineSelectors):null;
 parent.replaceChildren();
 parent.hidden=!run;
 if(!run)return;
 parent.append(node('h3','Latest task execution · worker lifecycle'),
  node('small','Current journal summary; not rewound by the causal-prefix scrubber.'),
  node('p',run.dataset_id+' / '+run.item_id,'mono'),
  badge(run.run_state,tone(run.run_state)),executionDetails(run));
}
async function openTimeline(runId,item={}){
 const selectors=taskSelectors(item);
 if(timelineRun!==runId||JSON.stringify(timelineSelectors)!==JSON.stringify(selectors)){
  timelineRun=runId;timelineSelectors=selectors;timelineGeneration++;
  $('follow-live').checked=true;
  $('timeline-control').hidden=true;
  clearTimeline();
 }
 const run=selectedRunItem(runId,timelineSelectors);
 const sourceKind=timelineData?.source_kind??run?.source_kind;
 setTimelineProvenance(sourceKind,run?.has_trace);
 $('timeline-title').textContent=runId;
 $('timeline-actions').replaceChildren(runButtons(runId,true));
 renderTimelineExecution();
 openDialog('timeline-drawer');
 await loadTimeline();
}
async function loadTimeline(){
 if(!timelineRun||!$('timeline-drawer').open)return;
 if(timelineLoading){timelineAgain=true;return;}
 timelineLoading=true;
 const generation=timelineGeneration,runId=timelineRun,selectors={...timelineSelectors};
 if(!timelineData)notice('timeline-status','Loading durable run events…');
 try{
  const query=new URLSearchParams(selectors).toString();
  const data=await api('/api/runs/'+encodeURIComponent(runId)+'/timeline'+(query?'?'+query:''));
  if(generation!==timelineGeneration||runId!==timelineRun)return;
  timelineData=data;
  const resolved=taskSelectors(data);
  if(resolved.dataset_id)timelineSelectors=resolved;
  $('timeline-diff').disabled=false;
  // Each journal boundary is a causal snapshot, including assessments between steps.
  const sequences=new Set([0]);
  for(const field of ['steps','prefixes','cards','gates','controls'])
   for(const item of list(data[field]))if(Number.isFinite(item.seq))sequences.add(item.seq);
  if(Number.isFinite(data.last_seq))sequences.add(data.last_seq);
  points=[...sequences].sort((a,b)=>a-b);
  if($('follow-live').checked||selectedSeq==null)selectedSeq=points.at(-1);
  else selectedSeq=points.filter(seq=>seq<=selectedSeq).at(-1)??0;
  const slider=$('prefix-scrubber');
  slider.max=String(points.length-1);
  slider.value=String(Math.max(0,points.indexOf(selectedSeq)));
  slider.disabled=points.length<2;
  $('timeline-meta').textContent=(data.dataset_id??'Unattributed')+' / '+(data.item_id??'—')+
   ' · '+(data.split??'unknown split')+' · latest seq '+data.last_seq;
  setTimelineProvenance(data.source_kind,list(data.steps).length>0);
  $('timeline-state').textContent=(isReplay(data.source_kind)?'Replay: ':'Live: ')+data.run_state;
  $('timeline-state').className='badge '+tone(data.run_state);
  notice('timeline-status',[
   !list(data.steps).length?noTraceLabel+'. Task-scoped grader comparison is available when recorded.':'',
   data.truncated?'Bounded view: earlier run events or steps have been truncated.':''
  ].filter(Boolean).join(' '));
  renderTimelineExecution();
  renderTimeline();
  renderControls();
 }catch(error){
  if(generation===timelineGeneration){
   if(error.status===403||error.detail?.code==='ambiguous_run')clearTimeline();
   notice('timeline-status',errorMessage(error),true);
  }
 }finally{
  timelineLoading=false;
  if(timelineAgain){timelineAgain=false;loadTimeline();}
 }
}
function visible(field){return list(timelineData?.[field]).filter(item=>Number(item.seq)<=selectedSeq);}
function prefixLabel(){
 return visible('steps').at(-1)?.step_id??visible('prefixes').at(-1)?.prefix_step??'start';
}
function renderTimeline(){
 if(!timelineData)return;
 const steps=visible('steps'),label=prefixLabel(),hasSteps=list(timelineData.steps).length>0;
 $('prefix-position').textContent=(hasSteps?'Prefix '+label+' · ':'')+'journal ≤ '+selectedSeq+
  ' · snapshot '+(Number($('prefix-scrubber').value)+1)+' / '+points.length;
 $('prefix-scrubber').setAttribute('aria-valuetext',
  (hasSteps?'Prefix '+label+', ':'')+'journal sequence '+selectedSeq);
 $('prefix-boundary').textContent=hasSteps?
  steps.length+' / '+list(timelineData.steps).length+' steps visible. Later evidence is hidden.':
  noTraceLabel+'. Later evidence is hidden.';
 fillCards($('trace-steps'),steps,step=>{
  const el=node('article',undefined,'step');
  el.dataset.step=String(step.step_id);
  el.tabIndex=-1;
  const head=node('div',undefined,'step-header');
  head.append(node('strong','Step '+step.step_id+' · '+(step.source||step.system)),
   node('small','seq '+step.seq));
  const body=node('div',undefined,'step-content');
  if(step.reasoning)body.append(node('small','Reasoning'),node('p',step.reasoning));
  if(step.message)body.append(node('small','Message'),node('p',step.message));
  for(const tool of list(step.tool_calls)){
   const detail=node('details',undefined,'tool-call');
   detail.open=true;
   detail.append(node('summary',tool.function_name+' · '+(tool.tool_call_id||'tool call')),
    node('pre',tool.arguments));
   body.append(detail);
  }
  if(step.observation)body.append(node('small','Observation'),node('pre',step.observation));
  el.append(head,body);
  return el;
 },list(timelineData.steps).length?'No trace steps at this prefix.':noTraceLabel+'.');
 fillCards($('prefix-assessments'),visible('prefixes').slice().reverse(),prefix=>{
  const card=basicCard('Prefix '+prefix.prefix_step+' · '+prefix.event_type,
   prefix.assessment??'No assessment text',prefix.status);
  const chips=node('div',undefined,'chips');
  chips.append(badge(prefix.status,tone(prefix.status)));
  if(prefix.score!=null)chips.append(node('span','Score '+prefix.score,'prefix-score'));
  for(const tripwire of list(prefix.tripwires))chips.append(badge(tripwire,'warn'));
  card.append(chips,node('small','seq '+prefix.seq));
  return card;
 },'No assessments at this prefix.');
 fillCards($('timeline-cards'),visible('cards').slice().reverse(),finding=>{
  const card=basicCard(finding.summary,
   finding.severity+' · '+finding.status+' · prefix '+(finding.prefix_step??'—'),finding.status);
  const citations=node('div',undefined,'citation-list');
  for(const stepId of list(finding.step_citations))
   citations.append(button('Step '+stepId,()=>jumpToStep(stepId),'citation'));
  card.append(citations,node('small','seq '+finding.seq));
  return card;
 },'No findings at this prefix.');
 fillCards($('timeline-gates'),visible('gates').slice().reverse(),gate=>
  basicCard('Step '+gate.step_id+' · '+text(gate.decision),
   gate.status+' · seq '+gate.seq,gate.status),'No gate decisions at this prefix.');
 fillCards($('timeline-controls'),visible('controls').slice().reverse(),controlCard,
  'No controls at this prefix.');
}
function scrub(index){const slider=$('prefix-scrubber');index=Math.max(0,Math.min(points.length-1,index));slider.value=String(index);selectedSeq=points[index]??0;$('follow-live').checked=index===points.length-1;renderTimeline();}
$('prefix-scrubber').addEventListener('input',()=>scrub(Number($('prefix-scrubber').value)));
$('prefix-scrubber').addEventListener('keydown',event=>{if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();scrub(Number($('prefix-scrubber').value)+(event.key==='ArrowRight'?1:-1));}});
$('follow-live').addEventListener('change',()=>{if($('follow-live').checked&&points.length)scrub(points.length-1);});
function jumpToStep(stepId){const step=list(timelineData?.steps).find(item=>String(item.step_id)===String(stepId));if(!step)return;if(step.seq>selectedSeq)scrub(Math.max(0,points.findIndex(seq=>seq>=step.seq)));for(const el of $('trace-steps').children)if(el.dataset.step===String(stepId)){el.scrollIntoView({block:'center',behavior:'instant'});el.focus({preventScroll:true});el.classList.add('focus-step');setTimeout(()=>el.classList.remove('focus-step'),1600);break;}}
$('timeline-diff').addEventListener('click',()=>{if(timelineRun)openDiff({run_id:timelineRun,...timelineSelectors});});
let diffGeneration=0;
function rewardBox(label,data,repaired=false){const box=node('div',undefined,'reward-box'+(repaired?' repaired':''));box.append(node('div',label,'eyebrow'),node('div',data.reward==null?'—':Number(data.reward).toFixed(1),'reward-value'),node('p',data.task_package),node('p','Digest: '+(data.digest??'unavailable')));if(data.trial_dir)box.append(node('p','Trial: '+data.trial_dir));return box;}
async function openDiff(query){
 const generation=++diffGeneration;
 $('diff-content').replaceChildren();
 $('diff-meta').textContent='';
 notice('diff-status','Loading verified grader comparison…');
 openDialog('diff-drawer');
 try{
  const selection=query.asset_id?{asset_id:query.asset_id}:
   {run_id:query.run_id,...taskSelectors(query)};
  const data=await api('/api/grader-diff?'+new URLSearchParams(selection));
  if(generation!==diffGeneration)return;
  notice('diff-status','');
  $('diff-meta').textContent=data.dataset_id+' / '+data.item_id+' · '+data.split+' · campaign '+data.run_id;
  renderDiff(data);
 }catch(error){
  if(generation===diffGeneration)notice('diff-status',errorMessage(error),true);
 }
}
function diffRows(file){
 const rows=[],lines=typeof file.unified_diff==='string'?file.unified_diff.split('\n'):[];
 let oldNo=0,newNo=0,hasHunk=false,removed=[],added=[];
 // Align adjacent removal/addition blocks while retaining each side's line numbers.
 const flush=()=>{
  for(let i=0;i<Math.max(removed.length,added.length);i++)
   rows.push({left:removed[i]??null,right:added[i]??null});
  removed=[];added=[];
 };
 for(const line of lines){
  const hunk=/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
  if(hunk){
   flush();oldNo=Number(hunk[1]);newNo=Number(hunk[2]);hasHunk=true;
   rows.push({hunk:line});
   continue;
  }
  if(!hasHunk)continue;
  if(line.startsWith('-'))removed.push({number:oldNo++,value:line.slice(1),kind:'removed'});
  else if(line.startsWith('+'))added.push({number:newNo++,value:line.slice(1),kind:'added'});
  else if(line.startsWith(' ')){
   flush();
   rows.push({left:{number:oldNo++,value:line.slice(1)},right:{number:newNo++,value:line.slice(1)}});
  }
 }
 flush();
 if(hasHunk)return rows;
 const original=file.original==null?[]:String(file.original).split('\n');
 const repaired=file.repaired==null?[]:String(file.repaired).split('\n');
 for(let i=0;i<Math.max(original.length,repaired.length);i++){
  const equal=original[i]===repaired[i];
  rows.push({
   left:i<original.length?{number:i+1,value:original[i],kind:equal?'':'removed'}:null,
   right:i<repaired.length?{number:i+1,value:repaired[i],kind:equal?'':'added'}:null
  });
 }
 return rows;
}
function renderFile(file,parent){parent.replaceChildren();const title=node('div',undefined,'diff-title');title.append(node('strong',file.path),node('small',' · '+file.status+(file.truncated?' · bounded preview / truncated':'')));parent.append(title);if(file.binary){parent.append(node('p','Binary file: text comparison is unavailable.','diff-note'));return;}const wrap=node('div',undefined,'diff-scroll');wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label','Side-by-side diff for '+file.path);const table=node('table',undefined,'diff-table');const columns=node('colgroup');for(const width of ['38px','auto','38px','auto']){const col=node('col');col.style.width=width;columns.append(col);}const head=node('thead'),headRow=node('tr');for(const label of ['Original','Repaired']){const cell=node('th',label);cell.colSpan=2;cell.scope='col';headRow.append(cell);}head.append(headRow);const body=node('tbody');for(const row of diffRows(file)){const tr=node('tr');if(row.hunk){const cell=node('td',row.hunk,'hunk');cell.colSpan=4;tr.append(cell);}else for(const [i,side] of [row.left,row.right].entries()){const number=node('td',side?.number??'','line-number '+(side?.kind||'absent')+(i?' right':''));number.setAttribute('aria-hidden','true');const value=node('td',side?.value??'','code-line '+(side?.kind||(!side?'absent':'')));tr.append(number,value);}body.append(tr);}table.append(columns,head,body);wrap.append(table);parent.append(wrap);if(!body.children.length)parent.append(node('p','Empty text file.','diff-note'));}
function renderDiff(data){
 const parent=$('diff-content'),rewards=node('div',undefined,'reward-header');
 rewards.append(rewardBox('Original reward',data.original),node('div','→','reward-arrow'),
  rewardBox('Repaired reward',data.repaired,true));
 parent.append(rewards,node('h3','Qualification controls · 3 × 3'));
 if(data.control_rewards){
  const grid=node('div',undefined,'control-rewards');
  for(const kind of ['reference','nop','exploit']){
   grid.append(node('div',kind,'reward-label'));
   for(let i=0;i<3;i++){
    const value=list(data.control_rewards[kind])[i]??'—';
    const cell=node('div',value,'reward-cell');
    cell.setAttribute('aria-label',kind+' trial '+(i+1)+' reward '+value);
    grid.append(cell);
   }
  }
  parent.append(grid);
 }else parent.append(node('p','No qualification control matrix was recorded.'));
 const files=list(data.files),layout=node('div',undefined,'diff-layout');
 const fileList=node('div',undefined,'diff-files'),view=node('div',undefined,'diff-view');
 fileList.setAttribute('role','tablist');
 fileList.setAttribute('aria-label','Task package files');
 fileList.setAttribute('aria-orientation','vertical');
 view.id='grader-file-panel';
 view.setAttribute('role','tabpanel');
 const select=(file,trigger)=>{
  for(const el of fileList.children){
   el.setAttribute('aria-selected',String(el===trigger));
   el.tabIndex=el===trigger?0:-1;
  }
  view.setAttribute('aria-labelledby',trigger.id);
  renderFile(file,view);
 };
 for(const [index,file] of files.entries()){
  const item=button('',()=>select(file,item),'diff-file');
  item.id='grader-file-tab-'+index;
  item.setAttribute('role','tab');
  item.setAttribute('aria-controls',view.id);
  item.append(node('span',file.path),
   badge(file.status,file.status==='added'?'good':file.status==='removed'?'bad':''));
  item.addEventListener('keydown',event=>{
   if(!['ArrowDown','ArrowUp','ArrowRight','ArrowLeft','Home','End'].includes(event.key))return;
   event.preventDefault();
   const items=[...fileList.children];
   let next=items.indexOf(item);
   if(event.key==='Home')next=0;
   else if(event.key==='End')next=items.length-1;
   else next=(next+(['ArrowDown','ArrowRight'].includes(event.key)?1:-1)+items.length)%items.length;
   items[next].click();
   items[next].focus();
  });
  fileList.append(item);
 }
 layout.append(fileList,view);
 parent.append(node('h3','Task package diff · '+
  files.filter(file=>file.status!=='unchanged').length+' changed files'),layout);
 if(files.length){
  const index=Math.max(0,files.findIndex(file=>file.status!=='unchanged'));
  select(files[index],fileList.children[index]);
 }else view.append(node('p','No comparable task files were recorded.','diff-note'));
 if(data.exploit){
  const exploit=node('div',undefined,'exploit-panel');
  exploit.append(node('h3','Exploit mechanism'),node('p',data.exploit.mechanism??'Not recorded'));
  if(data.exploit.taxonomy_class)exploit.append(badge(data.exploit.taxonomy_class,'warn'));
  for(const [path,content] of Object.entries(data.exploit.files||{})){
   const detail=node('details');
   detail.append(node('summary',path),node('pre',content));
   exploit.append(detail);
  }
  parent.append(exploit);
 }
}
let refreshing=false,again=false,stateTimer=null;
async function refresh(){if(refreshing){again=true;return;}refreshing=true;try{render(await api('/api/state'));}catch(error){$('connection').textContent=errorMessage(error);$('connection').className='disconnected';}finally{refreshing=false;if(again){again=false;refresh();}}}
function scheduleState(){
 if(stateTimer!==null)return;
 stateTimer=setTimeout(()=>{stateTimer=null;refresh();},120);
}
const stream=new EventSource('/api/stream');
stream.addEventListener('state',event=>{try{render(JSON.parse(event.data));if(timelineRun)scheduleTimeline();}catch{notice('assets-status','Unreadable journal snapshot. Reconnecting may be required.',true);}});
stream.onmessage=event=>{
 try{
  const envelope=JSON.parse(event.data);
  scheduleState();
  if(envelope.run_id===timelineRun)scheduleTimeline();
  if(['asset.verified','verified_asset.created','asset.promoted','asset.exported'].includes(envelope.event_type))
   refreshAssets();
 }catch{
  $('connection').textContent='Unreadable journal event';
  $('connection').className='disconnected';
 }
};
stream.onopen=()=>{$('connection').textContent='Live · connected';$('connection').className='status';};
stream.onerror=()=>{$('connection').textContent='Reconnecting · journal replay enabled';$('connection').className='disconnected';};
window.addEventListener('pagehide',()=>{stream.close();clearTimeout(stateTimer);clearTimeout(timelineTimer);});
refresh();
</script>
</body>
</html>"""
