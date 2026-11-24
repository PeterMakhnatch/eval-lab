from __future__ import annotations

import html
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._io import atomic_write_text
from worldwatch.model import as_list, as_map, canonical_json

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
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
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
  width: 320px;
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
  var box = document.getElementById("ww-filter");
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


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_json_for_html(payload: Mapping[str, Any]) -> str:
    encoded = canonical_json(payload)
    encoded = encoded.replace("&", "\\u0026")
    encoded = encoded.replace("<", "\\u003c")
    encoded = encoded.replace(">", "\\u003e")
    encoded = encoded.replace("\u2028", "\\u2028")
    encoded = encoded.replace("\u2029", "\\u2029")
    assert "</" not in encoded and "<" not in encoded and ">" not in encoded
    return encoded


def _status_badge(report: Mapping[str, Any]) -> str:
    if report.get("critical_alerts", 0) > 0 or report.get("stopped"):
        return '<span class="badge badge-crit">HALT / CRITICAL</span>'
    if report.get("warn_alerts", 0) > 0:
        return '<span class="badge badge-warn">WARN</span>'
    return '<span class="badge badge-ok">HEALTHY</span>'


def _render_kpis(report: Mapping[str, Any]) -> str:
    counts = as_map(report.get("counts"))
    cost = report.get("total_cost_usd")
    cost_txt = "n/a" if cost is None else f"${float(cost):.4f}"
    items = (
        ("Status", _status_badge(report)),
        ("Running Trials", esc(counts.get("running", 0))),
        ("Finished / Failed", f"{esc(counts.get('finished', 0))} / {esc(counts.get('failed', 0))}"),
        ("Events Read", esc(report.get("total_events", 0))),
        ("Observed Cost", esc(cost_txt)),
        ("Critical / Warn", f"{esc(report.get('critical_alerts', 0))} / {esc(report.get('warn_alerts', 0))}"),
    )
    cards = [f'<div class="card"><div class="label">{esc(label)}</div><div class="val">{val}</div></div>' for label, val in items]
    return '<div class="kpis">' + "".join(cards) + "</div>"


def _render_alerts_table(alerts: list[Mapping[str, Any]]) -> str:
    if not alerts:
        return '<div class="card">No active tripwire alerts.</div>'
    rows = []
    for alert in alerts[:500]:
        sev = str(alert.get("severity") or "warn")
        cls = "badge-crit" if sev == "critical" else "badge-warn"
        code = esc(alert.get("code"))
        trial = esc(alert.get("trial") or "-")
        seat = esc(alert.get("seat") or "-")
        msg = esc(alert.get("message"))
        flt = esc(f"{alert.get('code')} {alert.get('trial')} {alert.get('seat')} {alert.get('message')}")
        rows.append(
            f'<tr data-filter="{flt}">'
            f'<td><span class="badge {cls}">{esc(sev.upper())}</span></td>'
            f"<td>{code}</td><td>{trial}</td><td>{seat}</td><td>{msg}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Severity</th><th>Tripwire</th><th>Trial</th><th>Seat</th><th>Detail</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_trials_table(trials: list[Mapping[str, Any]]) -> str:
    if not trials:
        return '<div class="card">No trials discovered yet.</div>'
    rows = []
    for trial in trials[:1000]:
        seats = as_list(trial.get("seats"))
        seat_txt = ", ".join(
            f"{esc(s.get('seat'))}:{esc(s.get('state'))}"
            + (f"({float(s['silent_s']):.0f}s)" if s.get("silent_s") is not None else "")
            for s in seats
            if isinstance(s, Mapping)
        )
        chain_ok = trial.get("chain_ok")
        chain_badge = (
            '<span class="badge badge-crit">BROKEN</span>'
            if chain_ok is False
            else ('<span class="badge badge-ok">VERIFIED</span>' if chain_ok is True else '<span class="badge">UNHASHED</span>')
        )
        rew = "-" if trial.get("reward") is None else f"{float(trial['reward']):.4f}"
        cost = "-" if trial.get("cost_usd") is None else f"${float(trial['cost_usd']):.4f}"
        flt = esc(f"{trial.get('trial')} {trial.get('ecosystem')} {trial.get('state')}")
        rows.append(
            f'<tr data-filter="{flt}">'
            f"<td>{esc(trial.get('trial'))}</td>"
            f"<td>{esc(trial.get('ecosystem'))}</td>"
            f"<td>{esc(trial.get('state'))}</td>"
            f'<td class="num">{esc(trial.get("events_count", 0))}</td>'
            f'<td class="num">{esc(f"{100.0 * float(trial.get('reject_ratio', 0.0)):.1f}%")}</td>'
            f"<td>{chain_badge}</td>"
            f'<td class="num">{esc(rew)}</td>'
            f'<td class="num">{esc(cost)}</td>'
            f"<td>{seat_txt or '-'}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Trial</th><th>Ecosystem</th><th>State</th>"
        '<th class="num">Events</th><th class="num">Reject %</th><th>Chain</th>'
        '<th class="num">Reward</th><th class="num">Cost</th><th>Seats</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_dashboard_html(report: Mapping[str, Any]) -> str:
    safe_blob = safe_json_for_html(report)
    alerts = [a for a in (report.get("alerts") or []) if isinstance(a, Mapping)]
    trials = [t for t in (report.get("trials") or []) if isinstance(t, Mapping)]
    title = f"Worldwatch — {esc(report.get('target', ''))}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        "<header>"
        f"<div><h1>Worldwatch Live Monitor</h1><div class=\"meta\">Target: {esc(report.get('target'))}</div></div>"
        f'<div class="meta">Snapshot UTC: {esc(report.get("now"))} | Stall: {esc(report.get("stall_seconds"))}s</div>'
        "</header>\n"
        f"{_render_kpis(report)}\n"
        '<div class="toolbar"><input id="ww-filter" type="search" placeholder="Filter trials or alerts..."></div>\n'
        f"<h2>Tripwire Alerts ({len(alerts)})</h2>\n"
        f"{_render_alerts_table(alerts)}\n"
        f"<h2>Trials ({len(trials)})</h2>\n"
        f"{_render_trials_table(trials)}\n"
        f'<script id="worldwatch-snapshot" type="application/json">{safe_blob}</script>\n'
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )


def write_dashboard_html(path: Path, report: Mapping[str, Any]) -> None:
    content = render_dashboard_html(report)
    atomic_write_text(path, content)
