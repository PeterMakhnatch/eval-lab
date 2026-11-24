from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._io import atomic_write_text
from worldwatch.dashboard import esc, safe_json_for_html
from worldwatch.model import DETECTOR_NAMES, as_list, as_map, finite_float

_MAX_HTML_PAIRS = 2_016
_MAX_DETECTOR_DISPLAY = len(DETECTOR_NAMES)

_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data:;"
)

_CSS = """
:root {
  --bg: #0b0f17;
  --panel: #131a26;
  --border: #233044;
  --text: #e2e8f0;
  --muted: #94a3b8;
  --ok: #10b981;
  --warn: #f59e0b;
  --crit: #ef4444;
  --accent: #38bdf8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px;
  background: var(--bg);
  color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  line-height: 1.45;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
  margin-bottom: 18px;
}
h1 { margin: 0; font-size: 18px; color: var(--accent); }
h2 { margin: 22px 0 10px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.meta { color: var(--muted); font-size: 12px; }
.kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
}
.card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; }
.card .val { font-size: 20px; font-weight: 700; margin-top: 4px; }
.banner { margin-bottom: 10px; color: var(--warn); }
.badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 700;
}
.badge-ok { background: rgba(16, 185, 129, 0.16); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.4); }
.badge-warn { background: rgba(245, 158, 11, 0.16); color: var(--warn); border: 1px solid rgba(245, 158, 11, 0.4); }
.badge-crit { background: rgba(239, 68, 68, 0.16); color: var(--crit); border: 1px solid rgba(239, 68, 68, 0.4); }
.toolbar { margin: 10px 0; }
input[type="search"] {
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 6px 10px;
  width: 340px;
  font-family: inherit;
  font-size: 12px;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}
th, td {
  padding: 8px 10px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
th { color: var(--muted); font-weight: 600; background: #0f1520; }
tr:last-child td { border-bottom: none; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
""".strip()

_JS = """
(function () {
  var box = document.getElementById("ww-compare-filter");
  if (!box) return;
  box.addEventListener("input", function () {
    var q = box.value.toLowerCase();
    var rows = document.querySelectorAll("tr[data-filter]");
    for (var i = 0; i < rows.length; i++) {
      var hay = (rows[i].getAttribute("data-filter") || "").toLowerCase();
      rows[i].style.display = hay.indexOf(q) !== -1 ? "" : "none";
    }
  });
})();
""".strip()


def _fmt_num(value: Any, digits: int = 4, *, signed: bool = False) -> str:
    num = finite_float(value)
    if num is None:
        return "n/a"
    spec = f"+.{digits}f" if signed else f".{digits}f"
    return format(num, spec)


def _fmt_pct(value: Any, digits: int = 1) -> str:
    num = finite_float(value)
    if num is None:
        return "n/a"
    return f"{100.0 * num:.{digits}f}%"


def _fmt_usd(value: Any, *, signed: bool = False) -> str:
    num = finite_float(value)
    if num is None:
        return "n/a"
    if signed:
        prefix = "+$" if num >= 0.0 else "-$"
        return f"{prefix}{abs(num):.4f}"
    return f"${num:.4f}"


def _status_badge(report: Mapping[str, Any]) -> str:
    if bool(as_list(report.get("differential_pairs"))):
        return '<span class="badge badge-crit">ATTRITION ALERT</span>'
    if report.get("passed") is False or bool(as_list(report.get("zero_paired_pairs"))):
        return '<span class="badge badge-crit">ZERO PAIRED</span>'
    return '<span class="badge badge-ok">VALID</span>'


def _render_kpis(report: Mapping[str, Any]) -> str:
    arms = [a for a in as_list(report.get("arms")) if isinstance(a, Mapping)]
    pairs = [p for p in as_list(report.get("pairs")) if isinstance(p, Mapping)]
    diff_pairs = as_list(report.get("differential_pairs"))
    total_trials = sum(int(a.get("total_trials") or 0) for a in arms)
    measured_trials = sum(int(a.get("measured") or 0) for a in arms)
    items = (
        ("Validity Gate", _status_badge(report)),
        ("Arms / Pairs", f"{esc(len(arms))} / {esc(len(pairs))}"),
        ("Measured / Total", f"{esc(measured_trials)} / {esc(total_trials)}"),
        ("Alpha", esc(_fmt_num(report.get("alpha"), 4))),
        ("Diff Attrition Pairs", esc(len(diff_pairs))),
    )
    cards = [
        f'<div class="card"><div class="label">{esc(label)}</div><div class="val">{val}</div></div>'
        for label, val in items
    ]
    return '<div class="kpis">' + "".join(cards) + "</div>"


def _render_arms_table(arms: list[Mapping[str, Any]]) -> str:
    if not arms:
        return '<div class="card">No arms evaluated.</div>'
    rows: list[str] = []
    for arm in arms[:256]:
        det_rates = as_map(arm.get("detector_rates"))
        det_counts = as_map(arm.get("detector_counts"))
        triggered_keys = [
            k
            for k in sorted(det_rates.keys())
            if (finite_float(det_counts.get(k) if k in det_counts else det_rates.get(k)) or 0.0) > 0.0
        ][:_MAX_DETECTOR_DISPLAY]
        active_dets = [f"{esc(k)}:{esc(_fmt_pct(det_rates.get(k)))}" for k in triggered_keys]
        det_txt = ", ".join(active_dets) if active_dets else "-"
        flt = esc(f"{arm.get('arm')} {' '.join(triggered_keys)}".strip())
        rows.append(
            f'<tr data-filter="{flt}">'
            f"<td>{esc(arm.get('arm'))}</td>"
            f'<td class="num">{esc(arm.get("total_trials", 0))}</td>'
            f'<td class="num">{esc(arm.get("measured", 0))}</td>'
            f'<td class="num">{esc(_fmt_pct(arm.get("unmeasured_rate")))}</td>'
            f'<td class="num">{esc(_fmt_num(arm.get("mean_reward")))}</td>'
            f'<td class="num">{esc(_fmt_num(arm.get("mean_measured_reward")))}</td>'
            f'<td class="num">{esc(_fmt_pct(arm.get("binary_pass_rate")))}</td>'
            f'<td class="num">{esc(_fmt_usd(arm.get("mean_cost_usd")))}</td>'
            f'<td class="num">{esc(_fmt_usd(arm.get("total_cost_usd")))}</td>'
            f"<td>{det_txt}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Arm</th>"
        '<th class="num">Trials</th><th class="num">Measured</th><th class="num">Unmeasured</th>'
        '<th class="num">Mean (All)</th><th class="num">Mean (Meas)</th><th class="num">Pass %</th>'
        '<th class="num">Mean Cost</th><th class="num">Total Cost</th><th>Triggered Detectors</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _render_pairs_table(pairs: list[Mapping[str, Any]]) -> str:
    if not pairs:
        return '<div class="card">No pairwise contrasts computed.</div>'
    ordered = (
        sorted(
            pairs,
            key=lambda p: (p.get("differential_attrition") is not True, p.get("zero_paired_keys") is not True),
        )
        if len(pairs) > _MAX_HTML_PAIRS
        else pairs
    )
    banner = (
        f'<div class="card banner">Showing {_MAX_HTML_PAIRS} of {len(pairs)} contrasts (alerts prioritized).</div>'
        if len(pairs) > _MAX_HTML_PAIRS
        else ""
    )
    rows: list[str] = []
    for pair in ordered[:_MAX_HTML_PAIRS]:
        diff_attr = pair.get("differential_attrition") is True
        zero_p = pair.get("zero_paired_keys") is True
        attr_badge = (
            '<span class="badge badge-crit">DIFF</span>'
            if diff_attr
            else ('<span class="badge badge-warn">UNPAIRED</span>' if zero_p else '<span class="badge badge-ok">OK</span>')
        )
        det_deltas = as_map(pair.get("detector_deltas"))
        nz_dets = [k for k in sorted(det_deltas.keys()) if (finite_float(det_deltas.get(k)) or 0.0) != 0.0]
        det_txt = ", ".join(
            f"{esc(k)}:{esc(_fmt_num(det_deltas.get(k), 3, signed=True))}"
            for k in sorted(det_deltas.keys())[:_MAX_DETECTOR_DISPLAY]
        ) or "-"
        pair_label = f"{esc(pair.get('arm_a'))} vs {esc(pair.get('arm_b'))}"
        flt = esc(f"{pair.get('arm_a')} {pair.get('arm_b')} {' '.join(nz_dets)}".strip())
        rows.append(
            f'<tr data-filter="{flt}">'
            f"<td>{pair_label}</td>"
            f'<td class="num">{esc(pair.get("n_paired", 0))}</td>'
            f'<td class="num">{esc(_fmt_num(pair.get("mean_delta"), 4, signed=True))}</td>'
            f'<td class="num">{esc(_fmt_num(pair.get("sign_flip_p"), 6))}</td>'
            f'<td class="num">{esc(_fmt_num(pair.get("mcnemar_p"), 6))}</td>'
            f'<td class="num">{esc(_fmt_num(pair.get("attrition_fisher_p"), 6))}</td>'
            f"<td>{attr_badge}</td>"
            f'<td class="num">{esc(_fmt_usd(pair.get("cost_delta_usd"), signed=True))}</td>'
            f"<td>{det_txt}</td></tr>"
        )
    return (
        banner
        + "<table><thead><tr><th>Contrast (A vs B)</th>"
        '<th class="num">Paired N</th><th class="num">Mean Delta</th>'
        '<th class="num">Sign-Flip p</th><th class="num">McNemar p</th>'
        '<th class="num">Fisher Attr p</th><th>Attrition</th>'
        '<th class="num">Cost Delta</th><th>Detector Deltas</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def render_compare_html(report: Mapping[str, Any]) -> str:
    assert isinstance(report, Mapping)
    safe_blob = safe_json_for_html(report)
    arms = [a for a in as_list(report.get("arms")) if isinstance(a, Mapping)]
    pairs = [p for p in as_list(report.get("pairs")) if isinstance(p, Mapping)]
    arm_names = ", ".join(esc(a.get("arm")) for a in arms[:64])
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Worldwatch Compare — {arm_names}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        "<header>"
        f'<div><h1>Worldwatch Multi-Arm Comparison</h1><div class="meta">Arms: {arm_names}</div></div>'
        f'<div class="meta">Seed: {esc(report.get("seed", 0))} | Stall: {esc(report.get("stall_seconds"))}s</div>'
        "</header>\n"
        f"{_render_kpis(report)}\n"
        '<div class="toolbar"><input id="ww-compare-filter" type="search" placeholder="Filter arms, contrasts, or detectors..."></div>\n'
        f"<h2>Arms ({len(arms)})</h2>\n"
        f"{_render_arms_table(arms)}\n"
        f"<h2>Pairwise Contrasts ({len(pairs)})</h2>\n"
        f"{_render_pairs_table(pairs)}\n"
        f'<script id="worldwatch-compare" type="application/json">{safe_blob}</script>\n'
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )


def write_compare_html(path: Path, report: Mapping[str, Any]) -> None:
    content = render_compare_html(report)
    atomic_write_text(Path(path), content)
