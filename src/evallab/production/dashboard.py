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
.playbook-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin:12px 0}.playbook-card{border:1px solid var(--line);border-radius:7px;padding:14px;background:var(--raised);min-width:0}.playbook-card h4{margin:0 0 6px}.playbook-card small{display:block;margin:4px 0;color:var(--muted)}.playbook-card ul{padding-left:18px;color:var(--muted);font-size:11px}.playbook-card summary{cursor:pointer;color:var(--blue)}
.atlas-health{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.atlas-layout{display:grid;grid-template-columns:minmax(250px,310px) minmax(0,1fr);gap:18px;margin-top:20px}.atlas-layout>div{min-width:0}.atlas-tree{max-height:720px;overflow:auto;padding:3px}.atlas-tree ul{list-style:none;margin:0;padding:0}.atlas-tree ul ul{padding-left:14px;border-left:1px solid var(--line);margin-left:5px}.atlas-cluster{display:block;width:100%;text-align:left;margin:0 0 7px;padding:10px;font-weight:400;overflow-wrap:anywhere}.atlas-cluster strong,.atlas-cluster small{display:block}.atlas-cluster[aria-pressed=true]{border-color:var(--accent);background:#143129}.atlas-cluster[aria-pressed=true] strong{color:var(--accent)}.atlas-cluster small{margin-top:4px}.atlas-cluster .atlas-centroid{color:var(--blue)}
.atlas-runs .cloud-table{table-layout:fixed;min-width:1440px}.atlas-runs .cloud-table th:nth-child(1){width:65px}.atlas-runs .cloud-table th:nth-child(2){width:145px}.atlas-runs .cloud-table th:nth-child(4),.atlas-runs .cloud-table th:nth-child(5),.atlas-runs .cloud-table th:nth-child(6){width:170px}.atlas-run-row{cursor:pointer}.atlas-run-row:hover{background:var(--raised)}.atlas-run-row:focus-within{background:#143129}.atlas-runs .chips{gap:3px}.atlas-runs .badge{padding:2px 4px;font-weight:400}.atlas-runs .cloud-table-wrap{max-height:720px}.atlas-runs .cloud-table th{position:sticky;top:0;z-index:1}.atlas-pagination{flex-wrap:wrap}
.atlas-detail{margin-top:20px;border-top:1px solid var(--line);padding-top:18px;scroll-margin-top:18px;min-width:0}.atlas-detail:focus{outline:2px solid var(--accent);outline-offset:8px}.atlas-detail h3{margin:16px 0 10px}.atlas-facet{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px 18px}.atlas-facet div{min-width:0}.atlas-facet dt{color:var(--muted);font-size:11px}.atlas-facet dd{margin:3px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}.atlas-step{padding:12px;border:1px solid var(--line);border-radius:7px;scroll-margin-top:18px;min-width:0}.atlas-step h4{margin-bottom:8px}.atlas-step.evidence{border-left:3px solid var(--accent);background:#10251f}.atlas-step.neighbour{color:var(--muted);background:#0e1723}.atlas-step.neighbour pre{color:var(--muted)}.atlas-step:focus{outline:2px solid var(--accent);outline-offset:3px}.atlas-detail pre{max-width:100%;white-space:pre-wrap;overflow-wrap:anywhere}.atlas-flags .actions{margin-top:7px}.atlas-detail .playbook-grid{grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr))}
.atlas-detail .panel-head>div:first-child{min-width:0}.atlas-detail h2{overflow-wrap:anywhere}
@media(max-width:1000px){.atlas-layout{grid-template-columns:1fr}.atlas-tree{max-height:370px}.atlas-detail .panel-head{flex-wrap:wrap}}
.atlas-tools{margin:12px 0;padding:10px 12px;border:1px solid var(--line);border-radius:7px;background:var(--raised)}.atlas-tools summary{cursor:pointer;color:var(--blue);font-weight:600}.atlas-tools .cloud-table{min-width:760px}.atlas-tools .row{margin-top:8px}
.tools-cohorts{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.tools-table .cloud-table{min-width:1180px}.tools-table>.cloud-table-wrap>.cloud-table>tbody>tr>td:first-child{min-width:280px}.tools-table td>div{display:grid;gap:3px;min-width:0}.tools-table td small{color:var(--muted);overflow-wrap:anywhere}.tools-table details{margin-top:8px}.tools-table summary{cursor:pointer;color:var(--blue)}.tools-table details h4{margin:10px 0 0}.tools-table details .cloud-table{min-width:0}.tools-sources summary{cursor:pointer;color:var(--blue)}
.task-section{margin-top:22px}.task-section h3{margin:0 0 8px}.task-section>p{margin:0 0 8px}.task-command{white-space:pre-wrap;overflow-wrap:anywhere;margin:6px 0;padding:10px;border:1px solid var(--line);border-radius:7px;background:var(--raised);font:12px var(--mono)}.task-command-row{margin:12px 0}.task-lines{display:grid;gap:3px;min-width:0}.task-lines small{color:var(--muted);overflow-wrap:anywhere}.task-runs .cloud-table{min-width:1280px}.task-qa .cloud-table,.task-seeker .cloud-table,.task-fixes .cloud-table{min-width:900px}
.reliability-bar{display:block;height:6px;border-radius:3px;background:var(--blue);min-width:2px}.reliability-track{display:block;width:140px;height:6px;border-radius:3px;background:var(--line)}.atlas-tools .cloud-table.reliability-table{min-width:560px}
.repairs-table .cloud-table{min-width:1100px}.repairs-table td>div{display:grid;gap:3px;min-width:0}.repairs-table td small{color:var(--muted);overflow-wrap:anywhere}.repairs-table td a{color:var(--blue);overflow-wrap:anywhere}
.gate-table .cloud-table{min-width:1100px}.gate-table td>div{display:grid;gap:3px;min-width:0}.gate-table td small{color:var(--muted);overflow-wrap:anywhere}.gate-bar{display:block;height:6px;border-radius:3px;background:var(--line);width:140px}.gate-fill{display:block;height:6px;border-radius:3px;background:var(--blue);min-width:2px}
.harness-table .cloud-table{min-width:1200px}.harness-table td>div{display:grid;gap:3px;min-width:0}.harness-table td small{color:var(--muted);overflow-wrap:anywhere}.harness-card h3{margin:18px 0 8px}.harness-card .cloud-table{min-width:1000px}.harness-form{margin:12px 0}
.scope-box{margin:16px 0}.scope-box h3{margin:0 0 6px}.scope-lines{margin:0 0 10px;padding-left:18px;color:var(--muted)}.scope-lines li{margin:2px 0}.scope-box .cloud-table{min-width:1100px}
.crosstest-table .cloud-table{min-width:1320px}.crosstest-table td>div{display:grid;gap:3px;min-width:0}.crosstest-table td small{color:var(--muted);overflow-wrap:anywhere}.crosstest-table td a{color:var(--blue);overflow-wrap:anywhere}.crosstest-table details .cloud-table{min-width:0}.crosstest-table summary{cursor:pointer;color:var(--blue)}.crosstest-hold{margin-top:22px}
@media(max-width:1200px){main{padding:20px}.workspace{grid-template-columns:minmax(0,1.2fr) minmax(345px,1fr)}.top-meta{gap:14px}.token-field{width:230px}.panel{padding:15px}.timeline-grid{grid-template-columns:minmax(0,1.4fr) minmax(270px,1fr)}.run-main{flex-wrap:wrap}.diff-layout{grid-template-columns:195px minmax(0,1fr)}}
@media(max-width:900px){.topbar{flex-wrap:wrap}.top-meta{width:100%;justify-content:space-between}.workspace,.timeline-grid,.preview-layout{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.system .cards{max-height:130px}.system-traces{display:none}.workspace section{margin-top:14px}.metrics{gap:10px}.metric strong{font-size:24px}.diff-layout{grid-template-columns:1fr}.diff-files{position:static;display:flex;flex-wrap:wrap}.diff-file{max-width:100%}.dialog-header{padding:14px}.dialog-body{padding:14px}.scrubber{padding:12px 14px}.reward-header{gap:8px}.reward-box{padding:10px}.reward-value{font-size:25px}}
@media(prefers-reduced-motion:no-preference){button{transition:background .12s,border-color .12s}.focus-step{animation:step-focus 1.5s ease-out}@keyframes step-focus{from{box-shadow:0 0 0 5px #6ce4b844}to{box-shadow:0 0 0 0 transparent}}}
</style>
</head>
<body data-home="live">
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
 <button id="atlas-tab" type="button" role="tab" aria-selected="false" aria-controls="atlas-view" tabindex="-1">Run Atlas</button>
 <button id="tools-tab" type="button" role="tab" aria-selected="false" aria-controls="tools-view" tabindex="-1">Trace Tools</button>
 <button id="harness-tab" type="button" role="tab" aria-selected="false" aria-controls="harness-view" tabindex="-1">Harness</button>
 <button id="task-tab" type="button" role="tab" aria-selected="false" aria-controls="task-view" tabindex="-1">Task Evidence</button>
 <button id="repairs-tab" type="button" role="tab" aria-selected="false" aria-controls="repairs-view" tabindex="-1">Grader repairs</button>
 <button id="crosstest-tab" type="button" role="tab" aria-selected="false" aria-controls="crosstest-view" tabindex="-1">Cross-test sets</button>
 <button id="trust-tab" type="button" role="tab" aria-selected="false" aria-controls="trust-view" tabindex="-1">Trust &amp; QC</button>
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
<section id="atlas-view" class="panel" role="tabpanel" aria-labelledby="atlas-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Blinded run facets / admitted DEV evidence</div><h2>Run Atlas</h2></div><button id="cloud-atlas-refresh" type="button" class="small">Refresh atlas</button></div>
 <p class="seal">Facets are model-read summaries of blinded runs: leads, not verdicts. DEV only. Label rates are recorded at scoring time.</p>
 <p id="cloud-atlas-status" class="notice" role="status"></p>
 <div id="cloud-atlas-health"></div>
 <form id="cloud-atlas-filters" class="cloud-filters">
  <div><label for="atlas-source">Source</label><select id="atlas-source"><option value="">Any source</option></select></div>
  <div><label for="atlas-outcome">Outcome</label><select id="atlas-outcome"><option value="">Any outcome</option></select></div>
  <div><label for="atlas-tests">Tests</label><select id="atlas-tests"><option value="">Any tests</option></select></div>
  <div><label for="atlas-eval_awareness">Eval awareness</label><select id="atlas-eval_awareness"><option value="">Any eval awareness</option></select></div>
  <div><label for="atlas-facet_model">Reader (facet model)</label><select id="atlas-facet_model"><option value="">Any reader</option></select></div>
  <div><label for="atlas-alarmed_by">Detector alarm</label><select id="atlas-alarmed_by"><option value="">Any detector alarm</option></select></div>
  <div><label for="atlas-results">Detector results</label><select id="atlas-results"><option value="">Any detector results</option></select></div>
  <div><label for="atlas-min_suspicion">Min suspicion · counts at exact value</label><select id="atlas-min_suspicion"><option value="">Any suspicion</option></select></div>
  <div><label for="atlas-grader_or_harness_touched">Grader / harness touched</label><select id="atlas-grader_or_harness_touched"><option value="">Any</option></select></div>
  <div><label for="atlas-answer_material_accessed">Answer material accessed</label><select id="atlas-answer_material_accessed"><option value="">Any</option></select></div>
  <div><label for="atlas-success_claim_without_evidence">Success claim without evidence</label><select id="atlas-success_claim_without_evidence"><option value="">Any</option></select></div>
  <div><label for="atlas-limit">Page size</label><select id="atlas-limit"><option value="25">25</option><option value="50">50</option><option value="100">100</option><option value="200">200</option></select></div>
  <div><label for="atlas-sort">Order</label><select id="atlas-sort"><option value="suspicion">Most suspicious first</option><option value="alarms">Most detector alarms first</option></select></div>
  <div class="cloud-query"><label for="atlas-query">Search run ID, task, goal, approach or shortcut</label><input id="atlas-query" type="search" autocomplete="off" placeholder="Search blinded facets…"></div>
  <div class="actions"><button type="submit" class="primary">Search DEV facets</button><button id="atlas-reset" type="button">Reset filters and cluster</button><small>Option counts describe all admitted facets, not the current filter. Boolean counts are supplied for Yes only.</small></div>
 </form>
 <div class="atlas-layout">
  <div><h3>Cluster tree</h3><p>Recorded size and label rates stay fixed; matching counts follow these filters. Select a cluster again to clear it.</p><div id="cloud-atlas-clusters" class="atlas-tree"></div></div>
  <div class="atlas-runs"><div class="row atlas-pagination"><h3>Runs</h3><div class="actions"><button id="atlas-copy-ids" type="button" class="small" disabled>Copy run IDs · this page</button><button id="atlas-copy-link" type="button" class="small">Copy link to this view</button><button id="atlas-prev" type="button" class="small" disabled>Prev</button><span id="atlas-page" class="mono"></span><button id="atlas-next" type="button" class="small" disabled>Next</button></div></div><div id="cloud-atlas-runs"></div></div>
 </div>
 <section id="atlas-run-panel" class="atlas-detail" tabindex="-1" aria-labelledby="atlas-run-title" hidden>
  <div class="panel-head"><div><div class="eyebrow">Facet / bounded evidence / routing leads</div><h2 id="atlas-run-title">Run detail</h2></div><div class="actions"><button id="atlas-run-refresh" type="button" class="small">Refresh run</button><button id="atlas-run-close" type="button" class="small">Close · Esc</button></div></div>
  <p class="seal">Trace flags and playbook cards are routing leads, not verdicts. No wrong submission or grader weakness is established by a flag alone.</p>
  <p id="atlas-run-status" class="notice" role="status"></p><div id="atlas-run-content"></div>
 </section>
 <details id="atlas-tools" class="atlas-tools" open>
  <summary>Which tool to trust · recall and false alarms on labelled cohorts</summary>
  <p id="atlas-tools-status" class="notice" role="status"></p>
  <div id="atlas-tools-table"></div>
  <div class="row"><button id="atlas-tools-open" type="button" class="small">Open Trace Tools for cost, detectors and sources</button></div>
 </details>
<details id="atlas-readers" class="atlas-tools" open>
 <summary>Reader mix · which model read each population's stored facets</summary>
 <p id="atlas-readers-status" class="notice" role="status"></p>
 <div id="atlas-readers-body"></div>
</details>
<details id="atlas-coverage" class="atlas-tools">
 <summary>Coverage · stored facets versus admitted runs, and what is not yet ready</summary>
 <p id="atlas-coverage-status" class="notice" role="status"></p>
 <div id="atlas-coverage-body"></div>
</details>
<details id="atlas-reliability" class="atlas-tools">
 <summary>Field reliability · how often a blind re-rating agreed with each facet field</summary>
 <p id="atlas-reliability-status" class="notice" role="status"></p>
 <div id="atlas-reliability-body"></div>
</details>
</section>
<section id="tools-view" class="panel" role="tabpanel" aria-labelledby="tools-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Whole-run tools / rule cascade / blinded facets</div><h2>Trace Tools</h2></div><button id="cloud-tools-refresh" type="button" class="small">Refresh scorecard</button></div>
 <p class="seal">Retained DEV and cross-test measurements, not a ranking. CheatBench DEV was already examined and is not held out. A tool that did not read a run abstained: that run is neither a miss nor a clean pass.</p>
 <p id="cloud-tools-status" class="notice" role="status"></p>
 <div id="cloud-tools-cohorts" class="tools-cohorts"></div>
<section id="cloud-tools-scope" class="scope-box" aria-labelledby="cloud-tools-scope-title">
 <h3 id="cloud-tools-scope-title">Where each detector works</h3>
 <p id="cloud-tools-scope-status" class="notice" role="status"></p>
 <ul id="cloud-tools-scope-lines" class="scope-lines"></ul>
 <div id="cloud-tools-scope-table" class="tools-table"></div>
</section>
 <div id="cloud-tools-table" class="tools-table"></div>
<section id="tools-runs-panel" class="atlas-detail" tabindex="-1" aria-labelledby="tools-runs-title" hidden>
 <div class="panel-head"><div><div class="eyebrow">Admitted DEV runs by trace id / no labels</div><h2 id="tools-runs-title">Tool runs</h2></div><div class="actions"><button id="tools-runs-copy" type="button" class="small">Copy run IDs · this page</button><button id="tools-runs-close" type="button" class="small">Close</button></div></div>
 <div id="tools-runs-kinds" class="chips"></div>
 <p id="tools-runs-status" class="notice" role="status"></p>
 <div id="tools-runs-body"></div>
 <div class="row"><button id="tools-runs-prev" type="button" class="small">Previous</button> <span id="tools-runs-page"></span> <button id="tools-runs-next" type="button" class="small">Next</button></div>
</section>
 <details id="cloud-tools-sources" class="tools-sources"><summary>Sources read</summary><div id="cloud-tools-source-list"></div></details>
</section>
<section id="harness-view" class="panel" role="tabpanel" aria-labelledby="harness-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Every trace detector merged per run / retained files only, DEV runs</div><h2>Harness</h2></div><button id="harness-refresh" type="button" class="small">Refresh harness</button></div>
 <p class="seal">Read-only. A card lists every detector's newest result for one run, including run health, and merges nothing into a verdict. A detector that recorded nothing is not a clean pass, and a run with no actor transcript is unknown, never clean. Scorecard numbers are counts on the named corpus with the reader model stated.</p>
 <p id="harness-status" class="notice" role="status"></p>
 <div id="harness-summary"></div>
 <h3>Combined scorecard</h3>
 <div id="harness-scorecard" class="harness-table"></div>
 <h3>Detector coverage on DEV runs</h3>
 <div id="harness-detectors" class="harness-table"></div>
 <h3>Runs with an alarm</h3>
 <div id="harness-flagged" class="harness-table"></div>
 <form id="harness-form" class="cloud-filters harness-form">
  <div class="cloud-query"><label for="harness-run">Run id</label><input id="harness-run" type="search" autocomplete="off" maxlength="200" placeholder="e.g. retained-trace:…"></div>
  <button type="submit">Open evidence card</button>
 </form>
 <p id="harness-card-status" class="notice" role="status"></p>
 <div id="harness-card" class="harness-card" tabindex="-1"></div>
</section>
<section id="task-view" class="panel" role="tabpanel" aria-labelledby="task-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">One admitted DEV task / runs, tool findings, Task QA, seeker differentials, grader fixes</div><h2>Task Evidence</h2></div><button id="task-refresh" type="button" class="small">Refresh task</button></div>
 <p class="seal">Retained records for one task, not a verdict. A tool that did not read a run abstained, which is neither a miss nor a clean pass; no finding recorded means none recorded.</p>
<details id="task-gate" class="atlas-tools" open>
 <summary>Gate rerun progress · the corrected Task QA gate on the DEV tasks, per model and arm</summary>
 <p id="task-gate-status" class="notice" role="status"></p>
 <div id="task-gate-body" class="gate-table"></div>
</details>
 <form id="task-form" class="cloud-filters">
  <div class="cloud-query"><label for="task-query">Task id (two or more characters)</label><input id="task-query" type="search" autocomplete="off" maxlength="300" placeholder="e.g. terminal-bench/vf2-speedup-networkx"></div>
  <div class="actions"><button type="submit" class="primary">Find task</button><button id="task-copy-link" type="button">Copy link to this task</button></div>
 </form>
 <p id="task-status" class="notice" role="status"></p>
 <div id="task-matches"></div>
 <div id="task-content"></div>
</section>
<section id="repairs-view" class="panel" role="tabpanel" aria-labelledby="repairs-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Keyed DEV tasks / counts and PR numbers only</div><h2>Grader repairs</h2></div><button id="repairs-refresh" type="button" class="small">Refresh board</button></div>
 <p class="seal">Read-only. “Static only” means a candidate passed host checks and no native sandbox run happened. Nothing is called repaired without a native receipt.</p>
 <p id="repairs-status" class="notice" role="status"></p>
 <div id="repairs-summary" class="tools-cohorts"></div>
 <form id="repairs-filters" class="cloud-filters">
  <div><label for="repairs-status-filter">Status</label><select id="repairs-status-filter"><option value="">Any status</option></select></div>
  <div><label for="repairs-defect-filter">Defect type</label><select id="repairs-defect-filter"><option value="">Any defect type</option></select></div>
  <div><label for="repairs-task-filter">Task</label><input id="repairs-task-filter" type="search" maxlength="100" autocomplete="off" placeholder="Part of a task id"></div>
 </form>
 <div id="repairs-table" class="repairs-table"></div>
</section>
<section id="crosstest-view" class="panel" role="tabpanel" aria-labelledby="crosstest-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Staged cross-test sets / registry metadata only</div><h2>Cross-test sets</h2></div><button id="crosstest-refresh" type="button" class="small">Refresh registry</button></div>
 <p class="seal">Read-only. Frozen, one-shot, local evaluation sets: none is uploadable, none may be trained on, and no record text is shown here.</p>
 <p id="crosstest-status" class="notice" role="status"></p>
 <div id="crosstest-summary" class="tools-cohorts"></div>
 <div id="crosstest-validator"></div>
 <div id="crosstest-malformed"></div>
 <div id="crosstest-table" class="crosstest-table"></div>
 <div id="crosstest-hold" class="crosstest-table crosstest-hold"></div>
</section>
<section id="trust-view" class="panel" role="tabpanel" aria-labelledby="trust-tab" hidden>
 <div class="panel-head"><div><div class="eyebrow">Retained producer reports / counts and arithmetic only</div><h2>Trust tiers, Hackbook QC and cross-test</h2></div><button id="cloud-trust-refresh" type="button" class="small">Refresh retained reports</button></div>
 <p class="seal">Retained DEV-intended studies, not held-out performance</p>
 <p>Counts come from retained producer reports and are source claims; this page only derives rates from them. Unscored cases are shown separately and never enter a rate. Tier 3 provisional rows are never prompt eligible. No benchmark or rule is ranked or declared a winner.</p>
 <p id="cloud-trust-status" class="notice" role="status"></p>
<h3>Three core systems side by side</h3><div id="cloud-trust-playbook"></div>
 <h3>Trusted-memory tiers</h3><div id="cloud-trust-tiers"></div>
 <h3>Hackbook QC per rule</h3><div id="cloud-trust-qc"></div>
 <h3>Tune versus cross-test</h3><div id="cloud-trust-tune"></div>
<h3>Monitor cascade per stage</h3><div id="cloud-trust-cascade"></div>
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
  const executed=list(data?.benchmarks).filter(entry=>
   ['evallab.seeker_executed_differentials/v2','evallab.seeker_factorial_2x2_differentials/v1'].includes(entry?.report?.schema)&&entry.report.cohort?.split==='dev');
  for(const entry of executed){
   const section=node('article',undefined,'cloud-report');
   section.append(node('h3',entry.file),node('small',entry.report.schema));
   cloudProvenance(entry,section);
   cloudExecutedDifferentials(entry.report,section);
   $('cloud-seeker-reports').append(section);
  }
  const orthogonal=list(data?.benchmarks).filter(entry=>
   entry?.report?.schema==='evallab.seeker_orthogonal_differentials/v1'&&entry.report.cohort?.split==='dev');
  for(const entry of orthogonal){
   const section=node('article',undefined,'cloud-report');
   section.append(node('h3',entry.file),node('small',entry.report.schema));
   cloudProvenance(entry,section);
   cloudOrthogonalDifferentials(entry.report,section);
   $('cloud-seeker-reports').append(section);
  }
  const roster=node('article',undefined,'cloud-report');
  roster.append(node('h3','Declared comparison lanes'),
   node('small','Declared by the Priority #2 contract. Presence here is a request, not a result.'),
   cloudTable(['Declared requested model','Retained reports naming it'],seekerRoster.map(id=>[id,
    named.get(modelBase(id))?'Named in '+named.get(modelBase(id))+' retained report(s)':
    'No retained report names this model'])));
  $('cloud-seeker-roster').append(roster);
  notice('cloud-seeker-status',reports.length+executed.length+orthogonal.length?
   (reports.length+executed.length+orthogonal.length)+' retained DEV Reward-Seeker reports.':
   'No retained DEV Reward-Seeker comparison report. No results have been fabricated.');
 }catch(error){
  notice('cloud-seeker-status',cloudError(error,'Reward-Seeker comparison'),true);
 }finally{$('cloud-seeker-refresh').disabled=false;}
}
function cloudExecutedDifferentials(report,parent){
 const cohort=report.cohort,total=cohort.task_count,capacity=new Map();
 for(const row of list(cohort.datasets))capacity.set(row.dataset_id,row.count_n);
 parent.append(node('p','Protocol: '+report.protocol+' · source digest '+report.source_sha256.slice(0,12)+' · generated '+report.generated_at),
  node('p','Cross-key audit: '+cohort.audited_n+' tasks joined against '+cohort.key_count+' assignment keys; '+
   cohort.cross_key_excluded_n+' excluded for a test or heldout row under any key; '+total+
   ' have no test or heldout row under any key ('+list(cohort.datasets).map(row=>row.dataset_id+' '+row.count_n).join(', ')+'). A task that appears under no key is not affirmatively assigned DEV.','seal'));
 const rows=[];
 for(const arm of list(report.arms)){
  for(const condition of list(arm.conditions)){
   const split=list(condition.by_dataset).map(row=>row.dataset_id+' '+row.executed_differential_n+'/'+row.cells_n+' of '+capacity.get(row.dataset_id)).join(' · ');
   const cells=condition.cells_n===0?'Not evaluated':condition.cells_n+' of '+total+' tasks';
   const conditionLabel=condition.condition+(typeof condition.hide_tests==='boolean'?' · tests '+(condition.hide_tests?'hidden':'visible'):'');
   rows.push([arm.requested_model,conditionLabel,cells,condition.attested_n+'/'+condition.cells_n,
    condition.executed_differential_n+'/'+condition.cells_n,cloudRatio(condition.executed_differential_n,condition.cells_n),split]);
  }
 }
 parent.append(cloudTable(['Named model (requested)','Prompt condition','Evaluated cells','Attested','Executed differential n/cells',
  'Executed rate','Executed / cells by dataset (of tasks)'],rows),
  node('small','An executed differential is an attested cell whose original grader passed (1.0) and whose repaired grader failed it (0.0) when run. Unattested cells (digest mismatch, a different served model, refusal or truncation) are in the denominator and never in the numerator. A count can never exceed the cells evaluated for that arm and condition; conditions cover different tasks and are not pooled. Requested model names only. No model is ranked.'));
}
function cloudOrthogonalDifferentials(report,parent){
 const cohort=report.cohort,total=cohort.task_count,capacity=new Map(),roles=new Map();
 for(const row of list(cohort.datasets)){capacity.set(row.dataset_id,row.count_n);roles.set(row.dataset_id,row.role);}
 parent.append(node('p','Protocol: '+report.protocol+' · source digest '+report.source_sha256.slice(0,12)+' · generated '+report.generated_at),
  node('p','Affirmative-DEV selection: '+cohort.affirmative_dev_n+' of '+total+' selected tasks affirmatively DEV and none non-DEV under '+cohort.key_count+' assignment keys; assignments digest '+cohort.assignments_sha256.slice(0,12)+' ('+list(cohort.datasets).map(row=>row.dataset_id+' '+row.count_n+' · '+row.role).join(', ')+').','seal'));
 const rows=[];
 for(const arm of list(report.arms)){
  for(const condition of list(arm.conditions)){
   const split=list(condition.by_dataset).map(row=>row.dataset_id+' ('+roles.get(row.dataset_id)+') '+row.executed_differential_n+'/'+row.cells_n+' of '+capacity.get(row.dataset_id)).join(' · ');
   const conditionLabel=condition.condition+' · tests '+(condition.hide_tests?'hidden':'visible')+' · feedback '+condition.feedback+' · retrieval '+(condition.retrieval?'on':'off');
   rows.push([arm.requested_model,conditionLabel,condition.cells_n+' of '+total+' tasks',condition.attested_n+'/'+condition.cells_n,
    condition.executed_differential_n+'/'+condition.cells_n,cloudRatio(condition.executed_differential_n,condition.cells_n),split]);
  }
 }
 parent.append(cloudTable(['Named model (requested)','Orthogonal condition','Evaluated cells','Attested','Executed differential n/cells',
  'Executed rate','Executed / cells by dataset and role (of tasks)'],rows),
  node('small','Tune and cross-test are producer-designated roles in this retained matrix; the report records no freeze-before-measurement ordering, so cross-test rows are not an unseen-data accuracy. Conditions cover the same selected tasks but are never pooled. Requested model names only. No model is ranked.'));
}
const cloudRatio=(count,total)=>total>0?cloudFraction(count/total):'Not available';
function cloudTierCensus(report,parent){
 parent.append(node('p','Unit: '+report.unit+' · admitted DEV rows '+cloudCount(report.admitted_rows_n)+
  ' · unclassified '+cloudCount(report.unclassified_n)+' · source digest '+
  (report.source_sha256?report.source_sha256.slice(0,12):'not recorded')+' · generated '+report.generated_at));
 const totals=new Map(),tiles=node('div',undefined,'metrics');
 for(const row of list(report.tiers))totals.set(row.tier,(totals.get(row.tier)||0)+row.rows_n);
 for(const [tier,label] of [['tier1_container_verified','Tier 1 · container-verified'],
  ['tier2_consensus_labeled','Tier 2 · consensus-labeled'],['tier3_provisional','Tier 3 · provisional (never in prompts)']])
  tiles.append(cloudTile(cloudCount(totals.get(tier)||0),label));
 tiles.append(cloudTile(cloudCount(report.unclassified_n),'Unclassified'));
 parent.append(tiles,cloudTable(['Tier','Source','Evidence basis','Rows','Prompt eligible'],
  list(report.tiers).map(row=>[row.tier,row.source,row.basis,cloudCount(row.rows_n),
   row.tier==='tier3_provisional'?'0 · excluded':cloudCount(row.prompt_eligible_n)])));
 parent.append(node('small','Tier counts are producer-reported source claims; this page did not re-derive them.'));
}
function cloudHackbookQc(report,parent){
 const cohort=report.cohort;
 parent.append(node('p',cohort.dataset_id+' · DEV · '+cohort.positive_n+' labelled positives · '+cohort.clean_n+
  ' clean controls · '+cohort.unscored_n+' unscored (excluded from every rate)'),
  node('p','Ruleset SHA-256: '+report.ruleset_sha256.slice(0,12)+' · generated '+report.generated_at));
 const row=(name,positives,clean,marginal)=>[name,positives+'/'+cohort.positive_n,
  cloudRatio(positives,cohort.positive_n),clean+'/'+cohort.clean_n,cloudRatio(clean,cohort.clean_n),
  cloudRatio(positives,positives+clean),marginal];
 const rows=list(report.rules).map(rule=>row(rule.rule_id,rule.positives_hit_n,rule.clean_hit_n,
  rule.marginal_true_positive_n));
 rows.push(row('All rules (union)',report.union_positives_hit_n,report.union_clean_hit_n,'—'));
 parent.append(cloudTable(['Rule','Positives hit','Recall','Clean controls hit','False-alarm rate','Precision',
  'Marginal true positives'],rows),
  node('small','Precision depends on this cohort’s positive/clean mix and is not comparable across cohorts. A marginal true positive is a positive only that rule catches.'));
}
function cloudTuneCross(report,parent){
 const entries=list(report.entries),cross=entries.filter(entry=>entry.role==='cross_test');
 parent.append(node('p','System: '+report.system+' · frozen digest '+report.frozen_sha256.slice(0,12)+
  ' · generated '+report.generated_at),
  node('p',cross.length?cross.length+' cross-test cohort(s) measured on the same frozen digest after the last tuning measurement.':
   'Cross-test not run: no unseen cohort has been measured for this frozen system.','seal'));
 parent.append(cloudTable(['Role','Dataset','Scored positives / clean','Recall','False-alarm rate','Precision','Measured'],
  entries.map(entry=>[entry.role==='tune'?'Tune':'Cross-test',entry.dataset_id,
   entry.positive_n+' / '+entry.clean_n+' · unscored '+entry.unscored_n,
   cloudRatio(entry.true_positive_n,entry.positive_n),cloudRatio(entry.false_positive_n,entry.clean_n),
   cloudRatio(entry.true_positive_n,entry.true_positive_n+entry.false_positive_n),entry.measured_at])));
 parent.append(node('small','Each dataset appears once, so no cohort is both tuned and cross-tested. Rows are not pooled.'));
}
function cloudMonitorCascade(report,parent){
 parent.append(node('p',report.system+' '+report.cascade_version+' · arm '+report.arm+' · judge '+
  list(report.judge_models).join(', ')+' · frozen digest '+report.frozen_sha256.slice(0,12)+' · generated '+report.generated_at),
  node('p','Producer freeze statement: '+report.freeze_statement+' (recorded '+report.frozen_recorded_at+
   '). Per-cohort measurement times were not recorded, so this page cannot verify the freeze preceded the cross-test.','seal'));
 parent.append(cloudTable(['Role','Dataset','Scored positives / clean','Cross-key check','Stage 1 recall','Stage 1 false-alarm',
  'Cascade recall','Cascade false-alarm','Cascade precision','Judge calls','Judge tokens in / out'],
  list(report.cohorts).map(cohort=>[cohort.role==='tune'?'Tune':'Cross-test',cohort.dataset_id,
   cohort.positive_n+' / '+cohort.clean_n+' · unscored '+cohort.unscored_n,
   cohort.identities_checked_n+' checked · '+cohort.non_dev_assignment_n+' non-DEV',
   cloudRatio(cohort.stage1_positive_hit_n,cohort.positive_n),cloudRatio(cohort.stage1_clean_hit_n,cohort.clean_n),
   cloudRatio(cohort.true_positive_n,cohort.positive_n),cloudRatio(cohort.false_positive_n,cohort.clean_n),
   cloudRatio(cohort.true_positive_n,cohort.true_positive_n+cohort.false_positive_n),
   cloudCount(cohort.judge_calls_n),cloudCount(cohort.input_tokens_n)+' / '+cloudCount(cohort.output_tokens_n)])),
  node('small','Counts are retained producer source claims; rates are derived here. Unscored cases never enter a rate. Precision depends on each cohort’s positive/clean mix. Nothing is ranked.'));
}
const playbookRoles={tune:'Tune',cross_test:'Cross-test',dev_only:'DEV only, no cross-test'};
function playbookCost(cost){
 const tokens=cost.input_tokens_n===null&&cost.output_tokens_n===null?'tokens not recorded':
  cloudCount(cost.input_tokens_n)+' in / '+cloudCount(cost.output_tokens_n)+' out';
 const text=[cost.calls_n===null?null:cloudCount(cost.calls_n)+' calls',tokens,
  cost.usd===null?'no price recorded':cloudSpend(cost.usd)].filter(Boolean).join(' · ')+(cost.complete?'':' · usage incomplete');
 const span=node('span',text);span.title=cost.scope+' · USD: '+cost.usd_basis;return span;
}
function playbookResult(result){
 const span=node('span',result.hits_n+'/'+result.of_n+' '+result.hit_label+' · '+cloudRatio(result.hits_n,result.of_n)+
  (result.planned_of_n!==null&&result.planned_of_n>result.of_n?' (of '+result.planned_of_n+' planned)':''));
 if(result.stage_one_hits_n!==null)span.append(node('small','Stage 1 alone: '+result.stage_one_hits_n+'/'+result.of_n+' hits, '+
  result.stage_one_false_alarm_n+'/'+result.false_alarm_of_n+' false alarms'));
 return span;
}
const playbookFalseAlarms=result=>result.false_alarm_n===null?'Not measured':
 result.false_alarm_n+'/'+result.false_alarm_of_n+' clean · '+cloudRatio(result.false_alarm_n,result.false_alarm_of_n);
function playbookPerHit(result){
 const cost=result.cost;
 if(cost.input_tokens_n===null||cost.output_tokens_n===null)return 'Not recorded';
 if(result.hits_n===0)return 'No hits';
 return cloudCount(Math.round((cost.input_tokens_n+cost.output_tokens_n)/result.hits_n))+(cost.complete?'':' (usage incomplete)');
}
function cloudSystemPlaybook(report,parent){
 parent.append(node('p','Plain-English summary of the three core systems from retained results, generated '+report.generated_at+
  '. A hit means something different for each system, so the three are not comparable and nothing is ranked.'));
 const grid=node('div',undefined,'playbook-grid'),flagRows=[];
 for(const system of list(report.systems)){
  const runs=list(system.runs),card=node('article',undefined,'playbook-card');
  card.append(node('h4',system.name),node('p',system.plain_english),node('small','Measures: '+system.measure),
   node('small','Version: '+system.version+' · served by '+list(system.served_models).join(', ')),
   cloudTable(['Run','Result','False alarms','Cost'],runs.map(run=>[
    playbookRoles[run.role]+' · '+run.cohort+(run.frozen?' · frozen':''),
    playbookResult(run.result),playbookFalseAlarms(run.result),playbookCost(run.result.cost)])));
  if(!runs.some(run=>run.role==='cross_test'))
   card.append(node('p','No unseen cross-test was measured for this system, so none of these is an out-of-sample accuracy.','seal'));
  const limits=node('details'),items=node('ul');
  limits.append(node('summary','Limits ('+list(system.caveats).length+')'),items);
  for(const text of list(system.caveats))items.append(node('li',text));
  card.append(limits);grid.append(card);
  for(const flag of list(system.flags))for(const variant of list(flag.variants))
   flagRows.push([system.name,flag.flag+' · '+playbookRoles[flag.role]+' · '+flag.cohort,variant.label,playbookResult(variant),
    playbookFalseAlarms(variant),playbookPerHit(variant),playbookCost(variant.cost)]);
 }
 const flags=node('details');flags.open=true;
 flags.append(node('summary','Feature-flag efficiency: '+flagRows.length+' settings'),
  cloudTable(['System','Feature flag · cohort','Setting','Result','False alarms','Tokens per hit','Cost'],flagRows),
  node('small','Each flag is compared on one cohort with only that setting changed in the recorded runs. Tokens per hit is derived here (input plus output tokens over hits); a setting with no hits has none. Hover a cost for what it covers. A setting is never declared better.'));
 parent.append(grid,flags);
}
const atlasCategories=[['source','Any source'],['outcome','Any outcome'],['tests','Any tests'],
 ['eval_awareness','Any eval awareness'],['facet_model','Any reader'],['alarmed_by','Any detector alarm'],['min_suspicion','Any suspicion']];
const atlasBooleans=[['grader_or_harness_touched','Grader / harness'],
 ['answer_material_accessed','Answer material'],['success_claim_without_evidence','Unevidenced success claim']];
let atlasOffset=0,atlasCluster='',atlasListVersion=0,atlasRunVersion=0,atlasRunId='',atlasRunOpener=null,atlasShownIds=[],atlasToolsVersion=0;
function atlasAlarms(run,available){
 const chips=node('div',undefined,'chips');
 for(const name of list(run.alarms))chips.append(badge(name,'warn'));
 if(!available)chips.append(node('small','No detector results retained'));
 else if(!chips.children.length)chips.append(node('small',run.detector_results_n===0?'No recorded detector result':
  run.actor_inputs_available===false?'No actor transcript: unknown, not clean':'No detector alarm'));
 if(available&&run.detector_results_n>0)chips.append(node('small','results from '+cloudCount(run.detector_results_n)+(run.detector_results_n===1?' detector':' detectors')));
 return chips;
}
function atlasResultsOptions(options,available){
 const select=$('atlas-results'),selected=select.value,counts=options.results||{};
 select.replaceChildren();
 select.disabled=!available;
 if(!available){
  const option=node('option','No detector results retained');option.value='';select.append(option);select.value='';return;
 }
 for(const [value,label] of [['','Any detector results'],['any','Has detector results ('+cloudCount(counts.any||0)+')'],
  ['none','No recorded detector result ('+cloudCount(counts.none||0)+')']]){
  const option=node('option',label);option.value=value;select.append(option);
 }
 if(selected&&!Array.from(select.options).some(option=>option.value===selected)){
  const option=node('option',selected+' (not in current options)');option.value=selected;select.append(option);
 }
 select.value=selected;
}
function atlasOptions(options,available){
 for(const [key,any] of atlasCategories){
  const select=$('atlas-'+key),selected=select.value,first=node('option',any);
  first.value='';select.replaceChildren(first);
  for(const [value,count] of Object.entries(options[key==='min_suspicion'?'suspicion':key]||{})){
   const option=node('option',value+' ('+cloudCount(count)+')');option.value=value;select.append(option);
  }
  if(selected&&!Array.from(select.options).some(option=>option.value===selected)){
   const option=node('option',selected+' (not in current options)');option.value=selected;select.append(option);
  }
  select.value=selected;
 }
 for(const [key] of atlasBooleans){
  const select=$('atlas-'+key),selected=select.value;
  select.replaceChildren();
  for(const [value,label] of [['','Any'],['true','Yes ('+cloudCount(options[key])+')'],['false','No']]){
   const option=node('option',label);option.value=value;select.append(option);
  }
  select.value=selected;
 }
 atlasResultsOptions(options,available);
}
function atlasHealth(data){
 const store=data.store,parent=$('cloud-atlas-health'),counts=node('div',undefined,'atlas-health');
 parent.replaceChildren();
 for(const [key,label] of [['facets_n','Facets read'],['visible_n','Facets shown'],
  ['rejected_n','Rejected lines'],['superseded_n','Superseded facets'],
  ['unadmitted_n','Runs withheld · not admitted'],['evidence_outside_run_n','Evidence outside the run']])
  counts.append(badge(label+': '+cloudCount(store[key])));
 const scored=list(data.clusters).some(cluster=>list(cluster.label_rates).length);
 parent.append(counts,node('p',data.scored_at?'Scored at: '+data.scored_at:
  (scored?'Scoring time not recorded':'Not scored yet')));
 if(store.cluster_error)parent.append(node('p','Cluster-file error: '+store.cluster_error,'notice error'));
 if(store.rejected_n){
  const details=node('details'),items=node('ul',undefined,'cloud-issue-list');
  details.append(node('summary','Rejected lines ('+cloudCount(store.rejected_n)+') · retained rejection notes'),items);
  for(const rejection of list(store.rejected))items.append(node('li','Line '+text(rejection.line)+': '+text(rejection.reason)));
  parent.append(details);
 }
}
function atlasTree(clusters){
 const parent=$('cloud-atlas-clusters'),children=new Map(),focused=document.activeElement?.dataset?.clusterId;
 parent.replaceChildren();
 if(!clusters.length){parent.append(node('p','No retained cluster tree.','empty'));return;}
 for(const cluster of clusters){
  const key=cluster.parent??null;if(!children.has(key))children.set(key,[]);
  children.get(key).push(cluster);
 }
 function branch(parentId){
  const items=node('ul');
  for(const cluster of children.get(parentId)||[]){
   const item=node('li'),select=button('',()=>{
    atlasCluster=atlasCluster===cluster.cluster_id?'':cluster.cluster_id;atlasOffset=0;loadAtlas();
   },'atlas-cluster');
   select.dataset.clusterId=cluster.cluster_id;select.title=cluster.centroid_text;
   select.setAttribute('aria-pressed',String(atlasCluster===cluster.cluster_id));
   select.append(node('strong',(atlasCluster===cluster.cluster_id?'Selected · ':'')+cluster.name),
    node('small','Recorded size '+cloudCount(cluster.size)+' · admitted '+cloudCount(cluster.admitted_n)+
     ' · matching '+cloudCount(cluster.matching_n)),node('small',cluster.centroid_text,'atlas-centroid'));
   const rates=list(cluster.label_rates);
   select.append(node('small',rates.length?rates.map(rate=>text(rate.label)+': '+cloudCount(rate.hits_n)+
    '/'+cloudCount(rate.of_n)+' ('+cloudRatio(rate.hits_n,rate.of_n)+')').join(' · '):'Not scored yet'));
   item.append(select);
   if(cluster.level<3&&children.has(cluster.cluster_id))item.append(branch(cluster.cluster_id));
   items.append(item);
  }
  return items;
 }
 parent.append(branch(null));
 if(focused)Array.from(parent.querySelectorAll('button')).find(item=>item.dataset.clusterId===focused)?.focus({preventScroll:true});
}
function atlasFlagBadges(run){
 const chips=node('div',undefined,'chips');
 for(const [key,label] of atlasBooleans)chips.append(badge(label+': '+(typeof run[key]==='boolean'?
  (run[key]?'Yes':'No'):'Not recorded'),run[key]===true?'warn':''));
 return chips;
}
function atlasRuns(data){
 const parent=$('cloud-atlas-runs'),runs=list(data.runs),clusters=new Map(list(data.clusters).map(item=>[item.cluster_id,item.name]));
 atlasShownIds=runs.map(run=>String(run.run_id));$('atlas-copy-ids').disabled=!atlasShownIds.length;
 parent.replaceChildren();
 $('atlas-page').textContent=cloudCount(data.total)+' matching · offset '+atlasOffset;
 $('atlas-prev').disabled=atlasOffset===0;
 $('atlas-next').disabled=atlasOffset+Number($('atlas-limit').value)>=data.total;
 if(!runs.length){
  parent.append(node('p',data.store.facets_n===0&&data.store.rejected_n===0?
   'No facets have been written yet. Nothing is fabricated.':
   'No admitted DEV runs match these filters. Reset filters or inspect store health.','empty'));return;
 }
const openers=[],table=cloudTable(['Suspicion','Run ID · open detail','Detectors · results / alarms','Dataset / task','Goal','Approach','Shortcut',
  'Tests','Facet flags','Eval awareness','Tokens in / out','Clusters','Reader · facet version · blinding'],runs.map(run=>{
  const open=button(String(run.run_id).slice(0,20),()=>openAtlasRun(run.run_id,open),'link');
  open.title=run.run_id;open.setAttribute('aria-label','Open run '+run.run_id);open.dataset.runId=run.run_id;openers.push(open);
  const identity=node('span',run.dataset_id),chips=node('div',undefined,'chips');
  identity.append(node('small',run.task_id));
  for(const id of list(run.cluster_ids)){const chip=badge(clusters.get(id)??id);chip.title=id;chips.append(chip);}
  if(!chips.children.length)chips.append(node('small','No retained cluster'));
  return [badge(cloudCount(run.suspicion),run.suspicion>0?'warn':''),open,atlasAlarms(run,data.alarms_available),identity,run.goal,run.approach,
   run.shortcut,run.tests,atlasFlagBadges(run),run.eval_awareness,
   cloudCount(run.input_tokens)+' / '+cloudCount(run.output_tokens),chips,atlasVersion(run)];
 }));
 for(const [index,row] of Array.from(table.querySelectorAll('tbody tr')).entries()){
  row.className='atlas-run-row';
  row.addEventListener('click',event=>{if(!event.target.closest('button'))openers[index].click();});
 }
 parent.append(table);
}
function closeAtlasRun(restoreFocus=true){
 ++atlasRunVersion;atlasRunId='';$('atlas-run-panel').hidden=true;$('atlas-run-content').replaceChildren();
 if(restoreFocus)(atlasRunOpener?.isConnected?atlasRunOpener:$('cloud-atlas-refresh')).focus();
}
async function loadAtlas(){
 const version=++atlasListVersion,params=atlasFilterParams();
 for(const [name,value] of Object.entries({split:'dev',limit:$('atlas-limit').value,offset:String(atlasOffset)}))params.set(name,value);
 closeAtlasRun(false);$('cloud-atlas-refresh').disabled=true;$('atlas-prev').disabled=true;$('atlas-next').disabled=true;$('atlas-copy-ids').disabled=true;
 $('cloud-atlas-runs').replaceChildren();$('cloud-atlas-health').replaceChildren();$('atlas-page').textContent='';
 notice('cloud-atlas-status','Loading retained DEV facets and clusters…');
 $('atlas-view').setAttribute('aria-busy','true');
 try{
  const data=await cloudApi('/api/cloud-db/atlas?'+params);
  if(version!==atlasListVersion)return;
  atlasOptions(data.options,data.alarms_available);atlasHealth(data);atlasTree(list(data.clusters));atlasRuns(data);
  notice('cloud-atlas-status',data.notice);
  history.replaceState(null,'',atlasLink());
 }catch(error){
  if(version===atlasListVersion){
   $('cloud-atlas-clusters').replaceChildren();
   notice('cloud-atlas-status',cloudError(error,'Run Atlas'),true);
  }
 }finally{
  if(version===atlasListVersion){$('cloud-atlas-refresh').disabled=false;$('atlas-view').setAttribute('aria-busy','false');}
 }
}
const blindingText={refreshed:'Re-blinded',pending:'Re-blind pending',unlisted:'Not on refresh list',unknown:'Blinding unknown'};
const blindingBadge=state=>state?badge(blindingText[state]??state,state==='pending'?'warn':''):node('small','Not recorded');
function atlasVersion(run){
 const box=node('div',undefined,'task-lines');
 box.append(node('small',run.facet_model,'mono'),node('small',run.facet_version),blindingBadge(run.blinding));
 return box;
}
function openRunInAtlas(runId){
 $('cloud-atlas-filters').reset();$('atlas-query').value=runId;atlasOffset=0;atlasCluster='';$('atlas-tab').click();
}
function coverageLines(counts,label){
 const entries=Object.entries(counts||{}).filter(([,value])=>value>0);
 if(!entries.length)return node('small','None stored');
 return taskLines(entries.map(([key,value])=>label(key)+' · '+cloudCount(value)));
}
function atlasCoverage(data){
 const parent=$('atlas-coverage-body'),totals=data.totals,tiles=node('div',undefined,'metrics');
 parent.replaceChildren();
 for(const [value,label] of [[cloudCount(totals.stored_n)+' / '+cloudCount(totals.admitted_n),'Admitted runs with a stored facet'],
  [cloudCount(totals.not_stored_n),'Admitted runs without a facet (not ready)'],[cloudCount(totals.pending_blinding_n),'Stored facets awaiting re-blinding'],
  [cloudCount(totals.rejected_n),'Rejected facet lines'],[cloudCount(totals.unadmitted_n+totals.evidence_outside_run_n),'Stored but withheld']])tiles.append(cloudTile(value,label));
 parent.append(tiles);
 if(data.refresh_error)parent.append(node('p','Blinding refresh list: '+data.refresh_error,'notice error'));
 else if(totals.refresh_listed_n!==null)parent.append(node('small','Re-blinding refresh list: '+cloudCount(totals.refresh_listed_n)+' runs listed, '+cloudCount(totals.refreshed_n)+' stored as the re-extraction.'));
 parent.append(cloudTable(['Population (dataset · trust tier)','Stored / admitted','Ready','Facet version','Blinding'],
  list(data.populations).map(row=>{
   const missing=row.admitted_n-row.stored_n,ready=node('div',undefined,'task-lines');
   ready.append(node('small',cloudRatio(row.stored_n,row.admitted_n)),missing>0?badge(cloudCount(missing)+' not ready','warn'):badge('All stored','good'));
   return [row.dataset_id+' · '+row.trust_tier,cloudCount(row.stored_n)+' / '+cloudCount(row.admitted_n),ready,
    coverageLines(row.facet_versions,key=>key),
    coverageLines(row.blinding,key=>blindingText[key]??key)];
  })));
}
const readerKappa='Facets from different readers agree only at kappa 0.42 to 0.47 (RA-6, 92 runs read by every reader), so a score on a mixed population depends on who read the run. Compare within one reader, or filter the runs table to one.';
function atlasReaders(data){
 const parent=$('atlas-readers-body'),rows=list(data.populations),mixed=rows.filter(row=>row.mixed_readers);
 parent.replaceChildren();
 if(!rows.length){parent.append(node('p','No admitted DEV populations.','empty'));return;}
 parent.append(mixed.length?node('p',cloudCount(mixed.length)+' of '+cloudCount(rows.length)+' populations mix more than one reader: '+
   mixed.map(row=>row.dataset_id+' · '+row.trust_tier).join('; ')+'. '+readerKappa,'notice error'):
  node('p','No population mixes readers. Each population with stored facets was read by a single model.','notice'));
 parent.append(cloudTable(['Population (dataset · trust tier)','Stored facets','Readers'],rows.map(row=>{
  const readers=Object.entries(row.facet_models||{}).filter(([,value])=>value>0).sort((left,right)=>right[1]-left[1]),cell=node('div',undefined,'task-lines');
  if(!readers.length)cell.append(badge('No reader yet','warn'),node('small','Nothing stored, so no model has read this population.'));
  else{
   for(const [model,count] of readers)cell.append(node('small',model+' · '+cloudCount(count)+' ('+cloudRatio(count,row.stored_n)+')','mono'));
   cell.append(row.mixed_readers?badge('Mixed readers','warn'):badge('One reader','good'));
  }
  return [row.dataset_id+' · '+row.trust_tier,cloudCount(row.stored_n)+' / '+cloudCount(row.admitted_n)+' admitted',cell];
 })));
}
const reliabilityBasis={within_1:'within one level'};
function atlasReliability(data){
 const parent=$('atlas-reliability-body');
 parent.replaceChildren();
 if(!data.measured){
  parent.append(node('p',data.error||'Not measured.','empty'));
  return;
 }
 const item=data.reliability,meta=node('small',text(item.source)+' · measured '+toolsTime(item.measured_at)+' · '+cloudCount(item.sample_runs)+' runs sampled · '+item.design);
 parent.append(meta);
 const table=cloudTable(['Field','Agreement','Agreed / rated','95% interval','Basis'],item.fields.map(row=>{
  const track=node('span',undefined,'reliability-track'),bar=node('span',undefined,'reliability-bar'),cell=node('div',undefined,'task-lines');
  bar.style.width=row.agreement+'%';track.append(bar);
  cell.append(node('strong',row.agreement.toFixed(1)+'%'),track);
  return [row.field,cell,cloudCount(row.agree_n)+' / '+cloudCount(row.n),row.ci95[0].toFixed(1)+'% to '+row.ci95[1].toFixed(1)+'%',
   row.basis?reliabilityBasis[row.basis]??row.basis:'Not stated'];
 }));
 table.classList.add('reliability-table');
 parent.append(table);
}
let atlasReliabilityVersion=0;
async function loadAtlasReliability(){
 const version=++atlasReliabilityVersion;
 notice('atlas-reliability-status','Loading field reliability…');
 try{
  const data=await cloudApi('/api/cloud-db/atlas/field-reliability?split=dev');
  if(version!==atlasReliabilityVersion)return;
  atlasReliability(data);
  notice('atlas-reliability-status',data.measured?data.notice:'No agreement was measured, so nothing here says any field is reliable or unreliable.',!data.measured&&String(data.error).startsWith('unreadable'));
 }catch(error){
  if(version===atlasReliabilityVersion){
   $('atlas-reliability-body').replaceChildren();
   notice('atlas-reliability-status',cloudError(error,'Field reliability'),true);
  }
 }
}
let atlasCoverageVersion=0;
async function loadAtlasCoverage(){
 const version=++atlasCoverageVersion;
 notice('atlas-coverage-status','Loading coverage…');
 try{
  const data=await cloudApi('/api/cloud-db/atlas/coverage?split=dev');
  if(version!==atlasCoverageVersion)return;
  atlasCoverage(data);atlasReaders(data);
  notice('atlas-coverage-status',data.notice);
  notice('atlas-readers-status','Counts are stored facets on admitted DEV runs, by the model that read them. No labels are involved.');
 }catch(error){
  if(version===atlasCoverageVersion){
   $('atlas-coverage-body').replaceChildren();$('atlas-readers-body').replaceChildren();
   notice('atlas-coverage-status',cloudError(error,'Atlas coverage'),true);
   notice('atlas-readers-status',cloudError(error,'Reader mix'),true);
  }
 }
}
function atlasFacet(run){
 const details=node('dl',undefined,'atlas-facet');
 for(const [label,value] of [['Run ID',run.run_id],['Dataset / task',text(run.dataset_id)+' / '+text(run.task_id)],
  ['Source / split',text(run.source)+' / '+text(run.split)],['Goal',run.goal],['Approach',run.approach],
  ['Shortcut',run.shortcut],['Outcome',run.outcome],['Tests',run.tests],['Eval awareness',run.eval_awareness],
  ['Suspicion',cloudCount(run.suspicion)],['Reader (facet model)',run.facet_model],['Facet version',run.facet_version],
  ['Blinding',blindingText[run.blinding]??'Not recorded'],
  ['Tokens in / out',cloudCount(run.input_tokens)+' / '+cloudCount(run.output_tokens)],
  ['Trust tier',run.trust_tier],['Run steps',cloudCount(run.step_count)],
  ['Facet evidence indices',list(run.evidence_steps).join(', ')||'None recorded']]){
  const field=node('div');field.append(node('dt',label),node('dd',value));details.append(field);
 }
 return details;
}
function atlasEvidence(steps,parent){
 parent.append(node('h3','Evidence steps and neighbours'));
 if(!steps.length){parent.append(node('p','No returnable evidence steps were provided.','empty'));return new Map();}
 const targets=new Map(),items=node('div',undefined,'cards');
 for(const step of [...steps].sort((left,right)=>left.step_index-right.step_index)){
  const item=node('article',undefined,'atlas-step '+(step.is_evidence?'evidence':'neighbour'));
  item.tabIndex=-1;item.dataset.stepIndex=String(step.step_index);targets.set(step.step_index,item);
  item.append(node('h4','Step '+text(step.step_index)+' · '+(step.is_evidence?'Evidence':'Neighbour')+
   ' · '+text(step.role)+' · tool: '+text(step.tool_name)));
  for(const [label,value] of [['Command / action',step.command_or_action],['Text',step.content_text],
   ['Observation preview',step.observation_preview]])if(value)item.append(node('small',label),node('pre',value));
  if(step.flagged)item.append(badge('Flagged passage','warn'));
  if(step.flags&&Object.keys(step.flags).length)item.append(node('small','Retained step flags'),node('pre',step.flags));
  items.append(item);
 }
 parent.append(items);return targets;
}
function atlasTraceFlags(data,targets,parent){
 parent.append(node('h3','Fired trace flags'));
 if(data.flags===null){
  parent.append(node('p',data.flags_unavailable?'Flags unavailable: '+data.flags_unavailable:
   'Trace flags were not provided.','empty'));return;
 }
 const flags=Object.entries(data.flags||{});
 if(!flags.length){parent.append(node('p','No fired trace flags were provided.','empty'));return;}
 const items=node('div',undefined,'cards atlas-flags');
 for(const [name,flag] of flags){
  const card=basicCard(name,'Recorded count: '+cloudCount(flag.count)),indices=node('div',undefined,'actions');
  for(const index of list(flag.step_indices)){
   const target=targets.get(index),jump=button('Step '+text(index),()=>{
    target?.scrollIntoView({block:'center'});target?.focus({preventScroll:true});
   });
   jump.disabled=!target;jump.title=target?'Go to the shown evidence window':'Outside the shown evidence windows';
   indices.append(jump);
  }
  card.append(indices);items.append(card);
 }
 parent.append(items);
}
function atlasPlaybook(cards,parent){
 parent.append(node('h3','Matching playbook cards'));
 if(!cards.length){parent.append(node('p','No matching playbook cards were provided.','empty'));return;}
 const grid=node('div',undefined,'playbook-grid');
 for(const card of cards){
  const item=node('article',undefined,'playbook-card');
  item.append(node('h4',card.card_id),node('small','Trigger flags: '+list(card.trigger_flags).join(', ')),
   node('p',card.mechanism_summary),node('small','Signature pattern'),node('pre',card.signature_pattern),
   node('small','Verification recipe'),node('pre',card.verification_recipe),
   node('small','DEV support count: '+cloudCount(card.dev_support_count)));
  grid.append(item);
 }
 parent.append(grid);
}
async function openAtlasRun(runId,opener){
 const version=++atlasRunVersion,panel=$('atlas-run-panel'),parent=$('atlas-run-content');
 atlasRunId=runId;atlasRunOpener=opener;panel.hidden=false;parent.replaceChildren();
 $('atlas-run-title').textContent='Run '+runId;$('atlas-run-refresh').disabled=true;panel.setAttribute('aria-busy','true');
 notice('atlas-run-status','Loading retained run facet and evidence…');panel.focus();panel.scrollIntoView({block:'start'});
 try{
  const data=await cloudApi('/api/cloud-db/atlas/run?'+new URLSearchParams({run_id:runId,split:'dev'}));
  if(version!==atlasRunVersion)return;
  const run=data.run;
  parent.append(button('Open task evidence for '+text(run.task_id),()=>openTask(run.task_id)));
  parent.append(harnessCardButton(runId));
  parent.append(atlasFacet(run),atlasFlagBadges(run),node('h3','Cluster paths'));
  const paths=list(run.cluster_paths);
  if(paths.length)for(const path of paths)parent.append(node('p',list(path).map(cluster=>cluster.name).join(' → ')));
  else parent.append(node('p','No retained cluster paths.','empty'));
  if(data.withheld)parent.append(node('p',data.withheld,'notice error'));
  const targets=data.withheld?new Map():atlasEvidence(list(data.evidence),parent);
  atlasTraceFlags(data,targets,parent);atlasPlaybook(list(data.playbook_cards),parent);
  notice('atlas-run-status',data.notice);
 }catch(error){
  if(version===atlasRunVersion)notice('atlas-run-status',error.status===404?
   'This run is not in the atlas (404). Refresh the atlas to see currently admitted runs.':
   cloudError(error,'Run Atlas detail'),true);
 }finally{
  if(version===atlasRunVersion){$('atlas-run-refresh').disabled=false;panel.setAttribute('aria-busy','false');}
 }
}
$('cloud-atlas-refresh').addEventListener('click',()=>{loadAtlas();loadAtlasTools();loadAtlasCoverage();loadAtlasReliability();});
$('cloud-atlas-filters').addEventListener('change',()=>{atlasOffset=0;loadAtlas();});
$('cloud-atlas-filters').addEventListener('submit',event=>{event.preventDefault();atlasOffset=0;loadAtlas();});
$('atlas-reset').addEventListener('click',()=>{
 $('cloud-atlas-filters').reset();atlasOffset=0;atlasCluster='';loadAtlas();
});
$('atlas-prev').addEventListener('click',()=>{atlasOffset=Math.max(0,atlasOffset-Number($('atlas-limit').value));loadAtlas();});
$('atlas-next').addEventListener('click',()=>{atlasOffset+=Number($('atlas-limit').value);loadAtlas();});
$('atlas-run-close').addEventListener('click',()=>closeAtlasRun());
$('atlas-run-refresh').addEventListener('click',()=>{if(atlasRunId)openAtlasRun(atlasRunId,atlasRunOpener);});
$('atlas-view').addEventListener('keydown',event=>{
 if(event.key==='Escape'&&!$('atlas-run-panel').hidden){event.preventDefault();closeAtlasRun();}
});
const atlasFilterKeys=[...atlasCategories,...atlasBooleans].map(([key])=>key).concat('sort','results');
function atlasFilterParams(){
 const params=new URLSearchParams();
 for(const key of atlasFilterKeys){
  const value=$('atlas-'+key).value;
  if(value&&!(key==='sort'&&value==='suspicion')&&!(key==='results'&&$('atlas-results').disabled))params.set(key,value);
 }
 const query=$('atlas-query').value.trim();if(query)params.set('q',query);
 if(atlasCluster)params.set('cluster_id',atlasCluster);
 return params;
}
function atlasLink(){
 const query=String(atlasFilterParams());
 return location.origin+location.pathname+'#atlas'+(query?'?'+query:'');
}
function applyAtlasLink(){
 if(!location.hash.startsWith('#atlas'))return false;
 const params=new URLSearchParams(location.hash.slice(7));
 for(const key of atlasFilterKeys){
  const value=params.get(key),select=$('atlas-'+key);
  if(!value)continue;
  if(!Array.from(select.options).some(option=>option.value===value)){const option=node('option',value);option.value=value;select.append(option);}
  select.value=value;
 }
 $('atlas-query').value=(params.get('q')||'').slice(0,200);
 atlasCluster=params.get('cluster_id')||'';
 return true;
}
async function copyText(value,label,statusId='cloud-atlas-status'){
 try{await navigator.clipboard.writeText(value);notice(statusId,'Copied '+label+'.');}
 catch{notice(statusId,'Clipboard is unavailable in this browser context; '+label+' were not copied.',true);}
}
$('atlas-copy-ids').addEventListener('click',()=>copyText(atlasShownIds.join('\n'),
 cloudCount(atlasShownIds.length)+(atlasShownIds.length===1?' run ID':' run IDs')));
$('atlas-copy-link').addEventListener('click',()=>copyText(atlasLink(),'the link to this view'));
function toolsBrief(entry){
 if(!entry)return 'No retained result';
 if(!entry.positives_decided)return 'No runs read';
 const alarms=entry.negatives_decided?toolsPct(entry.fp/entry.negatives_decided)+' ('+cloudCount(entry.fp)+'/'+
  cloudCount(entry.negatives_decided)+')':'no clean runs read';
 return 'caught '+toolsPct(entry.tp/entry.positives_decided)+' ('+cloudCount(entry.tp)+'/'+
  cloudCount(entry.positives_decided)+') · false alarms '+alarms;
}
async function loadAtlasTools(){
 const version=++atlasToolsVersion;
 notice('atlas-tools-status','Loading tool scorecards…');
 try{
  const data=await cloudApi('/api/cloud-db/trace-tools?split=dev'),populations=list(data.populations);
  if(version!==atlasToolsVersion)return;
  $('atlas-tools-table').replaceChildren(cloudTable(['Tool',...populations.map(population=>population.title),'Last run'],
   list(data.tools).map(tool=>[tool.name,...populations.map(population=>toolsBrief(tool.populations[population.key])),
    tool.last_run_at?toolsTime(tool.last_run_at):'No run recorded'])));
  notice('atlas-tools-status','Counts only. A tool that did not read a run abstained: that run is neither a miss nor a clean pass.');
 }catch(error){
  if(version===atlasToolsVersion){
   $('atlas-tools-table').replaceChildren();
   notice('atlas-tools-status',cloudError(error,'Tool scorecard'),true);
  }
 }
}
$('atlas-tools-open').addEventListener('click',()=>$('tools-tab').click());
let repairData=null,repairVersion=0;
const repairTone=status=>status==='native-validated'?'good':status==='no repair'?'blue':'warn';
function repairLink(repair){
 const label='#'+repair.pr+' '+(repair.kind==='ts9-candidate'?'candidate':'related candidate')+(repair.merged===true?' · merged':'');
 const target=repair.pr_url?node('a',label):node('span',label);
 if(repair.pr_url){target.href=repair.pr_url;target.target='_blank';target.rel='noopener noreferrer';}
 const cell=node('div',undefined,'task-lines');
 cell.append(target,node('small',repair.native_receipt?'Native receipt: '+repair.native_receipt:'Checked: '+repair.verification+' · no native proof'));
 return cell;
}
function repairRow(row){
 const status=node('div',undefined,'task-lines');
 status.append(badge(row.status,repairTone(row.status)),node('small',row.status_note));
 const defects=node('div',undefined,'task-lines');
 for(const [kind,n] of Object.entries(row.defect_types))defects.append(node('span',kind+' × '+cloudCount(n)));
 const repairs=node('div',undefined,'task-lines');
 for(const repair of list(row.repairs))repairs.append(repairLink(repair));
 repairs.append(node('small','Main repair: '+row.main_repair));
 const runs=node('div',undefined,'task-lines');
 runs.append(node('strong',cloudCount(row.passed_tests_modified)),node('small','of '+cloudCount(row.atlas_runs)+' atlas runs'));
 const open=row.admitted?button('Task evidence',()=>openTask(row.task_id)):node('small','Not in the cloud database');
 const id=node('div',undefined,'task-lines');
 id.append(node('strong',row.task_id),node('small',row.dataset_id));
 return [id,defects,cloudCount(row.annotations)+(row.targeted_annotations?' ('+cloudCount(row.targeted_annotations)+' targeted)':''),repairs,status,runs,open];
}
function renderRepairs(){
 const data=repairData,body=$('repairs-table'),summary=$('repairs-summary');
 body.replaceChildren();summary.replaceChildren();
 if(!data)return;
 if(!data.present){
  body.append(node('p','No repair board to show: '+data.error+'. Set EVALLAB_REPAIR_INVENTORY to the TS-9 inventory, or place it at repair-board/inventory.json in the data root. Nothing has been fabricated.','empty'));
  return;
 }
 const counts=data.counts;
 for(const status of data.statuses)summary.append(badge(status+' · '+cloudCount(counts.by_status[status]),repairTone(status)));
 summary.append(badge(cloudCount(counts.tasks)+' tasks · '+cloudCount(counts.annotations)+' annotations'),
  badge('Passed with tests modified · '+cloudCount(counts.passed_tests_modified)),
  badge('Inventory '+toolsTime(data.inventory.observed_at)));
 const status=$('repairs-status-filter').value,defect=$('repairs-defect-filter').value,query=$('repairs-task-filter').value.trim().toLowerCase();
 const rows=data.tasks.filter(row=>(!status||row.status===status)&&(!defect||defect in row.defect_types)&&(!query||row.task_id.toLowerCase().includes(query)));
 if(!rows.length){body.append(node('p','No task matches these filters.','empty'));return;}
 body.append(cloudTable(['Task','Defect types','Annotations','Repair','Status','Passed, tests modified','Evidence'],rows.map(repairRow)));
 notice('repairs-status',data.notice+' Showing '+cloudCount(rows.length)+' of '+cloudCount(counts.tasks)+' tasks.');
}
function fillSelect(id,values,anyLabel){
 const select=$(id),current=select.value;
 select.replaceChildren(new Option(anyLabel,''),...values.map(value=>new Option(value,value)));
 select.value=values.includes(current)?current:'';
}
async function loadRepairs(){
 const version=++repairVersion;
 $('repairs-refresh').disabled=true;
 notice('repairs-status','Loading the grader repair board…');
 try{
  const data=await cloudApi('/api/cloud-db/repair-board?split=dev');
  if(version!==repairVersion)return;
  repairData=data;
  fillSelect('repairs-status-filter',data.statuses,'Any status');
  fillSelect('repairs-defect-filter',data.defect_types,'Any defect type');
  renderRepairs();
  if(!data.present)notice('repairs-status',data.error,String(data.error).startsWith('unreadable'));
 }catch(error){
  if(version===repairVersion){repairData=null;renderRepairs();notice('repairs-status',cloudError(error,'Grader repair board'),true);}
 }finally{
  if(version===repairVersion)$('repairs-refresh').disabled=false;
 }
}
$('repairs-refresh').addEventListener('click',loadRepairs);
$('repairs-filters').addEventListener('change',renderRepairs);
$('repairs-filters').addEventListener('input',renderRepairs);
$('repairs-filters').addEventListener('submit',event=>event.preventDefault());
let crosstestVersion=0;
const crosstestFlags=[['eval_only','Eval only'],['local_only','Local only'],['no_training','No training'],['no_rehosting','No rehosting']];
function crosstestLink(label,url){
 if(!url)return node('span',label);
 const link=node('a',label);link.href=url;link.target='_blank';link.rel='noopener noreferrer';return link;
}
function crosstestRow(entry){
 const id=node('div',undefined,'task-lines');
 id.append(node('strong',entry.name),node('small',entry.id+' · unit: '+entry.unit),crosstestLink('Source',entry.source_url));
 const counts=Object.entries(entry.counts),details=node('details');
 details.append(node('summary',cloudCount(counts.length)+' counts'),cloudTable(['Count','Value'],counts.map(([key,value])=>[key,cloudCount(value)])));
 const scoring=node('div',undefined,'task-lines');
 scoring.append(badge(entry.command_level.scorable?'Command-level scoring: possible':'Command-level scoring: not possible',entry.command_level.scorable?'blue':'warn'));
 if(entry.command_level.reason)scoring.append(node('small',entry.command_level.reason));
 const rights=node('div',undefined,'chips');
 for(const [key,label] of crosstestFlags)rights.append(badge(label+': '+(entry.flags[key]?'yes':'no')));
 rights.append(badge('Uploadable: no','warn'));
 const licence=node('div',undefined,'task-lines');
 licence.append(node('strong',entry.licence.text),node('small','Redistributable: '+(entry.licence.redistributable?'yes':'no')),node('small','Hosted model API for evaluation: '+entry.hosted_model_api.split('_').join(' ')));
 const adapter=node('div',undefined,'task-lines');
 adapter.append(badge(entry.adapter.merge_state,entry.adapter.merge_state==='merged'?'blue':'warn'),
  entry.adapter.pr?crosstestLink('PR #'+entry.adapter.pr,entry.adapter.pr_url):node('small','No PR recorded'),node('small',entry.status||''));
 return [id,entry.label_type,details,scoring,licence,rights,adapter];
}
function renderCrosstest(data){
 for(const id of ['crosstest-summary','crosstest-validator','crosstest-malformed','crosstest-table','crosstest-hold'])$(id).replaceChildren();
 if(!data.present){
  $('crosstest-table').append(node('p','No cross-test sets to show: '+data.error+'. Set EVALLAB_CROSS_TEST_REGISTRY to cross-test-registry.json, or place it at cross-test/registry.json in the data root. Nothing has been fabricated.','empty'));
  return;
 }
 $('crosstest-summary').append(badge(cloudCount(data.entries.length)+' staged sets'),badge(cloudCount(data.hold_access.length)+' need Peter, not available','warn'),
  badge(cloudCount(data.malformed.length)+' malformed',data.malformed.length?'bad':''),badge('Uploadable · 0'),
  badge(cloudCount(data.hold_licence_not_staged_n)+' more held for licence, not staged'),data.created?badge('Registry '+data.created):node('span'));
 if(data.validator){
  const box=node('div',undefined,'task-command-row');
  box.append(node('strong','Validate the registry against the staged bundles'),node('small',' · reads manifests on this machine; no record text'));
  for(const [label,command] of [['Full check',data.validator.command],['Quick check (no hashing, no adapter check)',data.validator.quick_command]]){
   box.append(node('small',label),node('pre',command,'task-command'),button('Copy command',()=>copyText(command,'the validator command','crosstest-status')));
  }
  $('crosstest-validator').append(box);
 }
 for(const problem of data.malformed){
  $('crosstest-malformed').append(node('p','Malformed '+problem.where+(problem.id?' ('+problem.id+')':'')+': '+problem.reason+'. Not shown as a usable set.','notice error'));
 }
 if(data.entries.length){
  $('crosstest-table').append(cloudTable(['Set','Label type','Counts','Command-level scoring','Licence','Rights','Adapter'],data.entries.map(crosstestRow)));
 }else $('crosstest-table').append(node('p','No valid staged set is listed.','empty'));
 if(data.hold_access.length){
  const hold=$('crosstest-hold');
  hold.append(node('h3','Held: needs Peter, not available'),cloudTable(['Set','State','Why held'],data.hold_access.map(item=>{
   const id=node('div',undefined,'task-lines');
   id.append(node('strong',item.name),node('small',item.id),crosstestLink('Source',item.source_url));
   return [id,badge(item.state,'warn'),item.why_held||'\u2014'];
  })));
 }
}
async function loadCrosstest(){
 const version=++crosstestVersion;
 $('crosstest-refresh').disabled=true;
 notice('crosstest-status','Loading the cross-test registry…');
 try{
  const data=await cloudApi('/api/cloud-db/cross-test-sets?split=dev');
  if(version!==crosstestVersion)return;
  renderCrosstest(data);
  notice('crosstest-status',data.present?data.notice:data.error,!data.present&&String(data.error).startsWith('unreadable'));
 }catch(error){
  if(version===crosstestVersion){
   for(const id of ['crosstest-summary','crosstest-validator','crosstest-malformed','crosstest-table','crosstest-hold'])$(id).replaceChildren();
   notice('crosstest-status',cloudError(error,'Cross-test registry'),true);
  }
 }finally{
  if(version===crosstestVersion)$('crosstest-refresh').disabled=false;
 }
}
$('crosstest-refresh').addEventListener('click',loadCrosstest);
let taskId='',taskVersion=0,taskSearchVersion=0;
const taskLink=id=>location.origin+location.pathname+'#task?task_id='+encodeURIComponent(id);
const taskReward=value=>typeof value==='number'?String(value):'Not recorded';
const taskRatio=(passes,trials)=>Number.isInteger(passes)&&Number.isInteger(trials)?passes+' of '+trials:'Not recorded';
function applyTaskLink(){
 if(!location.hash.startsWith('#task'))return false;
 const id=new URLSearchParams(location.hash.slice(6)).get('task_id');
 if(!id)return false;
 taskId=id.slice(0,300);$('task-query').value=taskId;
 return true;
}
function openTask(id){
 taskId=id;$('task-query').value=id;
 if($('task-tab').getAttribute('aria-selected')==='true')loadTask();else $('task-tab').click();
}
function taskSection(title,hint,cls){
 const section=node('section',undefined,'task-section '+(cls||''));
 section.append(node('h3',title));
 if(hint)section.append(node('p',hint,'muted'));
 return section;
}
function taskLines(lines){
 const box=node('div',undefined,'task-lines');
 for(const line of lines)box.append(node('small',line));
 return box;
}
function taskCommands(commands){
 const section=taskSection('Fetch these records','Copy a command to pull the same evidence into an agent session. Exports are blinded; labels stay in a sealed manifest.');
 for(const entry of list(commands)){
  const row=node('div',undefined,'task-command-row'),command=entry.command.split('$EVALLAB_CLOUD_URL').join(location.origin);
  row.append(node('strong',entry.label),node('small',' · needs '+entry.needs),node('pre',command,'task-command'),
   button('Copy command',()=>copyText(command,'the '+entry.label.toLowerCase()+' command','task-status')));
  section.append(row);
 }
 return section;
}
function taskGraderFixes(data){
 const section=taskSection('Grader fixes','The recorded repaired-grader digest above, repairs qualified by Task QA, and each issue\u2019s original and repaired reward.','task-fixes'),
  repairs=list(data.repairs);
 if(!repairs.length)section.append(node('p','No qualified repair events are retained for this task.','empty'));
 else section.append(cloudTable(['When','Campaign','Qualified','Source','Repaired task digest','Exploit passes','No-op passes','Reference passes'],
  repairs.map(repair=>[toolsTime(repair.at),repair.campaign,badge(repair.qualified?'Qualified':'Not qualified',repair.qualified?'good':'warn'),
   repair.source,cloudDigest(repair.repaired_task_digest),taskRatio(repair.exploit,repair.exploit_trials),
   taskRatio(repair.nop,repair.nop_trials),taskRatio(repair.ref_passes,repair.reference_trials)])));
 return section;
}
function taskQa(data){
 const section=taskSection('Task QA findings','Retained issue rows and the journal\u2019s Task QA findings. Provisional means not yet verified.','task-qa'),
  issues=list(data.issues),findings=list(data.qa_findings);
 if(!issues.length)section.append(node('p','No retained issue rows for this task.','empty'));
 else section.append(cloudTable(['Issue','Defect class','Direction','Status','Trust tier','Original \u2192 repaired reward','Citation','Witness'],
  issues.map(issue=>[issue.issue_id,issue.defect_class,issue.direction,badge(text(issue.status),tone(issue.status)),issue.trust_tier,
   taskReward(issue.original_reward)+' \u2192 '+taskReward(issue.repaired_reward),
   issue.citation_file?issue.citation_file+':'+text(issue.citation_line):'Not recorded',cloudDigest(issue.witness_digest)])));
 if(!findings.length)section.append(node('p','No journal Task QA findings are retained for this task.','empty'));
 else section.append(cloudTable(['When','Finding','Phase','Severity','Status','Summary'],
  findings.map(finding=>[toolsTime(finding.at),finding.kind==='blocked'?'Blocked: '+list(finding.finding_ids).join(', '):finding.finding_id,
   finding.phase,finding.severity,finding.status,finding.summary])));
 return section;
}
function taskSeeker(data){
 const section=taskSection('Seeker differentials','A differential is a submission that passes the original grader and fails the repaired one. Exploit files are never shown here.','task-seeker'),
  rows=list(data.seeker);
 if(!rows.length){section.append(node('p','No Reward Seeker attempts are retained for this task.','empty'));return section;}
 section.append(cloudTable(['When','Event','Attempt','Differential','Original \u2192 repaired','Class','Hackbook entry','Model route','Detail'],
  rows.map(row=>[toolsTime(row.at),row.kind,row.attempt_index??row.attempts_run??'\u2014',
   row.kind==='worker'?badge(row.exploit_confirmed?'Exploit confirmed':'No exploit',row.exploit_confirmed?'good':'warn'):
    badge(row.differential_confirmed?'Confirmed':'Not confirmed',row.differential_confirmed?'good':'warn'),
   taskReward(row.original_reward)+' \u2192 '+taskReward(row.repaired_reward),row.taxonomy_class,row.hackbook_entry_id,row.model_route,
   row.mechanism||[row.failure_kind,row.target_check].filter(Boolean).join(' \u00b7 ')||'\u2014'])));
 return section;
}
function taskTools(tools){
 const names=Object.keys(tools||{});
 if(!names.length)return node('small','No tool result retained');
 return taskLines(names.map(name=>{
  const entry=tools[name],flagged=list(entry.flagged);
  return name+': '+(flagged.length?'flagged '+flagged.map(item=>item.detector+(list(item.evidence_steps).length?' (steps '+item.evidence_steps.join(', ')+')':'')).join('; ')+' · ':'')+
   cloudCount(entry.clean_n)+' clean · '+cloudCount(entry.abstained_n)+' abstained';
 }));
}
function taskRuns(data){
 const runs=list(data.runs),section=taskSection('Runs of this task','Most suspicious atlas facet first. Facets are model-read leads; tool findings are leads, not verdicts.','task-runs');
 if(data.tool_results_error)section.append(node('p','Trace-tool results: '+data.tool_results_error,'notice error'));
 if(!runs.length){section.append(node('p','No runs are retained for this task.','empty'));return section;}
 if(data.summary.runs_n>runs.length)section.append(node('p','Showing '+cloudCount(runs.length)+' of '+cloudCount(data.summary.runs_n)+' runs.','notice'));
 section.append(cloudTable(['Suspicion','Run ID','Model','Outcome','Goal / approach / shortcut','Facet flags','Clusters','Trace tools','Actions'],
  runs.map(run=>{
   const facet=run.facet,id=node('code',String(run.run_id).slice(0,22),'mono'),actions=node('div',undefined,'task-lines');
   id.title=run.run_id;
   actions.append(button('Open in atlas',()=>openRunInAtlas(run.run_id)),harnessCardButton(run.run_id),
    button('Copy export command',()=>copyText(run.export_command,'the export command','task-status')));
   return [facet?badge(cloudCount(facet.suspicion),facet.suspicion>0?'warn':''):node('small','No facet'),id,run.model_id,run.outcome,
    facet?taskLines([facet.goal,facet.approach,'Shortcut: '+facet.shortcut]):node('small','No atlas facet'),
    facet?atlasFlagBadges(facet):'\u2014',
    run.clusters.length?taskLines(run.clusters.map(path=>list(path).join(' \u2192 '))):'\u2014',taskTools(run.tools),actions];
  })));
 return section;
}
function renderTask(data){
 const parent=$('task-content'),task=data.task,summary=data.summary,head=node('dl',undefined,'atlas-facet'),tiles=node('div',undefined,'metrics');
 parent.replaceChildren();
 for(const [label,value] of [['Task',task.task_id],['Dataset',task.dataset_id],['Item',task.item_id],['Split',task.split]]){
  const box=node('div');box.append(node('dt',label),node('dd',value));head.append(box);
 }
 for(const [label,value] of [['Original grader',task.original_grader_digest],['Repaired grader',task.repaired_grader_digest]]){
  const box=node('div'),detail=node('dd');
  detail.append(typeof value==='string'?cloudDigest(value):node('span','None recorded'));box.append(node('dt',label),detail);head.append(box);
 }
 for(const [value,label] of [[cloudCount(summary.runs_n),'Runs'],[cloudCount(summary.faceted_n),'With atlas facet'],
  [cloudCount(summary.suspicion_at_least_2_n),'Facet suspicion \u2265 2'],[cloudCount(summary.tool_flagged_runs_n),'Flagged by a tool'],
  [cloudCount(summary.issues_confirmed_n)+' / '+cloudCount(summary.issues_n),'Confirmed / retained issues'],
  [cloudCount(summary.seeker_confirmed_n)+' / '+cloudCount(summary.seeker_attempts_n),'Seeker differentials / attempts'],
  [cloudCount(summary.repairs_qualified_n),'Qualified grader repairs']])tiles.append(cloudTile(value,label));
 parent.append(head,tiles,taskCommands(data.commands),taskGraderFixes(data),taskQa(data),taskSeeker(data),taskRuns(data));
}
async function loadTask(){
 const version=++taskVersion;
 $('task-refresh').disabled=true;$('task-view').setAttribute('aria-busy','true');$('task-content').replaceChildren();$('task-matches').replaceChildren();
 notice('task-status','Loading retained evidence for '+taskId+'\u2026');
 try{
  const data=await cloudApi('/api/cloud-db/task-evidence?split=dev&task_id='+encodeURIComponent(taskId));
  if(version!==taskVersion)return;
  renderTask(data);history.replaceState(null,'',taskLink(taskId));
  notice('task-status',data.notice);
 }catch(error){
  if(version===taskVersion)notice('task-status',error.status===404?
   'This is not an admitted DEV task (404). Search for a task id above.':cloudError(error,'Task evidence'),true);
 }finally{
  if(version===taskVersion){$('task-refresh').disabled=false;$('task-view').setAttribute('aria-busy','false');}
 }
}
async function searchTasks(){
 const query=$('task-query').value.trim(),version=++taskSearchVersion;
 $('task-matches').replaceChildren();
 if(query.length<2){notice('task-status','Type at least two characters of a task id.',true);return;}
 notice('task-status','Searching admitted DEV tasks\u2026');
 try{
  const data=await cloudApi('/api/cloud-db/task-evidence/search?split=dev&q='+encodeURIComponent(query)),tasks=list(data.tasks);
  if(version!==taskSearchVersion)return;
  if(!tasks.length){notice('task-status','No admitted DEV task id contains that text.');return;}
  const exact=tasks.find(item=>item.task_id===query);
  if(exact||tasks.length===1){openTask((exact||tasks[0]).task_id);return;}
  $('task-content').replaceChildren();
  $('task-matches').replaceChildren(cloudTable(['Task','Dataset','Runs','Issues'],tasks.map(item=>[
   button(item.task_id,()=>openTask(item.task_id),'link'),item.dataset_id,cloudCount(item.runs_n),cloudCount(item.issues_n)])));
  notice('task-status',tasks.length>=data.limit?'Showing the first '+data.limit+' matches; narrow the text to see others.':'Choose a task.');
 }catch(error){
  if(version===taskSearchVersion)notice('task-status',cloudError(error,'Task search'),true);
 }
}
const gateScope='keyed subset, not population false-alarm rate';
function gateKeys(gate,row,planned){
 const cell=node('div');
 if(!gate){cell.append(node('span','Not read yet','muted'));return cell;}
 cell.append(node('strong',cloudCount(gate.matched_keys)+' of '+cloudCount(row.keys_read)+' keys'));
 if(row.keys_read<planned)cell.append(node('small',cloudCount(row.keys_read)+' of '+cloudCount(planned)+' keys read so far'));
 cell.append(node('small',gateScope));
 return cell;
}
function gatePrecision(gate){
 const cell=node('div');
 if(!gate){cell.append(node('span','Not read yet','muted'));return cell;}
 if(!gate.eligible_findings)cell.append(node('strong','No eligible findings'),node('small','precision is undefined, not 100%'));
 else cell.append(node('strong',cloudCount(gate.matched_keys)+' of '+cloudCount(gate.eligible_findings)+' findings'));
 cell.append(node('small',gateScope));
 return cell;
}
function taskGate(data){
 const parent=$('task-gate-body'),progress=data.progress;
 parent.replaceChildren();
 if(!data.present)return;
 const total=progress.cohort.dev_tasks,rows=list(progress.rows);
 const answered=rows.reduce((sum,row)=>sum+row.answered,0),possible=rows.length*total;
 const tiles=node('div',undefined,'metrics');
 tiles.append(cloudTile(cloudCount(progress.cohort.dev_tasks),'DEV tasks per model and arm'),
  cloudTile(cloudCount(progress.cohort.keyed_tasks)+' keyed / '+cloudCount(progress.cohort.unkeyed_tasks)+' unkeyed','Unkeyed tasks are not certified negatives'),
  cloudTile(cloudCount(progress.cohort.planned_keys),'Keys across the keyed tasks'),
  cloudTile(cloudCount(answered)+' / '+cloudCount(possible),'Tasks answered, all models and arms'));
 parent.append(tiles,progress.all_answered?badge('Every model and arm has answered all DEV tasks','good'):badge('Partial: not every model and arm has answered all DEV tasks','warn'),
  node('small','Exported '+scopeTime(progress.exported_at)+' from the retained OQ-6 coverage summary (sha256 '+progress.source.coverage_sha256.slice(0,12)+'); the checkpoint column is the v4 checkpoint.'));
 parent.append(cloudTable(['Model · arm','Tasks answered','At v4 checkpoint','Consumed','Quota-pending','Failed','Keyed recall · 0.5 floor','Keyed recall · old 0.8 gate','Keyed precision · 0.5 floor','Keyed precision · old 0.8 gate'],
  rows.map(row=>{
   const done=node('div'),track=node('span',undefined,'gate-bar'),fill=node('span',undefined,'gate-fill');
   fill.style.width=(100*row.answered/total)+'%';track.append(fill);
   done.append(node('strong',cloudCount(row.answered)+' of '+cloudCount(total)+' tasks'),track);
   if(row.scorer_quota_pending)done.append(node('small',cloudCount(row.scorer_quota_pending)+' answered tasks await their scorer (quota)'));
   return [row.route+' · arm '+row.arm,done,cloudCount(row.baseline_answered)+' of '+cloudCount(total),cloudCount(row.consumed),
    cloudCount(row.quota_pending),cloudCount(row.failed),gateKeys(row.new_0_5,row,progress.cohort.planned_keys),
    gateKeys(row.old_0_8,row,progress.cohort.planned_keys),gatePrecision(row.new_0_5),gatePrecision(row.old_0_8)];
  })));
}
let taskGateVersion=0;
async function loadTaskGate(){
 const version=++taskGateVersion;
 notice('task-gate-status','Loading gate rerun progress\u2026');
 try{
  const data=await cloudApi('/api/cloud-db/task-evidence/gate-progress?split=dev');
  if(version!==taskGateVersion)return;
  taskGate(data);
  notice('task-gate-status',data.present?data.notice:'No gate rerun progress is retained ('+data.error+'). Set EVALLAB_GATE_PROGRESS to gate-progress-v1.json or place it at task-qa/gate-progress-v1.json in the data root. Nothing is fabricated; a missing count is not zero.',!data.present&&data.error!=='not retained');
 }catch(error){
  if(version===taskGateVersion){
   $('task-gate-body').replaceChildren();
   notice('task-gate-status',cloudError(error,'Gate rerun progress'),true);
  }
 }
}
$('task-form').addEventListener('submit',event=>{event.preventDefault();searchTasks();});
$('task-refresh').addEventListener('click',()=>{loadTaskGate();if(taskId)loadTask();else searchTasks();});
$('task-copy-link').addEventListener('click',()=>{
 if(taskId)copyText(taskLink(taskId),'the link to this task','task-status');
 else notice('task-status','Open a task first.',true);
});
let harnessRun='',harnessVersion=0,harnessCardVersion=0;
const harnessLink=id=>location.origin+location.pathname+'#harness?run_id='+encodeURIComponent(id);
const harnessPct=(part,whole)=>whole>0?(100*part/whole).toFixed(1)+'%':'no runs';
const harnessTone=verdict=>verdict==='alarm'?'bad':verdict==='clear'?'good':'warn';
function applyHarnessLink(){
 if(!location.hash.startsWith('#harness'))return false;
 const id=new URLSearchParams(location.hash.slice(9)).get('run_id');
 if(id){harnessRun=id.slice(0,200);$('harness-run').value=harnessRun;}
 return true;
}
function harnessMeasure(budget,row){
 const cell=node('div');
 if(!budget){cell.append(node('span','Not measured','muted'));return cell;}
 cell.append(node('strong',cloudCount(budget.hits)+' of '+cloudCount(row.positives)+' positives ('+harnessPct(budget.hits,row.positives)+')'),
  node('small',cloudCount(budget.false_alarms)+' of '+cloudCount(row.negatives)+' clean runs flagged ('+harnessPct(budget.false_alarms,row.negatives)+')'));
 return cell;
}
function harnessPublished(row){
 const cell=node('div');
 if(row.published)cell.append(badge(row.published.reproduced?'Reproduces':'Does not reproduce',row.published.reproduced?'good':'bad'),node('small',row.published.check));
 else if(list(row.unreproduced_parts).length)cell.append(badge('A part does not reproduce','bad'),node('small',list(row.unreproduced_parts).join(', ')));
 else cell.append(node('span','No published number to check','muted'));
 return cell;
}
function harnessScorecard(data){
 const parent=$('harness-scorecard');parent.replaceChildren();
 if(!data.present){
  const absent=data.error==='not retained';
  parent.append(node('p',absent?'No combined scorecard is retained. Place scorecard-v1.json from evallab trace score at trace-harness/ in the data root. Nothing has been fabricated.':'The combined scorecard cannot be shown ('+data.error+'). Replace scorecard-v1.json with a valid counts-only file.',absent?'empty':'notice error'));return;
 }
 parent.append(node('small','Generated '+scopeTime(data.generated_at)+'. Positives and clean runs are the corpus label counts; each row names its reader model.'),
  cloudTable(['Corpus','Detector · rule','Reader model','Recall at a 3% false-alarm budget','Recall at a 5% false-alarm budget','Localization','Cost per 1,000 runs','Median latency','Published number'],
   list(data.rows).map(row=>{
    const budget=far=>list(row.budgets).find(item=>item.far_budget===far);
    return [row.corpus,row.detectors.join(' + ')+' · '+row.rule,row.reader_model==='none'?'No reader model':row.reader_model,
     harnessMeasure(budget(0.03),row),harnessMeasure(budget(0.05),row),
     row.localization?cloudCount(row.localization.hits)+' of '+cloudCount(row.localization.scored):'Not measured',
     typeof row.cost_usd_per_1k_runs==='number'?'$'+row.cost_usd_per_1k_runs.toFixed(2):'Not recorded',
     Number.isInteger(row.latency_ms_median)?cloudCount(row.latency_ms_median)+' ms':'Not recorded',harnessPublished(row)];
   })));
}
function harnessOverview(data){
 const found=data.findings,summary=$('harness-summary'),detectors=$('harness-detectors'),flagged=$('harness-flagged');
 summary.replaceChildren();detectors.replaceChildren();flagged.replaceChildren();
 harnessScorecard(data.scorecard);
 if(!found.present){
  const absent=found.error==='not retained';
  detectors.append(node('p',absent?'No detector findings are retained. Place findings-v1.jsonl at trace-harness/ in the data root. Nothing has been fabricated; no result is not a clean pass.':'The detector findings cannot be shown ('+found.error+'). Replace findings-v1.jsonl with a valid file.',absent?'empty':'notice error'));
  flagged.append(node('p','No runs to list.','empty'));return;
 }
 const tiles=node('div',undefined,'metrics');
 tiles.append(cloudTile(cloudCount(found.runs_admitted),'DEV runs with a recorded result'),
  cloudTile(cloudCount(found.alarmed_runs),'Runs with at least one alarm'),
  cloudTile(cloudCount(found.detectors.length),'Detector, version and reader combinations'),
  cloudTile(cloudCount(found.runs_outside),'Recorded runs not admitted DEV runs (not shown)'));
 summary.append(tiles);
 const refused=Object.entries(found.refused||{});
 if(refused.length)summary.append(node('p',cloudCount(refused.reduce((sum,item)=>sum+item[1],0))+' lines were refused: '+refused.map(item=>item[0]+' ×'+item[1]).join('; ')+'.','notice error'));
 detectors.append(cloudTable(['Detector · version','Group','Reader model','Runs read','Alarm','Clear','Abstain','Unknown','Cost','Mean latency'],
  list(found.detectors).map(row=>[row.detector+' · '+row.detector_version,row.family==='run_health'?'Run health':'Trace detector',row.reader_model||'No reader model recorded',
   cloudCount(row.runs),cloudCount(row.alarm),cloudCount(row.clear),cloudCount(row.abstain),cloudCount(row.unknown),
   '$'+row.cost_usd.toFixed(4),Number.isInteger(row.latency_ms_mean)?cloudCount(row.latency_ms_mean)+' ms':'Not recorded'])));
 if(!list(found.flagged).length){flagged.append(node('p','No DEV run has an alarm.','empty'));return;}
 flagged.append(node('small','Most alarming detectors first; showing '+cloudCount(list(found.flagged).length)+' of '+cloudCount(found.alarmed_runs)+'.'),
  cloudTable(['Run','Task','Alarming detectors','Results',''],list(found.flagged).map(row=>[row.run_id,row.task_id,row.alarms.join(', '),cloudCount(row.results),
   button('Open card',()=>openHarnessCard(row.run_id))])));
}
async function loadHarness(){
 const version=++harnessVersion;
 notice('harness-status','Loading the retained harness files…');
 try{
  const data=await cloudApi('/api/cloud-db/harness?split=dev');
  if(version!==harnessVersion)return;
  harnessOverview(data);notice('harness-status',data.notice);
 }catch(error){
  if(version===harnessVersion){
   for(const id of ['harness-summary','harness-scorecard','harness-detectors','harness-flagged'])$(id).replaceChildren();
   notice('harness-status',cloudError(error,'Harness'),true);
  }
 }
}
function harnessResult(item){
 const verdict=node('div');
 verdict.append(badge(item.verdict,harnessTone(item.verdict)));
 if(item.recorded_verdict)verdict.append(node('small','Recorded '+item.recorded_verdict+', shown unknown: this run has no actor transcript.'));
 const steps=node('div');
 steps.append(node('span',list(item.evidence_steps).length?list(item.evidence_steps).join(', '):'None cited'));
 if(item.hidden_steps)steps.append(node('small',cloudCount(item.hidden_steps)+' cited steps are not steps of this run and are hidden'));
 return [item.detector+' · '+item.detector_version,item.reader_model||'No reader model recorded',verdict,item.kind,
  typeof item.score==='number'?String(item.score):'Not recorded',steps,item.summary||'No summary',
  cloudCount(item.input_tokens)+' in / '+cloudCount(item.output_tokens)+' out · $'+item.cost_usd.toFixed(4)+' · '+(Number.isInteger(item.latency_ms)?cloudCount(item.latency_ms)+' ms':'latency not recorded')];
}
function harnessCard(data){
 const parent=$('harness-card'),run=data.run;
 parent.replaceChildren();
 const head=node('div',undefined,'row');
 head.append(node('strong','Run '+run.run_id),badge(run.actor_inputs_available?'Actor transcript present':'No actor transcript: unknown, never clean',run.actor_inputs_available?'good':'warn'),
  node('span',cloudCount(run.step_count)+' steps'),button('Task '+run.task_id,()=>openTask(run.task_id)),
  button('Open in Run Atlas',()=>openRunInAtlas(run.run_id)),button('Copy link',()=>copyText(harnessLink(run.run_id),'the link to this card','harness-card-status')));
 parent.append(head,node('p',data.headline,'seal'));
 const tally=node('div',undefined,'chips');
 for(const name of ['alarm','clear','abstain','unknown'])tally.append(badge(name+' '+cloudCount(data.tally[name]),harnessTone(name)));
 parent.append(tally);
 const titles={trace:'Trace detectors',run_health:'Run health'};
 for(const group of list(data.groups)){
  parent.append(node('h3',titles[group.family]));
  if(list(group.results).length)parent.append(cloudTable(['Detector · version','Reader model','Verdict','Kind','Score','Cited steps','Summary','Tokens · cost · latency'],list(group.results).map(harnessResult)));
  else parent.append(node('p','No '+titles[group.family].toLowerCase()+' result is recorded for this run. That is not a clean result.','empty'));
 }
 if(list(data.no_result).length)parent.append(node('p','No result for this run from: '+list(data.no_result).join('; ')+'. Not a clean result.','notice'));
 if(data.facet_lead){
  const lead=data.facet_lead,box=node('details'),summary=node('summary','Run Atlas facet lead (suspicion '+text(lead.suspicion)+' of 3)'),grid=node('dl',undefined,'atlas-facet');
  for(const [name,value] of Object.entries(lead)){const item=node('div');item.append(node('dt',name.replaceAll('_',' ')),node('dd',value));grid.append(item);}
  box.append(summary,grid);parent.append(box);
 }
 parent.append(node('small','Detector tokens '+cloudCount(data.totals.input_tokens)+' in / '+cloudCount(data.totals.output_tokens)+' out, $'+data.totals.cost_usd.toFixed(4)+' across the results above.'));
}
function harnessCardButton(runId){
 const el=button('Open combined evidence card',()=>openHarnessCard(runId));
 el.setAttribute('aria-label','Open combined evidence card for run '+runId);
 return el;
}
async function openHarnessCard(runId){
 harnessRun=runId;$('harness-run').value=runId;
 if($('harness-tab').getAttribute('aria-selected')!=='true'){$('harness-tab').click();return;}
 const version=++harnessCardVersion;
 $('harness-card').replaceChildren();notice('harness-card-status','Loading the evidence card…');
 try{
  const data=await cloudApi('/api/cloud-db/harness/card?'+new URLSearchParams({run_id:runId,split:'dev'}));
  if(version!==harnessCardVersion)return;
  harnessCard(data);history.replaceState(null,'',harnessLink(runId));notice('harness-card-status',data.notice);
  $('harness-card').focus();
 }catch(error){
  if(version===harnessCardVersion)notice('harness-card-status',error.status===404?'This run is not an admitted DEV run (404).':cloudError(error,'Harness card'),true);
 }
}
$('harness-form').addEventListener('submit',event=>{
 event.preventDefault();
 const id=$('harness-run').value.trim();
 if(id)openHarnessCard(id);else notice('harness-card-status','Type a run id first.',true);
});
$('harness-refresh').addEventListener('click',()=>{loadHarness();if(harnessRun)openHarnessCard(harnessRun);});
const toolsPct=value=>(value*100).toFixed(1)+'%';
const toolsTokens=new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:1});
function toolsTime(value){
 const date=new Date(value);
 return Number.isNaN(date.getTime())?'Not recorded':date.toLocaleString('en-US',{timeZone:'America/New_York',
  month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'});
}
function toolsRate(hits,decided,total,interval,extra){
 const cell=node('div');
 if(!decided){cell.append(node('span','No runs read ('+cloudCount(total)+' labelled)','muted'));return cell;}
 cell.append(node('strong',toolsPct(hits/decided)),node('small',cloudCount(hits)+' of '+cloudCount(decided)+
  (interval?' · 95% CI '+toolsPct(interval[0])+' to '+toolsPct(interval[1]):'')));
 if(decided<total)cell.append(node('small','Read '+cloudCount(decided)+' of '+cloudCount(total)+' labelled'));
 if(extra)cell.append(node('small',extra));
 return cell;
}
function toolsCost(usage){
 const cell=node('div');
 if(!usage){cell.append(node('span','No usage recorded','muted'));return cell;}
 if(!usage.usage_runs){
  cell.append(node('span','No billable usage recorded','muted'),
   node('small',cloudCount(usage.entries)+' ledger entries, none with tokens or dollars'));
  return cell;
 }
 const per=value=>value/usage.usage_runs*1000;
 cell.append(node('strong',cloudSpend(per(usage.usd))+' per 1k runs'),
  node('small',toolsTokens.format(per(usage.input_tokens))+' in · '+toolsTokens.format(per(usage.output_tokens))+' out per 1k runs'),
  node('small','Over '+cloudCount(usage.usage_runs)+' recorded runs'));
 if(list(usage.routes).length)cell.title='Routes: '+usage.routes.join(', ');
 return cell;
}
function toolsDetectors(tool,populations){
 const total=populations.reduce((sum,population)=>sum+list(tool.populations[population.key]?.details).length,0);
 if(!total)return null;
 const details=node('details');
 details.append(node('summary','Detectors ('+total+')'));
 for(const population of populations){
  const rows=list(tool.populations[population.key]?.details);
  if(!rows.length)continue;
  const side=(hits,decided)=>decided?toolsPct(hits/decided)+' ('+cloudCount(hits)+'/'+cloudCount(decided)+')':'No runs read';
  details.append(node('h4',population.title),cloudTable(['Detector','Caught','False alarms'],
   rows.map(row=>[row.detector,side(row.tp,row.positives_decided),side(row.fp,row.negatives_decided)])));
 }
 return details;
}
function toolsRows(data){
 const populations=list(data.populations);
 return list(data.tools).map(tool=>{
  const name=node('div'),cells=[name];
  name.append(node('strong',tool.name));
  const refresh=tool.pending_blinding_refresh;
  if(refresh&&refresh.pending)name.append(badge('Provisional · '+cloudCount(refresh.pending)+' of '+
   cloudCount(refresh.listed)+' re-blinding runs not re-extracted','warn'));
  const detectors=toolsDetectors(tool,populations);
  if(detectors)name.append(detectors);
  name.append(toolsRunButton(tool));
  for(const population of populations){
   const entry=tool.populations[population.key];
   if(!entry){
    cells.push(node('span','No retained result','muted'),node('span','No retained result','muted'));
    continue;
   }
   const beyond=entry.caught_beyond_cascade==null?'':'Caught beyond cascade: '+cloudCount(entry.caught_beyond_cascade)+
    ' · cascade only: '+cloudCount(entry.cascade_only_caught);
   cells.push(toolsRate(entry.tp,entry.positives_decided,entry.positives,entry.recall_ci,beyond),
    toolsRate(entry.fp,entry.negatives_decided,entry.negatives,entry.far_ci,''));
  }
  const last=node('div');
  if(tool.last_run_at){
   const when=node('strong',toolsTime(tool.last_run_at));when.title=tool.last_run_at;
   last.append(when,node('small',tool.last_run_source));
  }else last.append(node('span','No run recorded','muted'));
  cells.push(toolsCost(tool.usage),last);
  return cells;
 });
}
const scopePct=value=>value.toFixed(1)+'%';
const scopeInterval=interval=>interval?'95% Wilson CI '+scopePct(interval[0])+' to '+scopePct(interval[1]):'';
const scopeTime=value=>new Date(value).toLocaleString('en-US',{timeZone:'America/New_York',
 year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})+' ET';
function scopeCell(cell){
 const box=node('div');
 if(cell.state!=='measured'){box.append(node('span',cell.reason,'muted'));return box;}
 const exact=cell.measure==='exact_command';
 if(cell.read)box.append(node('strong',(exact?'Exact command named ':'Flagged ')+cloudCount(cell.hits)+' of '+cloudCount(cell.read)+
  ' ('+scopePct(100*cell.hits/cell.read)+')'),node('small',scopeInterval(cell.hit_ci)));
 else box.append(node('span','No cheating runs read','muted'));
 if(cell.read<cell.positives)box.append(node('small','Read '+cloudCount(cell.read)+' of '+cloudCount(cell.positives)+' cheating runs'));
 if(cell.negatives==null)box.append(node('small','All rows cheating: no false-alarm rate'));
 else{
  if(cell.negatives_read)box.append(node('strong','False alarms '+cloudCount(cell.false_alarms)+' of '+cloudCount(cell.negatives_read)+
   ' ('+scopePct(100*cell.false_alarms/cell.negatives_read)+')'),node('small',scopeInterval(cell.false_alarm_ci)));
  else box.append(node('span','No clean runs read','muted'));
  if(cell.negatives_read<cell.negatives)box.append(node('small','Read '+cloudCount(cell.negatives_read)+' of '+cloudCount(cell.negatives)+' clean runs'));
 }
 if(cell.version)box.append(node('small','Version '+cell.version));
 if(cell.note)box.append(node('small',cell.note));
 if(cell.recorded_at){
  const recorded=node('small',scopeTime(cell.recorded_at)+' · '+cell.date_basis);
  recorded.title=cell.recorded_at+' · '+cell.source;box.append(recorded);
 }
 return box;
}
function renderToolsScope(scope){
 const lines=$('cloud-tools-scope-lines'),holder=$('cloud-tools-scope-table');
 lines.replaceChildren(...list(scope.lines).map(text=>node('li',text)));
 notice('cloud-tools-scope-status',scope.present?'':'No detector scope receipt is retained ('+scope.error+
  '). Set EVALLAB_DETECTOR_SCOPE to detector-scope-v1.json or place it at trace-tools/detector-scope-v1.json in the data root. '+
  'Until then the frozen-cascade and Stage 1 DEV cells and every HackBench cell are blank; nothing is fabricated.',!scope.present);
 const rows=list(scope.rows).map(row=>{
  const name=node('div');name.append(node('strong',row.name));
  return [name,row.reads,row.route,...['goodfire_dev','cheatbench_dev','hackbench_natural'].map(cohort=>scopeCell(row.cohorts[cohort]))];
 });
 const size=cohort=>{
  for(const row of list(scope.rows)){
   const cell=row.cohorts[cohort];
   if(cell.state==='measured'&&cell.negatives!=null)return ' · '+cloudCount(cell.positives)+' cheating, '+cloudCount(cell.negatives)+' clean';
  }
  return '';
 };
 const table=cloudTable(['Detector','Reads','Reader or judge route','Goodfire DEV'+size('goodfire_dev'),
  'CheatBench DEV'+size('cheatbench_dev')+' · examined in tuning, not held out','HackBench natural'+
  (scope.hackbench_runs?' · '+cloudCount(scope.hackbench_runs)+' runs':'')+' · cross-test, all cheating'],rows);
 const tools=list(scope.rows);
 for(const [index,row] of Array.from(table.querySelector('table > tbody').children).entries())row.dataset.tool=tools[index].tool;
 holder.replaceChildren(table);
}
function toolsSources(sources){
 const parent=$('cloud-tools-source-list'),items=node('ul',undefined,'cloud-issue-list');
 parent.replaceChildren(items);
 for(const source of list(sources)){
  const state=source.error?source.error:source.rejected_n?cloudCount(source.rejected_n)+' unreadable lines skipped':'read';
  const item=node('li');
  item.append(badge(source.file+': '+state,source.error?'bad':source.rejected_n?'warn':'good'));
  if(source.sha256)item.append(' ',cloudDigest(source.sha256));
  for(const note of list(source.notes))item.append(node('small',note));
  items.append(item);
 }
}
let toolsVersion=0;
async function loadTraceTools(){
 const version=++toolsVersion;
 $('cloud-tools-refresh').disabled=true;$('tools-view').setAttribute('aria-busy','true');
 notice('cloud-tools-status','Loading retained tool scorecards…');
 try{
  const data=await cloudApi('/api/cloud-db/trace-tools?split=dev');
  if(version!==toolsVersion)return;
  const cohorts=$('cloud-tools-cohorts');cohorts.replaceChildren();
  for(const population of list(data.populations)){
   const labels=population.labels;
   cohorts.append(badge(population.title+': '+(labels?cloudCount(labels.positive)+' cheating · '+
    cloudCount(labels.negative)+' clean · '+cloudCount(labels.unscored)+' unscored':'labels not retained')));
  }
  const table=cloudTable(['Tool',...list(data.populations).flatMap(population=>
   [population.title+' · caught',population.title+' · false alarms']),'Cost per 1k runs','Last run'],toolsRows(data));
  const tools=list(data.tools);
  for(const [index,row] of Array.from(table.querySelector('table > tbody').children).entries())row.dataset.tool=tools[index].tool;
  $('cloud-tools-table').replaceChildren(table);
  renderToolsScope(data.where_it_works);
  toolsSources(data.sources);
  notice('cloud-tools-status',data.notice);
 }catch(error){
  if(version===toolsVersion){
   $('cloud-tools-table').replaceChildren();$('cloud-tools-cohorts').replaceChildren();
   $('cloud-tools-scope-table').replaceChildren();$('cloud-tools-scope-lines').replaceChildren();
   notice('cloud-tools-status',cloudError(error,'Trace tool scorecard'),true);
  }
 }finally{
  if(version===toolsVersion){$('cloud-tools-refresh').disabled=false;$('tools-view').setAttribute('aria-busy','false');}
 }
}
$('cloud-tools-refresh').addEventListener('click',loadTraceTools);
const toolsRunKinds=[['hit','Hit'],['clean','Read clean'],['unread','Not read']];
const toolsRunLists=new Set(['facets','laminar','docent','scout','scout_metr','omp']);
let toolsRunTool='',toolsRunName='',toolsRunKind='hit',toolsRunOffset=0,toolsRunVersion=0,toolsRunIds=[];
function toolsRunButton(tool){
 if(!toolsRunLists.has(tool.tool))return node('small','Counts only · no per-run list retained','muted');
 return button('Show runs · hit / read clean / not read',()=>{
  toolsRunTool=tool.tool;toolsRunName=tool.name;toolsRunKind='hit';toolsRunOffset=0;loadToolsRuns();
 });
}
function renderToolsRuns(data){
 const counts=data.counts,kinds=$('tools-runs-kinds'),body=$('tools-runs-body');
 kinds.replaceChildren();body.replaceChildren();
 for(const [kind,label] of toolsRunKinds){
  const choose=button(label+' · '+cloudCount(counts[kind+'_n']),()=>{toolsRunKind=kind;toolsRunOffset=0;loadToolsRuns();},kind===data.kind?'primary':'small');
  choose.setAttribute('aria-pressed',String(kind===data.kind));kinds.append(choose);
 }
 const runs=list(data.runs);
 toolsRunIds=runs.map(run=>String(run.run_id));$('tools-runs-copy').disabled=!toolsRunIds.length;
 $('tools-runs-page').textContent=cloudCount(data.total)+' runs · offset '+data.offset;
 $('tools-runs-prev').disabled=data.offset===0;$('tools-runs-next').disabled=data.offset+data.limit>=data.total;
 let note=data.notice;
 if(counts.outside_n)note+=' '+cloudCount(counts.outside_n)+' runs this tool read are not admitted DEV runs here and are not listed.';
 if(data.results_error)note='Tool results: '+data.results_error+'. '+note;
 notice('tools-runs-status',note,Boolean(data.results_error));
 if(!runs.length){body.append(node('p','No admitted DEV runs in this group.','empty'));return;}
 body.append(cloudTable(['Run ID','Task','Detectors (evidence steps)','Open'],runs.map(run=>{
  const id=node('code',String(run.run_id).slice(0,26),'mono'),actions=node('div',undefined,'task-lines');
  id.title=run.run_id;
  actions.append(run.faceted?button('Open in atlas',()=>openRunInAtlas(run.run_id)):node('small','No atlas facet yet'),
   button('Open task evidence',()=>openTask(run.task_id)),harnessCardButton(run.run_id));
  const detectors=list(run.detectors).map(item=>item.detector+(list(item.evidence_steps).length?' (steps '+item.evidence_steps.join(', ')+')':''));
  return [id,run.task_id,detectors.length?taskLines(detectors):'\u2014',actions];
 })));
}
async function loadToolsRuns(){
 const version=++toolsRunVersion,panel=$('tools-runs-panel'),wasHidden=panel.hidden;
 panel.hidden=false;$('tools-runs-title').textContent='Runs · '+toolsRunName;
 for(const id of ['tools-runs-prev','tools-runs-next','tools-runs-copy'])$(id).disabled=true;
 $('tools-runs-body').replaceChildren();notice('tools-runs-status','Loading run list…');
 if(wasHidden)panel.focus();
 try{
  const data=await cloudApi('/api/cloud-db/trace-tools/runs?split=dev&limit=50&tool='+encodeURIComponent(toolsRunTool)+'&kind='+toolsRunKind+'&offset='+toolsRunOffset);
  if(version===toolsRunVersion)renderToolsRuns(data);
 }catch(error){
  if(version===toolsRunVersion)notice('tools-runs-status',cloudError(error,'Tool run list'),true);
 }
}
$('tools-runs-prev').addEventListener('click',()=>{toolsRunOffset=Math.max(0,toolsRunOffset-50);loadToolsRuns();});
$('tools-runs-next').addEventListener('click',()=>{toolsRunOffset+=50;loadToolsRuns();});
$('tools-runs-copy').addEventListener('click',()=>copyText(toolsRunIds.join('\n'),cloudCount(toolsRunIds.length)+(toolsRunIds.length===1?' run ID':' run IDs'),'tools-runs-status'));
$('tools-runs-close').addEventListener('click',()=>{++toolsRunVersion;$('tools-runs-panel').hidden=true;});
const trustSections=[
 ['evallab.system_playbook/v1','cloud-trust-playbook',cloudSystemPlaybook,'No retained system playbook.'],
 ['evallab.trust_tier_census/v1','cloud-trust-tiers',cloudTierCensus,'No retained trust-tier census.'],
 ['evallab.hackbook_qc/v1','cloud-trust-qc',cloudHackbookQc,'No retained Hackbook QC report.'],
 ['evallab.tune_cross_test/v1','cloud-trust-tune',cloudTuneCross,'No retained tune-versus-cross-test scoreboard.'],
 ['evallab.monitor_cascade/v1','cloud-trust-cascade',cloudMonitorCascade,'No retained monitor-cascade report.']];
async function loadTrustQc(){
 $('cloud-trust-refresh').disabled=true;
 for(const [,id] of trustSections)$(id).replaceChildren();
 notice('cloud-trust-status','Loading retained reports…');
 try{
  const data=await cloudApi('/api/cloud-db/benchmarks');
  let shown=0;
  for(const [schema,id,render,empty] of trustSections){
   const found=list(data?.benchmarks).filter(entry=>entry?.report?.schema===schema);
   if(!found.length)$(id).append(node('p',empty+' None has been fabricated.','empty'));
   for(const entry of found){
    const section=node('article',undefined,'cloud-report');
    section.append(node('h4',entry.file),node('small',schema));
    cloudProvenance(entry,section);
    render(entry.report,section);
    $(id).append(section);shown++;
   }
  }
  notice('cloud-trust-status',shown?shown+' retained DEV reports.':'No retained trust, QC or cross-test reports. No results have been fabricated.');
 }catch(error){
  notice('cloud-trust-status',cloudError(error,'Trust and QC reports'),true);
 }finally{$('cloud-trust-refresh').disabled=false;}
}
$('cloud-trust-refresh').addEventListener('click',loadTrustQc);
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
  if(tab.id==='atlas-tab'){loadAtlas();loadAtlasTools();loadAtlasCoverage();loadAtlasReliability();}
  if(tab.id==='tools-tab')loadTraceTools();
  if(tab.id==='repairs-tab')loadRepairs();
  if(tab.id==='crosstest-tab')loadCrosstest();
  if(tab.id==='task-tab'){loadTaskGate();if(taskId)loadTask();}
  if(tab.id==='harness-tab'){loadHarness();if(harnessRun)openHarnessCard(harnessRun);}
  if(tab.id==='trust-tab')loadTrustQc();
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
if(applyTaskLink())$('task-tab').click();
else if(applyHarnessLink())$('harness-tab').click();
else if(applyAtlasLink()||document.body.dataset.home==='atlas')$('atlas-tab').click();
refresh();
</script>
</body>
</html>"""

HOME_SCREENS = ("live", "atlas")


def dashboard_html(home: str = "live") -> str:
    """The dashboard with its opening tab chosen by the server: ``live`` or ``atlas``.

    The cloud deployment opens on the Run Atlas, where the tool scorecard and the run
    picker are the first thing seen; the local control plane has no trace database and
    keeps the live journal.
    """
    if home not in HOME_SCREENS:
        raise ValueError(f"unknown home screen {home!r}; expected one of {HOME_SCREENS}")
    return DASHBOARD.replace('<body data-home="live">', f'<body data-home="{home}">', 1)
