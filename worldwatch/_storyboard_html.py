from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._io import atomic_write_text
from worldwatch.dashboard import esc, safe_json_for_html
from worldwatch.model import as_list, as_map, finite_float, nonneg_int

_MAX_RENDER_TRIALS = 64
_MAX_RENDER_SEATS = 64
_MAX_RENDER_STEPS = 400
_MAX_RENDER_EVENTS = 64

_CRIT_DETECTORS = frozenset({
    "dup_unseen",
    "batch_blind_act",
    "unauthorized_release",
    "creditor_override",
    "misreport_world_mismatch",
})
_WARN_DETECTORS = frozenset({
    "dup_seen",
    "empty_room_open",
    "slow_report",
    "claim_churn",
    "message_neglect_decay",
    "tool_friction_storm",
})

_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data:;"
)

_CSS = """
:root { --bg: #0b0f17; --panel: #131a26; --border: #233044; --text: #e2e8f0; --muted: #94a3b8; --ok: #10b981; --warn: #f59e0b; --crit: #ef4444; --accent: #38bdf8; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--bg); color: var(--text); font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 13px; line-height: 1.45; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 8px; border-bottom: 1px solid var(--border); padding-bottom: 12px; margin-bottom: 18px; }
h1 { margin: 0; font-size: 18px; color: var(--accent); }
h2 { margin: 22px 0 10px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.meta { color: var(--muted); font-size: 12px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 12px; }
.card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.card .val { font-size: 18px; font-weight: 700; margin-top: 4px; }
.card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }
.banner { margin-bottom: 12px; color: var(--warn); border-color: rgba(245, 158, 11, 0.45); }
.badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; font-weight: 700; white-space: nowrap; }
.badge-ok { background: rgba(16, 185, 129, 0.16); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.4); }
.badge-warn { background: rgba(245, 158, 11, 0.16); color: var(--warn); border: 1px solid rgba(245, 158, 11, 0.4); }
.badge-crit { background: rgba(239, 68, 68, 0.16); color: var(--crit); border: 1px solid rgba(239, 68, 68, 0.4); }
.badge-info { background: rgba(56, 189, 248, 0.16); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.4); }
.badge-muted { background: rgba(148, 163, 184, 0.14); color: var(--muted); border: 1px solid rgba(148, 163, 184, 0.35); }
.pill-wrap { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 12px 0; }
input[type="search"] { background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 4px; padding: 6px 10px; width: 340px; max-width: 100%; font-family: inherit; font-size: 12px; }
.tab-btn, .act-btn { background: var(--panel); color: var(--muted); border: 1px solid var(--border); border-radius: 4px; padding: 5px 10px; font-family: inherit; font-size: 12px; cursor: pointer; }
.tab-btn.active { color: var(--accent); border-color: var(--accent); background: rgba(56, 189, 248, 0.12); }
.link-btn { background: none; border: none; padding: 0; color: var(--accent); font-family: inherit; font-size: 12px; font-weight: 600; cursor: pointer; text-align: left; text-decoration: underline; }
table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 600; background: #0f1520; }
tr:last-child td { border-bottom: none; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
details.trial-panel { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; margin-bottom: 12px; overflow: hidden; }
details.trial-panel > summary { cursor: pointer; padding: 10px 12px; background: #0f1520; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; }
.trial-title { font-weight: 700; color: var(--accent); }
.trial-body { padding: 12px; border-top: 1px solid var(--border); }
.swimlanes { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 12px; align-items: start; }
.seat-lane { background: #0d131f; border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
.seat-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 6px; padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid var(--border); }
.seat-name { font-weight: 700; color: var(--text); }
.seat-meta { color: var(--muted); font-size: 11px; width: 100%; }
.step-list { display: flex; flex-direction: column; gap: 6px; max-height: 620px; overflow-y: auto; }
.step-card { background: var(--panel); border: 1px solid var(--border); border-left: 3px solid var(--border); border-radius: 4px; padding: 7px 9px; font-size: 12px; }
.step-card.step-non-agent { opacity: 0.78; background: #0f1520; }
.step-card.step-batch { border-left-color: var(--warn); background: rgba(245, 158, 11, 0.07); }
.step-card.step-edge { border-color: rgba(245, 158, 11, 0.55); }
.step-card.step-same-read-act { border-left: 4px solid var(--crit); border-color: rgba(239, 68, 68, 0.5); background: rgba(239, 68, 68, 0.10); }
.step-top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 6px; }
.step-cmd { margin-top: 5px; padding: 4px 6px; background: #090d14; border: 1px solid #1b2638; border-radius: 3px; color: #cbd5e1; word-break: break-all; font-size: 11px; }
.ev-list { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.ev-chip { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 4px; padding: 2px 6px; border-radius: 4px; font-size: 11px; background: rgba(56, 189, 248, 0.10); border: 1px solid rgba(56, 189, 248, 0.30); color: var(--text); }
.ev-chip.ev-edge-chip { border-color: var(--warn); background: rgba(245, 158, 11, 0.14); }
.ev-chip.ev-fail { background: rgba(239, 68, 68, 0.16); border-color: rgba(239, 68, 68, 0.55); color: #fecaca; }
.ev-tag { font-size: 10px; font-weight: 700; padding: 0 4px; border-radius: 3px; }
.ev-edge { background: rgba(245, 158, 11, 0.25); color: var(--warn); }
.ev-fail-tag { background: rgba(239, 68, 68, 0.30); color: #fca5a5; }
""".strip()

_JS = """
(function () {
  var box = document.getElementById("ww-storyboard-filter");
  var tabs = document.querySelectorAll(".tab-btn[data-tab]");
  var counter = document.getElementById("ww-match-count");
  var activeTab = "all";
  function matchesTab(el) {
    if (activeTab === "flagged") return el.getAttribute("data-flagged") === "1";
    if (activeTab === "causal") return el.getAttribute("data-causal") === "1";
    if (activeTab === "unjoined") return el.getAttribute("data-nonclean") === "1";
    return true;
  }
  function applyFilter() {
    var q = box ? box.value.toLowerCase() : "";
    var rows = document.querySelectorAll("tr[data-filter]");
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var hay = (rows[i].getAttribute("data-filter") || "").toLowerCase();
      var ok = hay.indexOf(q) !== -1 && matchesTab(rows[i]);
      rows[i].style.display = ok ? "" : "none";
      if (ok) shown++;
    }
    var panels = document.querySelectorAll("details.trial-panel[data-filter]");
    for (var j = 0; j < panels.length; j++) {
      var phay = (panels[j].getAttribute("data-filter") || "").toLowerCase();
      var pok = phay.indexOf(q) !== -1 && matchesTab(panels[j]);
      panels[j].style.display = pok ? "" : "none";
    }
    if (counter) counter.textContent = shown + " / " + rows.length + " trials shown";
  }
  if (box) box.addEventListener("input", applyFilter);
  for (var t = 0; t < tabs.length; t++) {
    tabs[t].addEventListener("click", function () {
      activeTab = this.getAttribute("data-tab") || "all";
      for (var k = 0; k < tabs.length; k++) tabs[k].classList.remove("active");
      this.classList.add("active");
      applyFilter();
    });
  }
  var jumpBtns = document.querySelectorAll("[data-open-trial]");
  for (var b = 0; b < jumpBtns.length; b++) {
    jumpBtns[b].addEventListener("click", function () {
      var tid = this.getAttribute("data-open-trial");
      var panel = tid ? document.getElementById(tid) : null;
      if (panel) {
        panel.open = true;
        panel.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  }
  var expBtn = document.getElementById("ww-expand-all");
  var colBtn = document.getElementById("ww-collapse-all");
  if (expBtn) expBtn.addEventListener("click", function () {
    var ps = document.querySelectorAll("details.trial-panel");
    for (var i = 0; i < ps.length; i++) if (ps[i].style.display !== "none") ps[i].open = true;
  });
  if (colBtn) colBtn.addEventListener("click", function () {
    var ps = document.querySelectorAll("details.trial-panel");
    for (var i = 0; i < ps.length; i++) ps[i].open = false;
  });
  applyFilter();
})();
""".strip()


def _fmt_num(value: Any, digits: int = 3, *, signed: bool = False) -> str:
    num = finite_float(value)
    if num is None:
        return "n/a"
    spec = f"+.{digits}f" if signed else f".{digits}f"
    return format(num, spec)


def _fmt_interval(lo: Any, hi: Any) -> str:
    lo_f = finite_float(lo)
    hi_f = finite_float(hi)
    if lo_f is None or hi_f is None:
        return "[n/a, n/a]"
    return f"[{lo_f:+.3f}s, {hi_f:+.3f}s]"


def _trial_join_badge(status: str) -> str:
    if status == "joined":
        return '<span class="badge badge-ok">JOINED</span>'
    if status == "skew_joined":
        return '<span class="badge badge-warn">SKEW</span>'
    if status == "partial":
        return '<span class="badge badge-warn">PARTIAL</span>'
    if status == "no_trajectory":
        return '<span class="badge badge-muted">NO_TRAJ</span>'
    return '<span class="badge badge-crit">UNJOINED</span>'


def _seat_join_badge(seat: Mapping[str, Any]) -> str:
    reason = str(seat.get("join_reason") or "")
    offset = finite_float(seat.get("clock_offset_s")) or 0.0
    if seat.get("joined") is True:
        if seat.get("zero_offset_ok") is False or reason == "skew_joined" or abs(offset) > 1e-9:
            return '<span class="badge badge-warn">SKEW</span>'
        return '<span class="badge badge-ok">JOINED</span>'
    if reason == "no_trajectory" or not seat.get("trajectory_path"):
        return '<span class="badge badge-muted">NO_TRAJ</span>'
    return '<span class="badge badge-crit">UNJOINED</span>'


def _detector_badge(name: str, count: int | None = None) -> str:
    if name in _CRIT_DETECTORS:
        cls = "badge-crit"
    elif name in _WARN_DETECTORS:
        cls = "badge-warn"
    else:
        cls = "badge-info"
    label = f"{esc(name)} ({esc(count)})" if (count is not None and count > 1) else esc(name)
    return f'<span class="badge {cls}">{label}</span>'


def _render_detector_pills(trial: Mapping[str, Any]) -> str:
    triggered = [str(x) for x in as_list(trial.get("triggered"))][:32]
    if not triggered:
        return '<span class="meta">-</span>'
    det_map = as_map(trial.get("detectors"))
    pills = [
        _detector_badge(name, nonneg_int(as_map(det_map.get(name)).get("count")))
        for name in triggered
    ]
    return '<div class="pill-wrap">' + "".join(pills) + "</div>"


def _trial_flags(trial: Mapping[str, Any]) -> tuple[bool, bool, bool, str]:
    triggered = [str(x) for x in as_list(trial.get("triggered"))][:32]
    tsum = as_map(trial.get("summary"))
    same_step = nonneg_int(tsum.get("read_and_act_same_step")) or 0
    batch_max = nonneg_int(tsum.get("batch_opens_max")) or 0
    jstatus = str(trial.get("join_status") or "unjoined")
    seats = [s for s in as_list(trial.get("seats"))[:_MAX_RENDER_SEATS] if isinstance(s, Mapping)]
    s_parts: list[str] = []
    for s in seats:
        sn = str(s.get("seat") or "").strip()
        fc = str(s.get("facing") or "").strip()
        jr = str(s.get("join_reason") or "").strip()
        if sn:
            s_parts.append(sn)
        if fc and fc != sn:
            s_parts.append(fc)
        if jr and jr != jstatus:
            s_parts.append(jr)
    seat_tokens = " ".join(s_parts)
    is_flagged = bool(triggered) or trial.get("chain_ok") is False
    is_causal = same_step > 0 or batch_max >= 2
    is_nonclean = jstatus != "joined"
    hay = f"{trial.get('trial') or ''} {trial.get('ecosystem') or ''} {jstatus} {' '.join(triggered)} {seat_tokens}"
    return is_flagged, is_causal, is_nonclean, hay.strip()


def _render_kpis(report: Mapping[str, Any]) -> str:
    summary = as_map(report.get("summary"))
    jsc = as_map(summary.get("join_status_counts"))
    joined_n = nonneg_int(jsc.get("joined")) or 0
    skew_n = nonneg_int(jsc.get("skew_joined")) or 0
    partial_n = nonneg_int(jsc.get("partial")) or 0
    unjoined_n = (nonneg_int(jsc.get("unjoined")) or 0) + partial_n
    no_traj_n = nonneg_int(jsc.get("no_trajectory")) or 0
    top_dets = [d for d in as_list(summary.get("top_detectors"))[:4] if isinstance(d, Mapping)]
    top_html = (
        '<div class="pill-wrap">'
        + "".join(
            _detector_badge(str(d.get("detector") or ""), nonneg_int(d.get("trials_flagged")))
            for d in top_dets
        )
        + "</div>"
        if top_dets
        else '<span class="badge badge-ok">NONE FIRED</span>'
    )
    trunc_sub = "clamped to max 64" if report.get("truncated") else "all trials rendered"
    same_n = nonneg_int(summary.get("read_and_act_same_step")) or 0
    same_tr = nonneg_int(summary.get("trials_with_same_step_read_act")) or 0
    edge_n = nonneg_int(summary.get("edge_events")) or 0
    cards = (
        ("Trials (Rendered / Total)", f"{esc(report.get('trials_rendered', 0))} / {esc(report.get('trials_total', 0))}", esc(trunc_sub)),
        ("Join Status", f"{esc(joined_n)} ok · {esc(skew_n)} skew", f"{esc(unjoined_n)} unjoined · {esc(no_traj_n)} no_traj"),
        ("Edge Events", esc(edge_n), f"within ±{esc(_fmt_num(report.get('margin_s'), 2))}s step boundary"),
        ("Same-Step Read+Act", esc(same_n), f"{esc(same_tr)} trial(s) affected"),
        ("Flagged / Broken Chain", f"{esc(summary.get('trials_flagged', 0))} / {esc(summary.get('broken_chains', 0))}", f"max batch={esc(summary.get('batch_opens_max', 0))}"),
        ("Top Firing Detectors", top_html, f"{esc(summary.get('events_matched', 0))} matched events"),
    )
    html_cards = [
        f'<div class="card"><div class="label">{esc(lbl)}</div><div class="val">{val}</div><div class="sub">{sub}</div></div>'
        for lbl, val, sub in cards
    ]
    return '<div class="kpis">' + "".join(html_cards) + "</div>"


def _render_toolbar(trials: list[Mapping[str, Any]]) -> str:
    n_flagged = n_causal = n_nonclean = 0
    for tr in trials[:_MAX_RENDER_TRIALS]:
        fl, ca, nc, _ = _trial_flags(tr)
        n_flagged += int(fl)
        n_causal += int(ca)
        n_nonclean += int(nc)
    return (
        '<div class="toolbar">'
        '<input id="ww-storyboard-filter" type="search" placeholder="Filter by trial, ecosystem, join_status, seat, or detector...">'
        f'<button type="button" class="tab-btn active" data-tab="all">All ({esc(len(trials))})</button>'
        f'<button type="button" class="tab-btn" data-tab="flagged">Flagged ({esc(n_flagged)})</button>'
        f'<button type="button" class="tab-btn" data-tab="causal">Same-Step / Batch ({esc(n_causal)})</button>'
        f'<button type="button" class="tab-btn" data-tab="unjoined">Skew / Unjoined ({esc(n_nonclean)})</button>'
        '<button type="button" class="act-btn" id="ww-expand-all">Expand All</button>'
        '<button type="button" class="act-btn" id="ww-collapse-all">Collapse All</button>'
        f'<span id="ww-match-count" class="meta">{esc(len(trials))} / {esc(len(trials))} trials shown</span>'
        "</div>"
    )


def _render_index_table(trials: list[Mapping[str, Any]]) -> str:
    if not trials:
        return '<div class="card">No trials rendered.</div>'
    rows: list[str] = []
    for idx, tr in enumerate(trials[:_MAX_RENDER_TRIALS]):
        fl, ca, nc, hay = _trial_flags(tr)
        tsum = as_map(tr.get("summary"))
        same_n = nonneg_int(tsum.get("read_and_act_same_step")) or 0
        batch_max = nonneg_int(tsum.get("batch_opens_max")) or 0
        edge_n = nonneg_int(tsum.get("edge_events")) or 0
        causal_pills: list[str] = []
        if same_n > 0:
            causal_pills.append(f'<span class="badge badge-crit">SAME-STEP READ+ACT x{esc(same_n)}</span>')
        if batch_max >= 2:
            causal_pills.append(f'<span class="badge badge-warn">BATCH &gt;= 2 ({esc(batch_max)})</span>')
        if edge_n > 0:
            causal_pills.append(f'<span class="badge badge-warn">EDGE x{esc(edge_n)}</span>')
        causal_html = '<div class="pill-wrap">' + "".join(causal_pills) + "</div>" if causal_pills else '<span class="meta">-</span>'
        rew = finite_float(tr.get("reward"))
        rew_txt = esc(f"{rew:.4f}") if rew is not None else "-"
        if tr.get("chain_ok") is False:
            rew_txt += ' <span class="badge badge-crit">BROKEN</span>'
        rows.append(
            f'<tr data-filter="{esc(hay)}" data-flagged="{int(fl)}" data-causal="{int(ca)}" data-nonclean="{int(nc)}">'
            f'<td><button type="button" class="link-btn" data-open-trial="ww-trial-{idx}">{esc(tr.get("trial"))}</button></td>'
            f"<td>{esc(tr.get('ecosystem'))}</td>"
            f"<td>{_trial_join_badge(str(tr.get('join_status') or 'unjoined'))}</td>"
            f'<td class="num">{esc(tr.get("seats_joined", 0))}/{esc(tr.get("seats_total", 0))}</td>'
            f'<td class="num">{esc(tr.get("events_count", 0))}</td>'
            f"<td>{causal_html}</td>"
            f'<td class="num">{rew_txt}</td>'
            f"<td>{_render_detector_pills(tr)}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Trial</th><th>Eco</th><th>Join</th>"
        '<th class="num">Seats</th><th class="num">Events</th><th>Step Signals</th>'
        '<th class="num">Reward</th><th>Triggered Detectors</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _render_event_chip(ev: Mapping[str, Any]) -> str:
    seq = ev.get("seq")
    method = str(ev.get("method") or "").strip()
    kind = str(ev.get("kind") or "").strip()
    label = f"{method} {kind}".strip() or "event"
    target = ev.get("target")
    tgt_str = f" [{esc(target)}]" if target is not None and str(target) != "" else ""
    t_str = _fmt_num(ev.get("t"), 3)
    is_fail = ev.get("ok") is False
    is_edge = ev.get("edge") is True
    cls = "ev-chip"
    if is_fail:
        cls += " ev-fail"
    elif is_edge:
        cls += " ev-edge-chip"
    tags: list[str] = []
    if is_fail:
        tags.append('<span class="ev-tag ev-fail-tag">REJECTED</span>')
    if is_edge:
        tags.append('<span class="ev-tag ev-edge">EDGE</span>')
    seq_txt = f"#{esc(seq)} " if seq is not None else ""
    return f'<span class="{cls}">{seq_txt}{esc(label)}{tgt_str} <span class="meta">@{esc(t_str)}</span>{"".join(tags)}</span>'


def _render_step_card(step: Mapping[str, Any]) -> str:
    source = str(step.get("source") or "unknown")
    events_brief = [e for e in as_list(step.get("events_brief"))[:_MAX_RENDER_EVENTS] if isinstance(e, Mapping)]
    open_n = sum(1 for e in events_brief if str(e.get("kind") or "") == "open" and e.get("ok") is not False)
    act_n = nonneg_int(step.get("act_count")) or 0
    batch_n = max(open_n, act_n)
    same_ra = step.get("same_step_read_act") is True
    has_edge = bool(as_list(step.get("edge_seqs"))) or any(e.get("edge") is True for e in events_brief)
    fail_n = sum(1 for e in events_brief if e.get("ok") is False)
    classes = ["step-card"]
    if source != "agent":
        classes.append("step-non-agent")
    if batch_n >= 2:
        classes.append("step-batch")
    if has_edge:
        classes.append("step-edge")
    if same_ra:
        classes.append("step-same-read-act")
    pills: list[str] = [f'<span class="badge badge-muted">{esc(source)}</span>']
    if step.get("scripted") is True:
        pills.append('<span class="badge badge-info">SCRIPTED</span>')
    sleep_v = finite_float(step.get("sleep_s")) or 0.0
    if sleep_v > 0.0:
        pills.append(f'<span class="badge badge-info">sleep {esc(_fmt_num(sleep_v, 2))}s</span>')
    if same_ra:
        pills.append('<span class="badge badge-crit">SAME-STEP READ+ACT</span>')
    if batch_n >= 2:
        pills.append(f'<span class="badge badge-warn">BATCH &gt;= 2 ({esc(batch_n)})</span>')
    if has_edge:
        pills.append('<span class="badge badge-warn">EDGE</span>')
    if fail_n > 0:
        pills.append(f'<span class="badge badge-crit">REJECTED x{esc(fail_n)}</span>')
    t_txt = _fmt_num(step.get("t_epoch"), 3)
    counts_txt = f"r={esc(step.get('read_count', 0))} a={esc(act_n)} m={esc(step.get('msg_count', 0))} sleep={esc(_fmt_num(sleep_v, 2))}s"
    preview = str(step.get("keystrokes_preview") or "").strip()
    cmd_html = f'<div class="step-cmd">{esc(preview)}</div>' if preview else ""
    ev_html = '<div class="ev-list">' + "".join(_render_event_chip(e) for e in events_brief) + "</div>" if events_brief else ""
    return (
        f'<div class="{" ".join(classes)}">'
        f'<div class="step-top"><div class="pill-wrap"><strong>step {esc(step.get("step_id"))}</strong>{"".join(pills)}</div>'
        f'<span class="meta" title="{esc(step.get("timestamp") or "")}">t={esc(t_txt)} | {counts_txt}</span></div>'
        f"{cmd_html}{ev_html}</div>"
    )


def _render_seat_lane(seat: Mapping[str, Any]) -> str:
    sname = str(seat.get("seat") or "-")
    facing = str(seat.get("facing") or "").strip()
    label = f"{esc(sname)} ({esc(facing)})" if (facing and facing != sname) else esc(sname)
    badge = _seat_join_badge(seat)
    interval_txt = _fmt_interval(seat.get("offset_lo_s"), seat.get("offset_hi_s"))
    off_txt = _fmt_num(seat.get("clock_offset_s"), 3, signed=True)
    matched = nonneg_int(seat.get("events_matched")) or 0
    unmatched = nonneg_int(seat.get("events_unmatched")) or 0
    edge_n = nonneg_int(seat.get("edge_events")) or 0
    reason = str(seat.get("join_reason") or "").strip()
    reason_txt = f" | reason={esc(reason)}" if reason and reason != "joined" else ""
    raw_dis = seat.get("text_disagree_steps")
    dis_n = len(raw_dis) if isinstance(raw_dis, list) else (nonneg_int(raw_dis) or 0)
    dis_badge = f' <span class="badge badge-warn">TEXT!=LOG ({esc(dis_n)})</span>' if dis_n > 0 else ""
    steps = [s for s in as_list(seat.get("steps"))[:_MAX_RENDER_STEPS] if isinstance(s, Mapping)]
    body = (
        '<div class="step-list">' + "".join(_render_step_card(s) for s in steps) + "</div>"
        if steps
        else f'<div class="meta">No trajectory steps rendered ({esc(reason or "empty")}).</div>'
    )
    trunc_note = '<div class="meta">Step list truncated for HTML size safety.</div>' if seat.get("steps_truncated") else ""
    return (
        '<div class="seat-lane">'
        f'<div class="seat-head"><span class="seat-name">{label}</span><div class="pill-wrap">{badge}{dis_badge}</div>'
        f'<div class="seat-meta">offset={esc(off_txt)}s | window={esc(interval_txt)} | '
        f"matched={esc(matched)} unmatched={esc(unmatched)} edge={esc(edge_n)}{reason_txt}</div></div>"
        f"{body}{trunc_note}</div>"
    )


def _render_trial_panel(idx: int, trial: Mapping[str, Any], *, default_open: bool) -> str:
    fl, ca, nc, hay = _trial_flags(trial)
    seats = [s for s in as_list(trial.get("seats"))[:_MAX_RENDER_SEATS] if isinstance(s, Mapping)]
    lanes_html = (
        '<div class="swimlanes">' + "".join(_render_seat_lane(s) for s in seats) + "</div>"
        if seats
        else '<div class="meta">No seats discovered in trial.</div>'
    )
    open_attr = " open" if default_open else ""
    rew = finite_float(trial.get("reward"))
    rew_txt = f"reward={esc(f'{rew:.4f}')}" if rew is not None else "reward=n/a"
    return (
        f'<details id="ww-trial-{idx}" class="trial-panel"{open_attr} '
        f'data-filter="{esc(hay)}" data-flagged="{int(fl)}" data-causal="{int(ca)}" data-nonclean="{int(nc)}">'
        "<summary>"
        f'<div class="pill-wrap"><span class="trial-title">{esc(trial.get("trial"))}</span>'
        f'<span class="badge badge-muted">{esc(trial.get("ecosystem"))}</span>'
        f"{_trial_join_badge(str(trial.get('join_status') or 'unjoined'))}</div>"
        f'<div class="pill-wrap"><span class="meta">{rew_txt} | events={esc(trial.get("events_count", 0))}</span>'
        f"{_render_detector_pills(trial)}</div>"
        "</summary>"
        f'<div class="trial-body">{lanes_html}</div>'
        "</details>"
    )


def render_storyboard_html(report: Mapping[str, Any]) -> str:
    assert isinstance(report, Mapping)
    safe_blob = safe_json_for_html(report)
    trials = [t for t in as_list(report.get("trials"))[:_MAX_RENDER_TRIALS] if isinstance(t, Mapping)]
    banner = (
        f'<div class="card banner">Showing {esc(report.get("trials_rendered", len(trials)))} of '
        f'{esc(report.get("trials_total", len(trials)))} trials (flagged, unjoined, and high-severity prioritized).</div>'
        if report.get("truncated")
        else ""
    )
    panels = [_render_trial_panel(idx, tr, default_open=(idx < 2)) for idx, tr in enumerate(trials)]
    target_txt = esc(report.get("path", ""))
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Worldwatch Storyboard — {target_txt}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        "<header>"
        f'<div><h1>Worldwatch Causal Storyboard</h1><div class="meta">Target: {target_txt}</div></div>'
        f'<div class="meta">Boundary Margin: ±{esc(_fmt_num(report.get("margin_s"), 3))}s | '
        f'Joined: {esc(report.get("trials_joined", 0))}/{esc(report.get("trials_total", 0))}</div>'
        "</header>\n"
        f"{banner}"
        f"{_render_kpis(report)}\n"
        f"{_render_toolbar(trials)}\n"
        f"<h2>Trial Index ({len(trials)})</h2>\n"
        f"{_render_index_table(trials)}\n"
        f"<h2>Seat Step Swimlanes ({len(trials)})</h2>\n"
        f"{''.join(panels)}\n"
        f'<script id="worldwatch-storyboard-data" type="application/json">{safe_blob}</script>\n'
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )


def write_storyboard_html(path: Path, report: Mapping[str, Any]) -> None:
    content = render_storyboard_html(report)
    atomic_write_text(Path(path), content)
