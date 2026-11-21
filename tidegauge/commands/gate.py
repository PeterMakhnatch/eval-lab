"""``tidegauge gate``: a pass or fail decision for CI and for agent loops.

Exit status: 0 = ok, 1 = regressed, 2 = undecided (run more trials), 3 = the data is not usable.

For rewards the gate decides under both counting rules. When the rules disagree,
the decision depends on how broken trials are counted, and the gate says
undecided. Use --one-rule to decide with the --trials rule only.
"""

from __future__ import annotations

import argparse

from tidegauge import render, stats
from tidegauge.audit import audit_arm, audit_pair
from tidegauge.commands._common import (
    add_common,
    counting_text,
    findings_text,
    load,
    other,
    unsigned,
    value_format,
)
from tidegauge.model import Arm, Counting

HELP = "decide whether a candidate may replace a baseline (exit status for CI)"
EXIT = {"ok": 0, "regressed": 1, "undecided": 2, "invalid": 3}
MAX_PRACTICAL_ATTEMPTS = 200  # above this, the gate says that the arms are too close to separate


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs=2, metavar="PATH", help="baseline first, then candidate")
    add_common(parser)
    parser.add_argument(
        "--question",
        choices=("rerun", "tasks"),
        default="rerun",
        help="which error bar decides (default: rerun)",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.0,
        help="largest drop that is still acceptable, in the metric's unit (default: 0)",
    )
    parser.add_argument(
        "--require-gain", action="store_true", help="ok only when the candidate is better beyond noise"
    )
    parser.add_argument(
        "--lower-is-better", action="store_true", help="for metrics such as cost, where lower is better"
    )
    parser.add_argument("--one-rule", action="store_true", help="decide with the --trials rule only")
    parser.add_argument(
        "--allow-errors", action="store_true", help="decide even when the audit reports errors"
    )


def decide(low: float, high: float, margin: float, require_gain: bool) -> str:
    """Decision from the interval of (candidate - baseline), where higher is better."""
    if require_gain:
        if low > 0:
            return "ok"
        return "regressed" if high < 0 else "undecided"
    if low > -margin:
        return "ok"
    if high < -margin or high < 0:
        return "regressed"
    return "undecided"


def _one(a: Arm, b: Arm, metric: str, counting: Counting, args: argparse.Namespace) -> dict:
    c = stats.compare(a.outcomes(metric, counting), b.outcomes(metric, counting))
    interval = c.ci_rerun(args.level) if args.question == "rerun" else c.ci_tasks(args.level)
    out = {
        "trials": str(counting),
        "shared_tasks": c.n_tasks,
        "difference": c.delta,
        "interval": [interval.low, interval.high] if interval else None,
        "attempts_to_resolve": None,
    }
    if interval is None:
        out["decision"] = "undecided"
        return out
    low, high = (-interval.high, -interval.low) if args.lower_is_better else (interval.low, interval.high)
    out["decision"] = decide(low, high, args.margin, args.require_gain)
    # A decision that claims a difference also needs the test to agree. The exact tests protect the cases
    # where few tasks show any trial noise, or few tasks differ, and the interval is too narrow.
    claims_difference = out["decision"] == "regressed" or (out["decision"] == "ok" and args.require_gain)
    p = c.rerun_p() if args.question == "rerun" else c.tasks_p()
    if claims_difference and (p is None or p >= 1.0 - args.level):
        out["decision"] = "undecided"
    if out["decision"] == "undecided":
        try:
            p = stats.plan(a.outcomes(metric, counting), b.outcomes(metric, counting))
        except ValueError:  # no task has 2 or more trials: no plan, and the decision stays undecided
            return out
        needed = p.attempts_for(abs(c.delta))
        out["attempts_to_resolve"] = (
            needed if needed is not None and needed <= MAX_PRACTICAL_ATTEMPTS else None
        )
    return out


def build(a: Arm, b: Arm, args: argparse.Namespace) -> dict:
    findings = [*audit_arm(a), *audit_arm(b), *audit_pair(a, b)]
    errors = [f for f in findings if f.severity == "error"]
    first = _one(a, b, args.metric, args.counting, args)
    report = {
        "command": "gate",
        "metric": args.metric,
        "baseline": a.name,
        "candidate": b.name,
        "question": args.question,
        "level": args.level,
        "margin": args.margin,
        "require_gain": args.require_gain,
        "lower_is_better": args.lower_is_better,
        **first,
        "other_counting": None,
        "findings": [f.as_dict() for f in findings],
    }
    reasons = {
        "ok": "the interval is above the limit",
        "regressed": "the interval is below the limit",
        "undecided": "the interval contains the limit: run more trials",
    }
    report["reason"] = reasons[first["decision"]]
    if first["interval"] is None:
        report["reason"] = (
            "the noise is not measured: it needs 2 or more trials per task and trials that disagree"
        )
    elif first["decision"] == "undecided":
        low, high = first["interval"]
        oriented = (-high, -low) if args.lower_is_better else (low, high)
        if decide(*oriented, args.margin, args.require_gain) != "undecided":
            report["reason"] = "the test does not agree with the interval: run more trials"
    if args.metric == "reward" and not args.one_rule:
        try:
            second = _one(a, b, args.metric, other(args.counting), args)
        except ValueError:
            second = None
        report["other_counting"] = second
        if second and second["decision"] != first["decision"]:
            report["decision"] = "undecided"
            report["reason"] = (
                f"the decision depends on the counting rule: {first['decision']} when "
                f"{counting_text(first['trials'])}, {second['decision']} when {counting_text(second['trials'])}"
            )
    if errors and not args.allow_errors:
        report["decision"], report["reason"] = "invalid", "the audit reports errors"
    return report


def text(report: dict) -> str:
    _, diff, unit = value_format(report["metric"])
    pair = report["interval"]
    shown = f"{diff(pair[0])} to {diff(pair[1])}{unit}" if pair else "n/a"
    rule = (
        "the candidate must be better"
        if report["require_gain"]
        else f"acceptable drop: {unsigned(diff(report['margin']))}{unit}"
    )
    out = [
        f"gate: {report['decision'].upper()} ({report['reason']})",
        f"candidate - baseline = {diff(report['difference'])}{unit}, {100 * report['level']:.0f}% interval {shown} "
        f"({report['question']}), on {report['shared_tasks']} shared tasks, {counting_text(report['trials'])}",
        f"rule: {rule}{' · lower is better' if report['lower_is_better'] else ''}",
    ]
    if report["attempts_to_resolve"]:
        out.append(
            f"a true difference of this size needs about {report['attempts_to_resolve']} attempts per task in each arm"
        )
    elif report["decision"] == "undecided" and pair:
        out.append("the arms are too close to separate with a practical number of trials")
    listing = findings_text(report["findings"])
    if listing:
        out += ["audit:", listing]
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    a, b = load(args)
    report = build(a, b, args)
    print(render.to_json(report) if args.json else text(report))
    return EXIT[report["decision"]]
