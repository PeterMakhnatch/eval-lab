"""``tidegauge score``: the score of each arm with both of its error bars."""

from __future__ import annotations

import argparse

from tidegauge import render, stats
from tidegauge.audit import audit_arm
from tidegauge.commands._common import (
    NAME_WIDTH,
    add_arms,
    add_common,
    counting_text,
    findings_text,
    load,
    other,
    scope_text,
    value_format,
)
from tidegauge.model import Arm, Counting

HELP = "score each arm, with rerun and task error bars"


def configure(parser: argparse.ArgumentParser) -> None:
    add_arms(parser)
    add_common(parser)


def arm_report(arm: Arm, metric: str, counting: Counting, level: float) -> dict:
    outcomes = arm.outcomes(metric, counting)
    report: dict = {"name": arm.name, "tasks": 0, "trials": 0, "score": None}
    if not outcomes:
        return report
    s = stats.score(outcomes)
    rerun, tasks = s.ci_rerun(level), s.ci_tasks(level)
    report.update(
        tasks=s.n_tasks,
        trials=s.n_trials,
        attempts=[s.attempts_min, s.attempts_max],
        score=s.mean,
        mean_over_trials=s.micro,
        se_rerun=s.se_rerun,
        ci_rerun=[rerun.low, rerun.high] if rerun else None,
        se_tasks=s.se_tasks,
        ci_tasks=[tasks.low, tasks.high] if tasks else None,
        noise_floor=s.noise_floor(level),
        mixed_tasks=s.mixed_tasks,
        not_valid=sum(1 for t in arm.trials if not t.valid),
    )
    if metric == "reward":
        alt = arm.outcomes(metric, other(counting))
        report["score_other_counting"] = stats.score(alt).mean if alt else None
    return report


def build(arms: list[Arm], metric: str, counting: Counting, level: float) -> dict:
    findings = [f for arm in arms for f in audit_arm(arm)]
    return {
        "command": "score",
        "metric": metric,
        "trials": str(counting),
        "level": level,
        "arms": [arm_report(arm, metric, counting, level) for arm in arms],
        "findings": [f.as_dict() for f in findings],
    }


def _half(interval: list[float] | None) -> float | None:
    return None if interval is None else (interval[1] - interval[0]) / 2.0


def text(report: dict) -> str:
    value, _, unit = value_format(report["metric"])

    def spread(v: float | None) -> str:
        if v is None:
            return "n/a"
        return f"±{100 * v:.1f}" if unit else f"±{v:,.2f}"

    rows = []
    for a in report["arms"]:
        name = render.shorten(a["name"], NAME_WIDTH)
        if a["score"] is None:
            rows.append([name, 0, 0, "n/a", "n/a", "n/a", "n/a", ""])
            continue
        uneven = a["attempts"][0] != a["attempts"][1]
        rows.append(
            [
                name,
                a["tasks"],
                f"{a['trials']}*" if uneven else a["trials"],
                value(a["score"]),
                spread(_half(a["ci_rerun"])),
                spread(_half(a["ci_tasks"])),
                spread(a["noise_floor"]).lstrip("±"),
                a["not_valid"] or "",
            ]
        )
    out = [
        f"metric: {report['metric']} · {scope_text(report['metric'], report['trials'])} · "
        f"{100 * report['level']:.0f}% intervals",
        "",
        render.table(
            ["arm", "tasks", "trials", "score", "rerun", "tasks", "noise floor", "not valid"],
            rows,
            right=(1, 2, 3, 4, 5, 6, 7),
        ),
        "",
        "rerun        how far the score moves when the same tasks run again",
        "tasks        how far the score moves on a new sample of similar tasks",
        "noise floor  2 identical runs differ by less than this, 19 times in 20",
    ]
    if any(str(row[2]).endswith("*") for row in rows):
        out.append("*            tasks have different numbers of trials; each task has equal weight")
    moved = [
        a
        for a in report["arms"]
        if a.get("score_other_counting") is not None and abs(a["score_other_counting"] - a["score"]) >= 0.005
    ]
    if moved:
        out += ["", f"With the other counting rule ({counting_text(other(Counting(report['trials'])))}):"]
        out += [f"  {a['name']}: {value(a['score'])} -> {value(a['score_other_counting'])}" for a in moved]
    listing = findings_text(report["findings"])
    if listing:
        out += ["", "audit:", listing]
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    report = build(load(args), args.metric, args.counting, args.level)
    print(render.to_json(report) if args.json else text(report))
    return 0
