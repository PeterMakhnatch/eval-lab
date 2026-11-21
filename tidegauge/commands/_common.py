from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Sequence

from tidegauge import render
from tidegauge.load import load_arms, rekey, split_arms
from tidegauge.model import METRICS, Arm, Counting

NAME_WIDTH = 44


def add_common(parser: argparse.ArgumentParser, *, metric: bool = True) -> None:
    parser.add_argument(
        "--trials",
        dest="counting",
        type=Counting,
        choices=list(Counting),
        default=Counting.VALID,
        help="which trials count. valid (default): only trials with a valid reward, as Harbor's job mean "
        "does. all: every trial, and a trial without a reward scores 0, as the Terminal-Bench leaderboard does",
    )
    parser.add_argument("--reward-key", help="name of the reward to use when a verifier writes several")
    add_task_key(parser)
    if metric:
        parser.add_argument(
            "--metric",
            default="reward",
            help=f"per-trial value to analyze (default: reward). One of: {', '.join(METRICS)}, or a reward name",
        )
    parser.add_argument(
        "--level", type=float, default=0.95, help="confidence level of the intervals (default: 0.95)"
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


def add_task_key(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--task-key",
        metavar="REGEX",
        help="pair tasks by the first group of REGEX in the task name, for names that hold a condition: "
        "'^(.*)-(?:solo|talk)$' gives schedule-009-solo and schedule-009-talk the key schedule-009. "
        "A trial whose name does not match keeps its key",
    )


def task_pattern(text: str | None) -> re.Pattern[str] | None:
    if text is None:
        return None
    try:
        pattern = re.compile(text)
    except re.error as error:
        raise ValueError(f"--task-key is not a valid regular expression: {error}") from None
    if not pattern.groups:
        raise ValueError("--task-key needs a capture group, as in '^(.*)-(?:solo|talk)$'")
    return pattern


def add_arms(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="a Harbor job directory, or any directory that holds trials. Use NAME=PATH to name the arm",
    )
    parser.add_argument(
        "--split", action="store_true", help="treat each child directory of the one PATH as its own arm"
    )


def load(args: argparse.Namespace) -> list[Arm]:
    pattern = task_pattern(getattr(args, "task_key", None))
    if getattr(args, "split", False):
        if len(args.paths) != 1:
            raise SystemExit("--split takes exactly 1 PATH")
        arms = split_arms(args.paths[0], args.reward_key)
    else:
        arms = load_arms(args.paths, args.reward_key)
    if pattern is not None:
        arms = [rekey(arm, pattern) for arm in arms]
    empty = [arm.name for arm in arms if not arm.trials]
    if empty or not arms:
        raise SystemExit(f"no trials found in: {', '.join(empty) or args.paths[0]}")
    metric = getattr(args, "metric", "reward")
    if metric not in METRICS and not any(metric in t.rewards for arm in arms for t in arm.trials):
        raise ValueError(f"unknown metric: {metric}. No trial has a reward with this name")
    return arms


def other(counting: Counting) -> Counting:
    return Counting.ALL if counting is Counting.VALID else Counting.VALID


def counting_text(counting: Counting | str) -> str:
    if Counting(counting) is Counting.VALID:
        return "only trials with a valid reward count"
    return "every trial counts, no reward means 0"


def is_rate(metric: str) -> bool:
    return metric not in METRICS or metric == "reward"


def _digits(metric: str) -> int:
    if metric.startswith("tokens"):
        return 0
    return 4 if metric == "cost_usd" else 1


def value_format(metric: str) -> tuple[Callable[[float | None], str], Callable[[float | None], str], str]:
    if is_rate(metric):
        return render.pct, render.pts, " pts"
    digits = _digits(metric)

    def value(v: float | None) -> str:
        return "n/a" if v is None else f"{v:,.{digits}f}"

    def difference(v: float | None) -> str:
        return "n/a" if v is None else f"{v + 0.0:+,.{digits}f}"

    return value, difference, ""


def scope_text(metric: str, counting: Counting | str) -> str:
    if metric == "reward":
        return counting_text(counting)
    return "trials that record this metric"


def unsigned(text: str) -> str:
    return text.lstrip("+-")


def detectable_text(value: float | None, metric: str) -> str:
    if value is None:
        return "n/a"
    if is_rate(metric) and value > 1.0:
        return "more than 100"
    return unsigned(value_format(metric)[1](value))


def findings_text(findings: Sequence[dict], *, severities: Sequence[str] = ("error", "warn")) -> str:
    lines = []
    for finding in findings:
        if finding["severity"] in severities:
            where = f"[{finding['arm']}] " if finding["arm"] else ""
            lines.append(f"  {finding['severity']:5s} {finding['code']}: {where}{finding['message']}")
    return "\n".join(lines)
