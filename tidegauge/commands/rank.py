from __future__ import annotations

import argparse

from tidegauge import render, stats
from tidegauge.commands._common import (
    NAME_WIDTH,
    add_arms,
    add_common,
    load,
    scope_text,
    unsigned,
    value_format,
)
from tidegauge.model import Arm, Counting

DESCRIPTION = """``tidegauge rank``: which positions in a ranking would survive a rerun?"""
HELP = "rank many arms and show which ranks are separated from rerun noise"


def configure(parser: argparse.ArgumentParser) -> None:
    add_arms(parser)
    add_common(parser)
    parser.add_argument(
        "--alpha", type=float, default=0.05, help="significance level for 'separated' (default: 0.05)"
    )
    parser.add_argument(
        "--all-tasks",
        action="store_true",
        help="score each arm on its own tasks, not only on the tasks that all arms share",
    )


def build(
    arms: list[Arm],
    metric: str,
    counting: Counting,
    level: float,
    alpha: float,
    *,
    shared_only: bool = True,
    seed: int = 0,
) -> dict:
    per_arm = {arm.name: arm.outcomes(metric, counting) for arm in arms}
    shared = None
    if shared_only:
        shared = set.intersection(*(set(o) for o in per_arm.values())) if per_arm else set()
        per_arm = {name: {t: v for t, v in o.items() if t in shared} for name, o in per_arm.items()}
    outcomes = {name: o for name, o in per_arm.items() if o}
    ranked = stats.rank(outcomes, level=level, seed=seed)
    rows = []
    for index, r in enumerate(ranked):
        row = {
            "rank": r.rank,
            "name": r.name,
            "score": r.score.mean,
            "se_rerun": r.score.se_rerun,
            "rank_interval": [r.rank_low, r.rank_high],
            "noise_borrowed": r.noise_borrowed,
            "p_first": r.p_first,
            "gap_to_next": None,
            "p_next": None,
            "separated_from_next": None,
        }
        if index + 1 < len(ranked):
            below = ranked[index + 1]
            c = stats.compare(outcomes[below.name], outcomes[r.name], seed=seed)
            p = c.rerun_p()
            row.update(
                gap_to_next=abs(c.delta), p_next=p, separated_from_next=None if p is None else p < alpha
            )
        rows.append(row)
    known = [r for r in rows if r["separated_from_next"] is not None]
    return {
        "command": "rank",
        "metric": metric,
        "trials": str(counting),
        "level": level,
        "alpha": alpha,
        "shared_tasks": len(shared) if shared is not None else None,
        "arms": rows,
        "neighbor_pairs": len(known),
        "neighbor_pairs_separated": sum(1 for r in known if r["separated_from_next"]),
    }


def text(report: dict) -> str:
    value, diff, _ = value_format(report["metric"])
    rows = []
    for r in report["arms"]:
        low, high = r["rank_interval"]
        separated = {None: "", True: "yes", False: "no"}[r["separated_from_next"]]
        rows.append(
            [
                r["rank"],
                render.shorten(r["name"], NAME_WIDTH),
                value(r["score"]),
                (f"{low}-{high}" if low != high else str(low)) + ("~" if r["noise_borrowed"] else ""),
                f"{100 * r['p_first']:.0f}%" if r["p_first"] is not None and r["p_first"] >= 0.005 else "",
                unsigned(diff(r["gap_to_next"])) if r["gap_to_next"] is not None else "",
                separated,
            ]
        )
    scope = (
        f"{report['shared_tasks']} tasks that all arms share"
        if report["shared_tasks"] is not None
        else "each arm on its own tasks"
    )
    out = [
        f"metric: {report['metric']} · {scope} · {scope_text(report['metric'], report['trials'])}",
        "",
        render.table(
            [
                "rank",
                "arm",
                "score",
                f"rank {100 * report['level']:.0f}%",
                "P(first)",
                "gap to next",
                "separated",
            ],
            rows,
            right=(0, 2, 3, 4, 5),
        ),
        "",
        f"{report['neighbor_pairs_separated']} of {report['neighbor_pairs']} neighbor pairs "
        f"{'is' if report['neighbor_pairs_separated'] == 1 else 'are'} separated from "
        f"rerun noise (p < {report['alpha']}).",
        f"rank {100 * report['level']:.0f}%: where the arm lands when every arm runs the same tasks again.",
    ]
    if any(r["noise_borrowed"] for r in report["arms"]):
        out.append(
            "~ the arm shows no trial noise of its own (1 trial per task, or no task with mixed outcomes). "
            "Its interval uses the mean trial noise of the other arms, and its 'separated' mark tests only "
            '"no change on any task".'
        )
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    arms = load(args)
    if len(arms) < 2:
        raise SystemExit("rank needs 2 or more arms. Give several paths, or 1 path with --split")
    report = build(arms, args.metric, args.counting, args.level, args.alpha, shared_only=not args.all_tasks)
    print(render.to_json(report) if args.json else text(report))
    return 0
