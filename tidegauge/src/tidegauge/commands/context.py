"""``tidegauge context``: where do the input tokens of an agent run go?

An agent loop sends its whole context again with every model call. This command
reads the ATIF trajectory of each trial (``agent/trajectory.json``) and splits
the prompt tokens of each call in 2 parts: new tokens (the growth over the call
before, ``fresh_tokens`` in the JSON report) and re-sent tokens (the rest).
Context management and prompt caching can reduce only the re-sent part.
The split uses token counts. It compares no text.

A seat trial has 1 trajectory per seat (``seats/<seat>/agent/trajectory.json``).
Its numbers add up the calls of its seats. Its peak is the largest prompt of 1 seat.
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter
from collections.abc import Iterator, Sequence
from itertools import pairwise

from tidegauge import render
from tidegauge.atif import Trajectory, read_trajectory
from tidegauge.audit import SEVERITIES, Finding
from tidegauge.commands._common import NAME_WIDTH, add_arms, add_task_key, findings_text, load
from tidegauge.model import Arm, Trial

HELP = "show where the input tokens go: model calls, peak context, re-sent and cached shares, cost per pass"

# A prompt below this share of the prompt before it is a context drop: prompts grow in a normal loop.
DROP_BELOW = 0.5
# Token sums that differ by more than this share are a finding: rounding stays far below 1%.
USAGE_TOLERANCE = 0.01

# An adapter that adds the cached tokens to a prompt count that already holds them leaves a mark:
# prompt minus cached of one call equals the cached tokens of the next call. Above this share of
# call pairs, the arm gets a finding.
TWICE_SHARE = 0.9
TWICE_TOLERANCE = 0.02

HEADERS = "arm,trials,usable,calls,input,peak,re-sent,cached,drops,$/trial,$/pass,s/call"
LEGEND = (
    "usable   trials whose trajectory gives the prompt tokens of its model calls. Only these are counted",
    "calls    model calls in the median trial",
    "input    prompt tokens of the median trial, added over its calls",
    "peak     largest prompt of 1 call, in the median trial",
    "re-sent  share of the input tokens that repeat the prompt of the call before. Only growth is new",
    "cached   share of the input tokens that the provider read from its cache",
    "drops    trials where a prompt is less than half of the prompt before: compaction, summary or reset",
    "$/trial  cost of the median trial. $/pass: cost of all trials divided by the number of passed trials",
    "s/call   median seconds from 1 call to the next",
    "Token counts are the numbers that the adapter of each agent reports. Adapters do not all count alike.",
)
SEATS_LEGEND = (
    "A trial with several seats adds up the calls of its seats. Its peak is the largest prompt of 1 seat."
)
NOT_COVERED = "The token figures do not cover them."


def configure(parser: argparse.ArgumentParser) -> None:
    add_arms(parser)
    parser.add_argument("--reward-key", help="name of the reward to use when a verifier writes several")
    add_task_key(parser)
    parser.add_argument("--json", action="store_true", help="print the report as JSON, with 1 row per trial")


def read_trial(trial: Trial) -> list[Trajectory | None]:
    """The trajectory of each agent of a trial: 1 file, or 1 file per seat of a seat trial."""
    return [read_trajectory(trial.path / "seats" / seat) for seat in trial.seats] or [
        read_trajectory(trial.path)
    ]


def state(trajectory: Trajectory | None) -> str:
    """What 1 trajectory gives: ``usable``, ``missing``, ``unreadable`` or ``no-call-metrics``."""
    if trajectory is None:
        return "missing"
    if trajectory.problem:
        return "unreadable"
    return "usable" if trajectory.calls else "no-call-metrics"


def trial_state(trial: Trial, found: Sequence[Trajectory | None]) -> str:
    """What the trajectories of a trial give. For a seat trial: the worst state of a seat that used a model.

    A seat trial is usable only when every seat that used a model has usable calls: numbers that
    cover a part of a team are not the numbers of the trial. A seat without a model needs no file.
    """
    states = [state(trajectory) for trajectory in found]
    needed = [s for seat, s in zip(trial.seats, states, strict=False) if seat in trial.model_seats] or states
    return next((s for s in ("unreadable", "missing", "no-call-metrics") if s in needed), "usable")


def usable_runs(found: Sequence[Trajectory | None]) -> list[Trajectory]:
    """The trajectories with usable calls: 1 run per agent."""
    return [trajectory for trajectory in found if trajectory is not None and trajectory.calls]


def passed(trial: Trial) -> bool:
    return bool(trial.valid and trial.reward and trial.reward > 0)


def _ratio(part: float | None, whole: float) -> float | None:
    return None if part is None or not whole else part / whole


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def p90(values: Sequence[float]) -> float | None:
    """90th percentile by linear interpolation between the sorted values. 1 value is its own percentile."""
    if len(values) < 2:
        return float(values[0]) if values else None
    return statistics.quantiles(values, n=10, method="inclusive")[-1]


def _whole(parts: Sequence[float | None]) -> float | None:
    """The sum of the parts, or ``None`` when a part is missing or 0."""
    return sum(parts) if all(parts) else None


def cost_of(trial: Trial, runs: Sequence[Trajectory]) -> float | None:
    """Sum of the call costs when every call has one, else the trajectory total, else the cost in result.json.

    A cost of 0 is read as not reported: Harbor's own agents write no cost when they do not know the price.
    A seat trial adds up its seats when every seat gives a cost, else it takes the cost in result.json.
    """
    costs = [_whole([call.cost_usd for call in run.calls]) or run.total_cost_usd for run in runs]
    return _whole(costs) or trial.cost_usd or None


def trial_report(trial: Trial, found: Sequence[Trajectory | None]) -> dict:
    """The numbers of 1 trial. A trial without usable calls has only the ``trajectory`` state.

    A seat trial adds up the calls of its seats, and it has the state of each seat in ``seats``.
    A pair of calls is 2 calls in a row of 1 agent. The first and the peak context are the
    largest over the seats: each is the size of 1 prompt.
    """
    row: dict = {
        "trial": trial.trial_name,
        "task": trial.task,
        "passed": passed(trial),
        "trajectory": trial_state(trial, found),
    }
    if trial.seats:
        row["seats"] = {seat: state(t) for seat, t in zip(trial.seats, found, strict=True)}
    if row["trajectory"] != "usable":
        return row
    runs = usable_runs(found)
    calls = [call for run in runs for call in run.calls]
    pairs = [pair for run in runs for pair in pairwise(run.calls)]
    total = sum(call.prompt_tokens for call in calls)
    firsts = [run.calls[0].prompt_tokens for run in runs]
    fresh = sum(firsts) + sum(max(0, b.prompt_tokens - a.prompt_tokens) for a, b in pairs)
    reported = [call.cached_tokens for call in calls if call.cached_tokens is not None]
    cached = sum(reported) if reported else None
    output = [call.completion_tokens for call in calls if call.completion_tokens is not None]
    gaps = [(b.timestamp - a.timestamp).total_seconds() for a, b in pairs if a.timestamp and b.timestamp]
    row.update(
        calls=len(calls),
        input_tokens=total,
        output_tokens=sum(output) if output else None,
        first_context=max(firsts),
        peak_context=max(call.prompt_tokens for call in calls),
        fresh_tokens=fresh,
        resent_share=_ratio(total - fresh, total),
        cached_tokens=cached,
        cached_share=_ratio(cached, total),
        context_drops=sum(1 for a, b in pairs if b.prompt_tokens < DROP_BELOW * a.prompt_tokens),
        cost_usd=cost_of(trial, runs),
        seconds_per_call=_median(gaps),
    )
    return row


def arm_report(name: str, rows: list[dict]) -> dict:
    """The numbers of 1 arm. A share is pooled: sums of tokens over the usable trials, not a mean of shares."""
    usable = [row for row in rows if row["trajectory"] == "usable"]
    total = sum(row["input_tokens"] for row in usable)
    resent = total - sum(row["fresh_tokens"] for row in usable)
    # A trial without cached tokens adds 0. The share is unknown only when no trial reports cached tokens.
    cached = [row["cached_tokens"] for row in usable if row["cached_tokens"] is not None]
    # Cost per passed trial uses only the trials with a cost, in the sum and in the count of passed trials.
    priced = [row for row in usable if row["cost_usd"] is not None]
    passes = sum(1 for row in priced if row["passed"])
    seconds = [row["seconds_per_call"] for row in usable if row["seconds_per_call"] is not None]
    return {
        "name": name,
        "trials": len(rows),
        "usable": len(usable),
        "calls_median": _median([row["calls"] for row in usable]),
        "input_tokens_median": _median([row["input_tokens"] for row in usable]),
        "input_tokens_total": total if usable else None,
        "peak_context_median": _median([row["peak_context"] for row in usable]),
        "peak_context_p90": p90([row["peak_context"] for row in usable]),
        "resent_share": _ratio(resent, total),
        "cached_share": _ratio(sum(cached) if cached else None, total),
        "trials_with_drop": sum(1 for row in usable if row["context_drops"]) if usable else None,
        "trials_with_cost": len(priced),
        "cost_median": _median([row["cost_usd"] for row in priced]),
        "cost_per_pass": sum(row["cost_usd"] for row in priced) / passes if passes else None,
        "seconds_per_call_median": _median(seconds),
        "per_trial": rows,
    }


def check_trajectories(
    name: str, read: Sequence[tuple[Trial, Sequence[Trajectory | None]]]
) -> Iterator[Finding]:
    """Findings on the trajectories of 1 arm: files that give no numbers, and numbers that do not add up."""
    judged = [(trial, found, trial_state(trial, found)) for trial, found in read]
    states = Counter(verdict for _, _, verdict in judged)
    if states["missing"]:
        seats = any(trial.seats for trial, _, verdict in judged if verdict == "missing")
        yield Finding(
            "trajectory-missing",
            "note",
            f"{states['missing']} of {len(read)} trials have no agent/trajectory.json"
            f"{' for 1 or more seats' if seats else ''}. {NOT_COVERED}",
            name,
            {"count": states["missing"]},
        )
    broken = [t for _, found, verdict in judged if verdict == "unreadable" for t in found if t and t.problem]
    if broken:
        reasons = Counter(trajectory.problem for trajectory in broken)
        shown = ", ".join(f"{reason} ×{count}" for reason, count in reasons.most_common(3))
        yield Finding(
            "trajectory-unreadable",
            "warn",
            f"{states['unreadable']} of {len(read)} trials have a trajectory file that cannot be read as ATIF "
            f"({shown}). {NOT_COVERED}",
            name,
            {
                "count": states["unreadable"],
                "reasons": dict(reasons),
                "paths": [str(t.path) for t in broken[:10]],
            },
        )
    if states["no-call-metrics"]:
        yield Finding(
            "no-call-metrics",
            "note",
            f"{states['no-call-metrics']} of {len(read)} trials have an ATIF trajectory in which no model call "
            f"reports prompt tokens. {NOT_COVERED}",
            name,
            {"count": states["no-call-metrics"]},
        )
    usable = [(trial, usable_runs(found)) for trial, found, verdict in judged if verdict == "usable"]
    sums = [sum(call.prompt_tokens for run in runs for call in run.calls) for _, runs in usable]
    # A recorded total of 0 is read as not recorded, as the audit does. A team has a total when every seat has one.
    finals = [_whole([run.total_prompt_tokens for run in runs]) for _, runs in usable]
    totals = [(calls, final) for calls, final in zip(sums, finals, strict=True) if final]
    results = [
        (calls, trial.tokens_in) for calls, (trial, _) in zip(sums, usable, strict=True) if trial.tokens_in
    ]
    off = [
        sum(1 for calls, recorded in pairs if abs(calls - recorded) > USAGE_TOLERANCE * recorded)
        for pairs in (totals, results)
    ]
    held = [_ratio(sum(c for c, _ in pairs), sum(r for _, r in pairs)) for pairs in (totals, results)]
    if any(off):
        yield Finding(
            "usage-mismatch",
            "warn",
            f"In {off[0]} of {len(totals)} trials the prompt tokens of the calls differ by more than "
            f"{USAGE_TOLERANCE:.0%} from final_metrics in the trajectory. In {off[1]} of {len(results)} trials they "
            f"differ from the input tokens in result.json. The calls hold {render.pct(held[0])} and "
            f"{render.pct(held[1])} of those totals. The table uses the sums over the calls.",
            name,
            {
                "vs_final_metrics": off[0],
                "with_final_metrics": len(totals),
                "calls_share_of_final_metrics": held[0],
                "vs_result": off[1],
                "with_result": len(results),
                "calls_share_of_result": held[1],
            },
        )
    pairs = [
        (a, b)
        for _, runs in usable
        for run in runs
        for a, b in pairwise(run.calls)
        if a.cached_tokens and b.cached_tokens
    ]
    twice = sum(
        1
        for a, b in pairs
        if abs(a.prompt_tokens - a.cached_tokens - b.cached_tokens) <= TWICE_TOLERANCE * b.cached_tokens
    )
    if pairs and twice >= TWICE_SHARE * len(pairs):
        yield Finding(
            "cached-counted-twice",
            "warn",
            f"In {twice} of {len(pairs)} call pairs, prompt tokens minus cached tokens equal the cached tokens of the "
            "next call. An adapter leaves this mark when it adds cached tokens to a prompt count that already holds "
            "them. The token figures of this arm can be about 2 times too high.",
            name,
            {"pairs": len(pairs), "matching": twice},
        )
    over = [
        sum(1 for run in runs for call in run.calls if (call.cached_tokens or 0) > call.prompt_tokens)
        for _, runs in usable
    ]
    if any(over):
        yield Finding(
            "cached-exceeds-input",
            "warn",
            f"{sum(over)} calls in {sum(1 for count in over if count)} trials report more cached tokens than prompt "
            "tokens. ATIF defines cached tokens as a part of prompt tokens. These calls break that rule, so the "
            "token figures of this arm are not reliable.",
            name,
            {"calls": sum(over), "trials": sum(1 for count in over if count)},
        )


def build(arms: list[Arm]) -> dict:
    reports, findings = [], []
    for arm in arms:
        read = [(trial, read_trial(trial)) for trial in arm.trials]
        reports.append(arm_report(arm.name, [trial_report(trial, found) for trial, found in read]))
        findings.extend(check_trajectories(arm.name, read))
    return {"command": "context", "arms": reports, "findings": [f.as_dict() for f in findings]}


def _tokens(value: float | None) -> str:
    """A token count in short form: 950, 36.9k, 1.3M."""
    if value is None:
        return "n/a"
    for unit, size in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return f"{value:.0f}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def text(report: dict) -> str:
    rows = [
        [
            render.shorten(a["name"], NAME_WIDTH),
            a["trials"],
            a["usable"],
            render.number(a["calls_median"], 1),
            _tokens(a["input_tokens_median"]),
            _tokens(a["peak_context_median"]),
            render.pct(a["resent_share"]),
            render.pct(a["cached_share"]),
            "n/a" if a["trials_with_drop"] is None else a["trials_with_drop"],
            _usd(a["cost_median"]),
            _usd(a["cost_per_pass"]),
            render.number(a["seconds_per_call_median"], 1),
        ]
        for a in report["arms"]
    ]
    headers = HEADERS.split(",")
    out = [render.table(headers, rows, right=range(1, len(headers))), "", *LEGEND]
    if any("seats" in row for a in report["arms"] for row in a["per_trial"]):
        out.append(SEATS_LEGEND)
    listing = findings_text(report["findings"], severities=SEVERITIES)
    if listing:
        out += ["", "accounting:", listing]
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    report = build(load(args))
    print(render.to_json(report) if args.json else text(report))
    return 0
