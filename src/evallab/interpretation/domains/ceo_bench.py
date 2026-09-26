"""CEO-Bench domain section for run reports.

Reads the ``ceo_bench/*.json`` sidecars written by
``ceo_bench.bridge`` (never ``world.nmdb``): cash-by-sim-day,
bankruptcy, forecast error, no-op weeks, and simulator spend metered
separately from agent spend.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean, median
from typing import Any

SIDECAR_DIR = "ceo_bench"
META_FILE = "meta.json"


def _read_sidecar(trial_dir: Path, name: str) -> dict[str, Any]:
    try:
        data = json.loads((trial_dir / SIDECAR_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{SIDECAR_DIR}/{name} unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{SIDECAR_DIR}/{name} is not an object")
    return data


def _money(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"${value:,.2f}"


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []

class CeoBenchPlugin:
    """Cash trajectory, bankruptcy, forecast error, no-op weeks, split spend."""

    name = "ceo_bench"
    version = "1"

    def detect(self, trial_dir: Path, result: dict[str, Any]) -> bool:
        try:
            meta = _read_sidecar(Path(trial_dir), META_FILE)
        except ValueError:
            return False
        return meta.get("plugin") == self.name

    def build(self, trial_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        trial = Path(trial_dir)
        try:
            meta = _read_sidecar(trial, META_FILE)
            cash_doc = _read_sidecar(trial, "cash_daily.json")
            spend_doc = _read_sidecar(trial, "spend.json")
            forecasts_doc = _read_sidecar(trial, "forecasts.json")
            weeks_doc = _read_sidecar(trial, "weeks.json")
        except ValueError as exc:
            return {
                "plugin": self.name,
                "version": self.version,
                "status": "unreadable",
                "reason": str(exc),
            }
        daily = _as_list(cash_doc.get("daily"))
        rows = [r for r in daily if isinstance(r, dict)]
        cashes = [r["cash"] for r in rows if isinstance(r.get("cash"), (int, float))]
        weekly = [r for i, r in enumerate(rows) if i % 7 == 0]
        if rows and (not weekly or weekly[-1] is not rows[-1]):
            weekly = [*weekly, rows[-1]]
        forecast_rows = _as_list(forecasts_doc.get("rows"))
        scored = [
            r for r in forecast_rows
            if isinstance(r, dict) and isinstance(r.get("abs_error"), (int, float))
        ]
        abs_errors = [float(r["abs_error"]) for r in scored]
        worst = sorted(scored, key=lambda r: float(r["abs_error"]), reverse=True)[:5]
        weeks = _as_list(weeks_doc.get("weeks"))
        noop = _as_list(weeks_doc.get("noop_weeks"))
        bankruptcy = _as_dict(meta.get("bankruptcy"))
        sources = [f"{SIDECAR_DIR}/{name}" for name in
                   ("meta.json", "cash_daily.json", "spend.json", "forecasts.json", "weeks.json")]
        return {
            "plugin": self.name,
            "version": self.version,
            "sources": sources,
            "run_id": meta.get("run_id"),
            "model": meta.get("model"),
            "outcome": meta.get("outcome"),
            "bankruptcy": {
                "bankrupt": bool(bankruptcy.get("bankrupt")),
                "day": bankruptcy.get("day"),
            },
            "cash": {
                "source": cash_doc.get("source"),
                "initial": meta.get("initial_cash"),
                "final": meta.get("final_cash"),
                "min": min(cashes) if cashes else None,
                "min_day": next(
                    (r.get("day") for r in rows if r.get("cash") == min(cashes)), None
                ) if cashes else None,
                "days_tracked": len(rows),
                "weekly": [{"day": r.get("day"), "cash": r.get("cash")} for r in weekly],
            },
            "spend": {
                "agent": spend_doc.get("agent"),
                "simulator": spend_doc.get("simulator"),
                "separation_rule": spend_doc.get("separation_rule"),
            },
            "forecasts": {
                "status": forecasts_doc.get("status"),
                "reason": forecasts_doc.get("reason"),
                "n_forecasts": len([r for r in forecast_rows if isinstance(r, dict)]),
                "n_scored": len(scored),
                "mae": round(mean(abs_errors), 2) if abs_errors else None,
                "median_abs_error": round(median(abs_errors), 2) if abs_errors else None,
                "worst": [
                    {
                        "submit_day": r.get("submit_day"),
                        "horizon_days": r.get("horizon_days"),
                        "target_day": r.get("target_day"),
                        "predicted_value": r.get("predicted_value"),
                        "actual_cash": r.get("actual_cash"),
                        "abs_error": r.get("abs_error"),
                    }
                    for r in worst
                ],
            },
            "weeks": {
                "n_weeks": len(weeks),
                "n_noop": len(noop),
                "noop_weeks": noop,
                "rule": weeks_doc.get("rule"),
            },
        }

    def render_markdown(self, section: dict[str, Any]) -> list[str]:
        if section.get("status") == "unreadable":
            return [f"- Domain section unreadable ({section.get('reason')})."]
        lines: list[str] = []
        bankruptcy = section.get("bankruptcy") or {}
        cash = section.get("cash") or {}
        if bankruptcy.get("bankrupt"):
            lines.append(
                f"- Bankrupt on sim day {bankruptcy.get('day')} "
                f"(cash {_money(cash.get('min'))})."
            )
        else:
            lines.append(f"- No bankruptcy (outcome: {section.get('outcome')}).")
        lines.append(
            f"- Cash {_money(cash.get('initial'))} initial -> "
            f"{_money(cash.get('final'))} final "
            f"(min {_money(cash.get('min'))} on day {cash.get('min_day')}; "
            f"{cash.get('days_tracked')} days tracked, source {cash.get('source')})."
        )
        spend = section.get("spend") or {}
        agent = spend.get("agent") or {}
        simulator = spend.get("simulator") or {}
        if agent.get("cost_usd") is not None:
            agent_part = (
                f"Agent spend {_money(agent.get('cost_usd'))} "
                f"(source {agent.get('source')})"
            )
        else:
            agent_part = f"Agent spend unavailable: {agent.get('reason') or 'unknown'}"
        if simulator.get("cost_usd") is not None:
            simulator_part = f"simulator spend {_money(simulator.get('cost_usd'))}"
        else:
            simulator_part = (
                f"simulator spend unavailable: {simulator.get('reason') or 'unknown'}"
            )
        lines.append(
            f"- {agent_part}; {simulator_part} metered separately, "
            "never merged into agent cost."
        )
        forecasts = section.get("forecasts") or {}
        if forecasts.get("status") == "ok":
            lines.append(
                f"- Forecasts: {forecasts.get('n_scored')} of "
                f"{forecasts.get('n_forecasts')} scored, "
                f"MAE {_money(forecasts.get('mae'))}."
            )
        else:
            lines.append(f"- Forecasts unavailable ({forecasts.get('reason')}).")
        weeks = section.get("weeks") or {}
        lines.append(
            f"- No-op weeks: {weeks.get('n_noop')} of {weeks.get('n_weeks')} "
            f"(weeks {weeks.get('noop_weeks')})."
        )
        return lines
