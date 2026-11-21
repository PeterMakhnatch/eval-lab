from __future__ import annotations

import argparse
from statistics import fmean

from tidegauge import render, stats
from tidegauge.commands._common import add_common, detectable_text, load, value_format
from tidegauge.model import Arm, Counting

DESCRIPTION = """``tidegauge plan``: how many trials does a comparison need?"""
HELP = "from pilot runs, say how many attempts per task a comparison needs"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths", nargs="+", metavar="PATH", help="1 pilot arm, or baseline and candidate pilot arms"
    )
    add_common(parser)
    parser.add_argument(
        "--detect",
        type=float,
        default=0.03,
        help="smallest difference that matters, in the metric's unit (default: 0.03 = 3 points of reward)",
    )
    parser.add_argument("--alpha", type=float, default=0.05, help="significance level (default: 0.05)")
    parser.add_argument(
        "--power", type=float, default=0.8, help="chance to detect a real difference (default: 0.8)"
    )


def build(
    arms: list[Arm], metric: str, counting: Counting, detect: float, alpha: float, power: float
) -> dict:
    a = arms[0].outcomes(metric, counting)
    b = arms[1].outcomes(metric, counting) if len(arms) > 1 else None
    p = stats.plan(a, b, alpha=alpha, power=power)
    needed = p.attempts_for(detect)
    trials = [t for arm in arms for t in arm.trials]
    costs = [t.cost_usd for t in trials if t.cost_usd is not None]
    seconds = [s for t in trials if (s := t.metric("seconds")) is not None]
    cost_per_trial = fmean(costs) if costs else None
    seconds_per_trial = fmean(seconds) if seconds else None
    report = {
        "command": "plan",
        "metric": metric,
        "trials": str(counting),
        "pilot": [arm.name for arm in arms],
        "tasks": p.n_tasks,
        "alpha": alpha,
        "power": power,
        "detect": detect,
        "attempts_needed": needed,
        "trials_needed_per_arm": needed * p.n_tasks if needed else None,
        "rows": [
            {
                "attempts": r.attempts,
                "trials_per_arm": r.trials_per_arm,
                "detectable_rerun": r.mde_rerun,
                "detectable_tasks": r.mde_tasks,
            }
            for r in p.rows
        ],
        "floor_tasks": p.floor_tasks,
        "cost_per_trial": cost_per_trial,
        "seconds_per_trial": seconds_per_trial,
        "cost_needed_per_arm": None,
        "hours_needed_per_arm": None,
    }
    if needed and cost_per_trial is not None:
        report["cost_needed_per_arm"] = needed * p.n_tasks * cost_per_trial
    if needed and seconds_per_trial is not None:
        report["hours_needed_per_arm"] = needed * p.n_tasks * seconds_per_trial / 3600.0
    return report


def text(report: dict) -> str:
    unit = value_format(report["metric"])[2]

    def show(v: float | None) -> str:
        return detectable_text(v, report["metric"])

    rows = [
        [r["attempts"], r["trials_per_arm"], show(r["detectable_rerun"]), show(r["detectable_tasks"])]
        for r in report["rows"]
    ]
    out = [
        f"pilot: {', '.join(report['pilot'])} · {report['tasks']} tasks · metric: {report['metric']}",
        f"smallest true difference that a comparison detects {100 * report['power']:.0f}% of the time "
        f"(significance level {report['alpha']}):",
        "",
        render.table(
            ["attempts per task", "trials per arm", f"rerun{unit}", f"tasks{unit}"], rows, right=(0, 1, 2, 3)
        ),
        "",
    ]
    line = (
        f"To detect {show(report['detect'])}{unit} on the same tasks: {report['attempts_needed']} attempts per task, "
        f"{report['trials_needed_per_arm']:,} trials per arm"
    )
    extras = []
    if report["cost_needed_per_arm"] is not None:
        extras.append(f"about ${report['cost_needed_per_arm']:,.0f}")
    if report["hours_needed_per_arm"] is not None:
        extras.append(f"{report['hours_needed_per_arm']:,.0f} trial-hours")
    out.append(line + (f" ({' and '.join(extras)} at the pilot's mean per trial)." if extras else "."))
    if report["floor_tasks"] is not None:
        out.append(
            f"For new similar tasks, no number of reruns detects less than {show(report['floor_tasks'])}{unit} "
            f"with {report['tasks']} tasks. Below that, add tasks."
        )
    else:
        out.append("Give a baseline and a candidate pilot to see the limit for new similar tasks.")
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    if len(args.paths) > 2:
        raise SystemExit("plan takes 1 or 2 paths")
    report = build(load(args), args.metric, args.counting, args.detect, args.alpha, args.power)
    print(render.to_json(report) if args.json else text(report))
    return 0
