from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from itertools import combinations
from statistics import fmean

from tidegauge import render, stats
from tidegauge.audit import SEVERITIES, Finding, audit_arm, check_conditions
from tidegauge.commands._common import (
    NAME_WIDTH,
    add_common,
    counting_text,
    detectable_text,
    findings_text,
    is_rate,
    load,
    other,
    scope_text,
    unsigned,
    value_format,
)
from tidegauge.model import Arm, Counting

DESCRIPTION = """``tidegauge decompose``: how much of a team's gain comes from more agents, and how much from coordination?

Give 3 arms in this order:

  solo   1 agent
  indep  a team whose agents cannot communicate
  team   the same team, with communication

Then  team - solo = (indep - solo) + (team - indep)
      total       = headcount      + coordination

The names of the arms are free: the position gives the role. Tasks pair by name (or by --task-key),
and only the tasks that all 3 arms have are used, so the 2 parts add up exactly.

Headcount and coordination share the arm indep, so their 2 estimates are negatively correlated.
Each interval is valid on its own. The table makes no joint claim.
"""
HELP = "split the gain of a team into a headcount effect and a coordination effect"
ROLES = ("solo", "indep", "team")
EFFECTS = {"headcount": ("solo", "indep"), "coordination": ("indep", "team"), "total": ("solo", "team")}
QUESTIONS = ("rerun", "tasks")
SHOWN_NOTES = ("attempts-mismatch", "seat-errors", "unmatched-task-key")
LEGEND = (
    "seats: agents in 1 trial · $/trial: mean cost of 1 trial · cost ratio: $/trial over the $/trial of solo",
    "headcount = indep - solo · coordination = team - indep · total = team - solo = headcount + coordination",
    "rerun: the same tasks run again · tasks: a new sample of similar tasks · resolved: p < {alpha:g} for that question",
    "For tasks, the paired t-test (p) and the sign-flip test (sign-flip p) must both give p < {alpha:g}.",
    "Headcount and coordination share the arm indep, so their 2 estimates are negatively correlated.",
    "Each interval is valid on its own. The table makes no joint claim.",
)
WORDS = {
    "headcount": ("More agents without communication add {}", "More agents without communication remove {}"),
    "coordination": (
        "Communication adds {} to the same agents",
        "Communication removes {} from the same agents",
    ),
    "total": (
        "The team with communication is {} above 1 agent",
        "The team with communication is {} below 1 agent",
    ),
}


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "paths",
        nargs=3,
        metavar="PATH",
        help="solo, then indep, then team: 1 agent, the team without communication, the team with "
        "communication. Use NAME=PATH to name an arm",
    )
    add_common(parser)


def _answer(
    se: float | None,
    interval: stats.Interval | None,
    p: float | None,
    mde: float | None,
    alpha: float,
    decides: float | None,
) -> dict:
    return {
        "se": se,
        "interval": [interval.low, interval.high] if interval else None,
        "p": p,
        "verdict": "cannot tell" if decides is None else "resolved" if decides < alpha else "not resolved",
        "detectable": mde,
    }


def _macro(values: Mapping[str, Sequence[float]], tasks: Iterable[str]) -> float | None:
    means = [fmean(values[task]) for task in tasks if task in values]
    return fmean(means) if means else None


def _shared(arms: Sequence[Arm], metric: str, counting: Counting) -> tuple[list[dict], dict[str, dict]]:
    raw = [arm.outcomes(metric, counting) for arm in arms]
    shared = sorted(set(raw[0]) & set(raw[1]) & set(raw[2]))
    if not shared:
        raise ValueError("the 3 arms share no task with counted trials. Tasks pair by name, or by --task-key")
    return raw, {role: {task: o[task] for task in shared} for role, o in zip(ROLES, raw, strict=True)}


def _effects(outs: Mapping[str, stats.Outcomes], level: float, alpha: float, seed: int) -> dict[str, dict]:
    effects: dict[str, dict] = {}
    differences: dict[str, float] = {}
    for name, (baseline, candidate) in EFFECTS.items():
        c = stats.compare(outs[baseline], outs[candidate], seed=seed)
        rerun = _answer(c.se_rerun, c.ci_rerun(level), c.rerun_p(), c.mde_rerun(alpha), alpha, c.rerun_p())
        tasks = _answer(c.se_tasks, c.ci_tasks(level), c.p_tasks, c.mde_tasks(alpha), alpha, c.tasks_p())
        differences[name] = c.delta
        flip = {"p_signflip": c.p_signflip, "signflip_exact": c.signflip_exact}
        effects[name] = {
            "baseline": baseline,
            "candidate": candidate,
            "difference": c.delta,
            "rerun": rerun | {"test": c.rerun_test},
            "tasks": tasks | flip,
        }
    gap = differences["total"] - differences["headcount"] - differences["coordination"]
    scale = max(1.0, *(abs(x) for o in outs.values() for xs in o.values() for x in xs))
    assert abs(gap) <= 1e-12 * scale, f"total is not headcount + coordination: the gap is {gap}"
    return effects


def _design(arms: Sequence[Arm], outs: Mapping[str, dict], dropped: Mapping[str, list[str]]) -> list[Finding]:
    findings = []
    shared = outs["solo"]
    if dropped:
        names = ", ".join(list(dropped)[:5]) + (" and more" if len(dropped) > 5 else "")
        message = (
            f"{len(dropped)} tasks do not have counted trials in all 3 arms and are left out: {names}. "
            f"The decomposition uses the {len(shared)} tasks that all 3 arms have."
        )
        detail = {"count": len(dropped), "missing_in": dict(list(dropped.items())[:20])}
        findings.append(Finding("task-set-mismatch", "warn", message, None, detail))
    uneven = sum(1 for task in shared if len({len(o[task]) for o in outs.values()}) > 1)
    if uneven:
        message = (
            f"{uneven} of {len(shared)} shared tasks have different numbers of counted trials in the 3 arms."
        )
        findings.append(Finding("attempts-mismatch", "note", message, None, {"tasks": uneven}))
    for a, b in combinations(arms, 2):
        findings.extend(check_conditions(a, b))
    return findings


def build(arms: Sequence[Arm], metric: str, counting: Counting, level: float, seed: int = 0) -> dict:
    alpha = round(1.0 - level, 12)
    raw, outs = _shared(arms, metric, counting)
    shared = sorted(outs["solo"])
    effects = _effects(outs, level, alpha, seed)
    left_out = sorted(set().union(*(arm.by_task() for arm in arms)) - set(shared))
    dropped = {t: [a.name for a, o in zip(arms, raw, strict=True) if t not in o] for t in left_out}
    costs = [{t: v for t, v in arm.outcomes("cost_usd").items() if t in outs["solo"]} for arm in arms]
    rows = []
    for role, arm, cost in zip(ROLES, arms, costs, strict=True):
        s = stats.score(outs[role])
        rerun, tasks = s.ci_rerun(level), s.ci_tasks(level)
        seats = [len(t.seats) or 1 for t in arm.trials if t.task in outs["solo"]]
        assert seats
        both = [task for task in shared if task in cost and task in costs[0]]
        base = _macro(costs[0], both)
        ratio = None
        if base:
            paired = _macro(cost, both)
            assert paired is not None
            ratio = paired / base
        rows.append(
            {
                "role": role,
                "name": arm.name,
                "seats": [min(seats), max(seats)],
                "tasks": s.n_tasks,
                "trials": s.n_trials,
                "score": s.mean,
                "ci_rerun": [rerun.low, rerun.high] if rerun else None,
                "ci_tasks": [tasks.low, tasks.high] if tasks else None,
                "cost_per_trial": _macro(cost, shared),
                "trials_with_cost": sum(len(values) for values in cost.values()),
                "trials_on_shared_tasks": len(seats),
                "cost_ratio": ratio,
            }
        )
    total = effects["total"]
    share = None
    if total["rerun"]["verdict"] == "resolved" and total["difference"]:
        share = effects["coordination"]["difference"] / total["difference"]
    report = {
        "command": "decompose",
        "metric": metric,
        "trials": str(counting),
        "level": level,
        "alpha": alpha,
        "shared_tasks": len(shared),
        "dropped_tasks": dropped,
        "arms": rows,
        "effects": effects,
        "coordination_share": share,
        "other_counting": None,
    }
    if metric == "reward":
        try:
            alt_outs = _shared(arms, metric, other(counting))[1]
            alt = effects if alt_outs == outs else _effects(alt_outs, level, alpha, seed)
            brief = {
                n: {"difference": e["difference"], "verdict": e["rerun"]["verdict"]} for n, e in alt.items()
            }
            report["other_counting"] = {"trials": str(other(counting)), "effects": brief}
        except ValueError:
            pass
    findings = [*(f for arm in arms for f in audit_arm(arm)), *_design(arms, outs, dropped)]
    report["findings"] = [f.as_dict() for f in findings]
    return report


def _where(effect: dict) -> str:
    yes = [q for q in QUESTIONS if effect[q]["verdict"] == "resolved"]
    no = [q for q in QUESTIONS if q not in yes]
    if not yes:
        return "not resolved"
    return f"resolved for {' and '.join(yes)}" + (f", not for {no[0]}" if no else "")


def _arm_rows(
    arms: Sequence[dict], value: Callable[[float | None], str], *, rate: bool
) -> list[list]:
    dollars = value_format("cost_usd")[0]

    def spread(interval: list[float] | None) -> str:
        if interval is None:
            return "n/a"
        half = (interval[1] - interval[0]) / 2.0
        return f"±{100 * half:.1f}" if rate else f"±{half:,.2f}"

    arm_rows = []
    for a in arms:
        low, high = a["seats"]
        part = "*" if a["trials_with_cost"] < a["trials_on_shared_tasks"] else ""
        label = a["role"] if a["name"] == a["role"] else f"{a['role']}: {a['name']}"
        arm_rows.append(
            [
                render.shorten(label, NAME_WIDTH),
                low if low == high else f"{low}-{high}",
                a["tasks"],
                a["trials"],
                value(a["score"]),
                spread(a["ci_rerun"]),
                spread(a["ci_tasks"]),
                "n/a" if a["cost_per_trial"] is None else f"${dollars(a['cost_per_trial'])}{part}",
                "n/a" if a["cost_ratio"] is None else f"{a['cost_ratio']:.2f}",
            ]
        )
    return arm_rows


def _effect_notes(report: dict, diff: Callable[[float | None], str], unit: str) -> list[str]:
    metric, effects = report["metric"], report["effects"]
    out = []
    for name, e in effects.items():
        size = unsigned(diff(e["difference"])) + (unit or f" {metric}")
        out.append(f"{WORDS[name][e['difference'] < 0].format(size)} ({name}, {_where(e)}).")
    share = report["coordination_share"]
    if share is None:
        out.append("The share of coordination is not defined: the total effect is not resolved for rerun.")
    else:
        opposite = "" if 0.0 <= share <= 1.0 else ": the 2 effects have opposite signs"
        caveat = "A share is a ratio of 2 estimates and has no interval."
        if effects["coordination"]["rerun"]["verdict"] != "resolved":
            caveat = "The coordination effect is not resolved, so its share can be 0%."
        gain = "gain" if effects["total"]["difference"] > 0 else "loss"
        out.append(
            f"Coordination gives {render.pct(share)} of the total {gain} and headcount gives "
            f"{render.pct(1.0 - share)}{opposite}. {caveat}"
        )
    detectable = {q: effects["coordination"][q]["detectable"] for q in QUESTIONS}
    sizes = [f"{detectable_text(mde, metric)}{unit} ({q})" for q, mde in detectable.items() if mde]
    if sizes:
        out.append(f"This design detects a true coordination effect of about {' or '.join(sizes)}.")
    if detectable["rerun"] is None:
        out.append(
            "The rerun noise of the coordination effect is not measured: it needs 2 or more trials per task "
            "and trials that disagree."
        )
    if report["shared_tasks"] < 2:
        out.append("The tasks question needs 2 or more shared tasks.")
    flat = [name for name, e in effects.items() if e["tasks"]["se"] == 0.0]
    if flat:
        out.append(
            f"The tasks interval of {', '.join(flat)} has width 0 because every task gives the same difference. "
            "More tasks can measure the spread between tasks."
        )
    weak = [name for name, e in effects.items() if e["rerun"]["test"].startswith("permutation")]
    if weak:
        out.append(
            f"rerun verdict of {', '.join(weak)}: the trial noise of the 2 arms cannot be compared, so the test "
            'checks only "no change on any task"'
        )
    return out


def text(report: dict) -> str:
    metric, effects = report["metric"], report["effects"]
    value, diff, unit = value_format(metric)
    percent = f"{100 * report['level']:g}%"
    level = percent + (" (pts)" if unit else "")

    def between(pair: list[float] | None) -> str:
        if pair is None:
            return "n/a"
        if is_rate(metric):
            pair = [max(-1.0, pair[0]), min(1.0, pair[1])]
        return f"{diff(pair[0])} to {diff(pair[1])}"

    arm_rows = _arm_rows(report["arms"], value, rate=bool(unit))
    effect_rows = []
    for name, e in effects.items():
        cells = [[between(e[q]["interval"]), render.pvalue(e[q]["p"]), e[q]["verdict"]] for q in QUESTIONS]
        cells[1].insert(2, render.pvalue(e["tasks"]["p_signflip"]))
        effect_rows.append([name, diff(e["difference"]) + unit, *cells[0], *cells[1]])
    mean = "score" if is_rate(metric) else "mean"
    arm_headers = f"arm,seats,tasks,trials,{mean},rerun,tasks,$/trial,cost ratio".split(",")
    effect_headers = f"effect,difference,rerun {level},p,verdict,tasks {level},p,sign-flip p,verdict"
    shared = report["shared_tasks"]
    out = [
        f"metric: {metric} · {shared} shared task{'s' if shared != 1 else ''} · "
        f"{scope_text(metric, report['trials'])} · {percent} intervals",
        "",
        render.table(arm_headers, arm_rows, right=range(1, 9)),
        "",
        render.table(effect_headers.split(","), effect_rows, right=(1, 3, 6, 7)),
        "",
        *(line.format(alpha=report["alpha"]) for line in LEGEND),
        "",
    ]
    out.extend(_effect_notes(report, diff, unit))
    if any("*" in row[7] for row in arm_rows):
        out.append("* only a part of the trials of this arm has a cost. The mean covers that part.")
    alt = report["other_counting"]
    if alt and any(
        e["verdict"] != effects[name]["rerun"]["verdict"]
        or abs(e["difference"] - effects[name]["difference"]) >= 0.005
        for name, e in alt["effects"].items()
    ):
        moved = ", ".join(
            f"{n} {diff(e['difference'])}{unit} ({e['verdict']})" for n, e in alt["effects"].items()
        )
        out.append(f"other counting rule ({counting_text(alt['trials'])}), rerun verdicts: {moved}")
    shown = [f for f in report["findings"] if f["severity"] != "note" or f["code"] in SHOWN_NOTES]
    listing = findings_text(shown, severities=SEVERITIES)
    if listing:
        out += ["", "audit:", listing]
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    report = build(load(args), args.metric, args.counting, args.level)
    print(render.to_json(report) if args.json else text(report))
    return 0
