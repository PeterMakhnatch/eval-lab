from __future__ import annotations

import argparse
from collections.abc import Callable

from tidegauge import render, stats
from tidegauge.audit import audit_arm, audit_pair
from tidegauge.commands._common import (
    add_common,
    counting_text,
    detectable_text,
    findings_text,
    is_rate,
    load,
    other,
    scope_text,
    value_format,
)
from tidegauge.model import Arm, Counting

DESCRIPTION = """``tidegauge compare``: is the difference between 2 arms real?"""
HELP = "compare a candidate arm with a baseline arm, task by task"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths", nargs=2, metavar="PATH", help="baseline first, then candidate. Use NAME=PATH to name an arm"
    )
    add_common(parser)
    parser.add_argument(
        "--alpha", type=float, default=0.05, help="significance level of the verdicts (default: 0.05)"
    )
    parser.add_argument("--tasks", action="store_true", help="list the tasks where the arms differ most")


def verdict(delta: float, p: float | None, alpha: float) -> str:
    if p is None:
        return "cannot tell"
    if p >= alpha:
        return "not resolved"
    return "candidate is higher" if delta > 0 else "baseline is higher"


def build(a: Arm, b: Arm, metric: str, counting: Counting, level: float, alpha: float, seed: int = 0) -> dict:
    out_a, out_b = a.outcomes(metric, counting), b.outcomes(metric, counting)
    c = stats.compare(out_a, out_b, seed=seed)
    rerun, tasks = c.ci_rerun(level), c.ci_tasks(level)
    report = {
        "command": "compare",
        "metric": metric,
        "trials": str(counting),
        "level": level,
        "alpha": alpha,
        "baseline": a.name,
        "candidate": b.name,
        "shared_tasks": c.n_tasks,
        "tasks_only_baseline": len(set(out_a) - set(out_b)),
        "tasks_only_candidate": len(set(out_b) - set(out_a)),
        "score_baseline": c.mean_a,
        "score_candidate": c.mean_b,
        "difference": c.delta,
        "rerun": {
            "se": c.se_rerun,
            "interval": [rerun.low, rerun.high] if rerun else None,
            "p": c.rerun_p(),
            "test": c.rerun_test,
            "degrees_of_freedom": c.df_rerun,
            "p_t": c.p_rerun,
            "p_permutation": c.p_permutation,
            "balanced": c.balanced,
            "verdict": verdict(c.delta, c.rerun_p(), alpha),
            "detectable": c.mde_rerun(alpha),
        },
        "tasks": {
            "se": c.se_tasks,
            "interval": [tasks.low, tasks.high] if tasks else None,
            "p": c.p_tasks,
            "test": "paired t and sign-flip over tasks",
            "p_signflip": c.p_signflip,
            "signflip_exact": c.signflip_exact,
            "verdict": verdict(c.delta, c.tasks_p(), alpha),
            "detectable": c.mde_tasks(alpha),
        },
        "per_task": {
            "candidate_higher": c.b_better,
            "baseline_higher": c.a_better,
            "equal": c.ties,
            "candidate_higher_every_trial": c.b_better_unanimous,
            "baseline_higher_every_trial": c.a_better_unanimous,
        },
        "task_differences": dict(sorted(c.task_deltas.items(), key=lambda kv: (-abs(kv[1]), kv[0]))),
        "other_counting": None,
    }
    if metric == "reward":
        alt_rule = other(counting)
        try:
            alt = stats.compare(a.outcomes(metric, alt_rule), b.outcomes(metric, alt_rule), seed=seed)
            report["other_counting"] = {
                "trials": str(alt_rule),
                "difference": alt.delta,
                "verdict": verdict(alt.delta, alt.rerun_p(), alpha),
            }
        except ValueError:
            pass
    findings = [*audit_arm(a), *audit_arm(b), *audit_pair(a, b)]
    report["findings"] = [f.as_dict() for f in findings]
    return report


def _notes(report: dict, diff: Callable[[float | None], str], unit: str) -> list[str]:
    rerun, tasks = report["rerun"], report["tasks"]
    out = []
    if tasks["se"] == 0.0:
        out.append(
            "the tasks interval has width 0 because every task gives the same difference: "
            "more tasks can measure the spread between tasks"
        )
    if rerun["test"].startswith("permutation"):
        out.append(
            "rerun verdict: the trial noise of the 2 arms cannot be compared, so the test checks only "
            '"no change on any task"'
        )
    left_out = report["tasks_only_baseline"] + report["tasks_only_candidate"]
    if left_out:
        out.append(
            f"{left_out} tasks are left out: {report['tasks_only_baseline']} have counted trials only in the "
            f"baseline and {report['tasks_only_candidate']} only in the candidate"
        )
    if rerun["detectable"] is None:
        out.append("rerun noise is not measured: it needs 2 or more trials per task and trials that disagree")
    else:
        metric = report["metric"]
        line = f"this design detects a true difference of about {detectable_text(rerun['detectable'], metric)}{unit} (rerun)"
        if tasks["detectable"] is not None:
            line += f" or {detectable_text(tasks['detectable'], metric)}{unit} (tasks)"
        out.append(line)
    alt = report["other_counting"]
    if alt and (alt["verdict"] != rerun["verdict"] or abs(alt["difference"] - report["difference"]) >= 0.005):
        out.append(
            f"other counting rule ({counting_text(alt['trials'])}): difference {diff(alt['difference'])}{unit}, "
            f"rerun verdict: {alt['verdict']}"
        )
    return out


def text(report: dict, *, show_tasks: bool = False) -> str:
    value, diff, unit = value_format(report["metric"])

    def interval(pair: list[float] | None) -> str:
        if pair is None:
            return "n/a"
        if is_rate(report["metric"]):
            pair = [max(-1.0, pair[0]), min(1.0, pair[1])]
        return f"{diff(pair[0])} to {diff(pair[1])}{unit}"

    rerun, tasks, per = report["rerun"], report["tasks"], report["per_task"]
    out = [
        f"baseline:  {report['baseline']}",
        f"candidate: {report['candidate']}",
        f"metric: {report['metric']} · {report['shared_tasks']} shared tasks · "
        f"{scope_text(report['metric'], report['trials'])}",
        "",
        render.table(
            ["", "baseline", "candidate", "difference"],
            [
                [
                    "score" if is_rate(report["metric"]) else "mean",
                    value(report["score_baseline"]),
                    value(report["score_candidate"]),
                    diff(report["difference"]) + unit,
                ]
            ],
            right=(1, 2, 3),
        ),
        "",
        render.table(
            ["question", f"{100 * report['level']:.0f}% interval", "p", "sign-flip p", "verdict"],
            [
                [
                    "rerun: same tasks again",
                    interval(rerun["interval"]),
                    render.pvalue(rerun["p"]),
                    "",
                    rerun["verdict"],
                ],
                [
                    "tasks: new similar tasks",
                    interval(tasks["interval"]),
                    render.pvalue(tasks["p"]),
                    render.pvalue(tasks["p_signflip"]),
                    tasks["verdict"],
                ],
            ],
            right=(2, 3),
        ),
        "",
        f"per task: candidate higher on {per['candidate_higher']} ({per['candidate_higher_every_trial']} in every "
        f"trial), baseline higher on {per['baseline_higher']} ({per['baseline_higher_every_trial']} in every trial), "
        f"{per['equal']} equal",
        f"tasks verdict: the paired t-test (p) and the sign-flip test (sign-flip p) must both give p < {report['alpha']:g}",
    ]
    out.extend(_notes(report, diff, unit))
    if show_tasks:
        rows = [[task, diff(d)] for task, d in list(report["task_differences"].items())[:20] if d != 0]
        if rows:
            out += ["", render.table(["task", "candidate - baseline"], rows, right=(1,))]
    listing = findings_text(report["findings"])
    if listing:
        out += ["", "audit:", listing]
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    a, b = load(args)
    report = build(a, b, args.metric, args.counting, args.level, args.alpha)
    print(render.to_json(report) if args.json else text(report, show_tasks=args.tasks))
    return 0
