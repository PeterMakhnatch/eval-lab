from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tidegauge import render, stats
from tidegauge.health import EARLY_SHARE, STALL_SECONDS, SeatFault, trial_faults
from tidegauge.load import task_key
from tidegauge.model import Arm, Counting, Trial, distinct

SEVERITIES = ("error", "warn", "note")

CONTROL_AGENTS = frozenset({"oracle", "nop"})
COUNTING_MATTERS = 0.005


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    severity: str
    message: str
    arm: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "arm": self.arm,
            "message": self.message,
            "detail": self.detail,
        }


def _top(counter: Counter[Any], limit: int = 5) -> str:
    return ", ".join(f"{key} ×{count}" for key, count in counter.most_common(limit))


def _share(part: int, whole: int) -> str:
    assert 0 <= part <= whole
    return f"{part} of {whole} ({100 * part / whole:.1f}%)" if whole else f"{part} of 0"


def _own(trial: Trial) -> str:
    return task_key(trial.task_name)


def check_unreadable(arm: Arm) -> Iterator[Finding]:
    unreadable = [p for p in arm.problems if "did not finish" not in p.reason]
    if unreadable:
        yield Finding(
            "unreadable-result",
            "error",
            f"{len(unreadable)} result files cannot be read. Their trials are not counted.",
            arm.name,
            {
                "paths": [str(p.path) for p in unreadable[:10]],
                "reasons": dict(Counter(p.reason for p in unreadable)),
            },
        )


def check_unfinished(arm: Arm) -> Iterator[Finding]:
    unfinished = [p for p in arm.problems if "did not finish" in p.reason]
    if unfinished:
        yield Finding(
            "unfinished-trials",
            "error",
            f"{len(unfinished)} trial directories have no result.json. Those trials started and are not counted.",
            arm.name,
            {"paths": [str(p.path) for p in unfinished[:10]]},
        )


def check_invalid(arm: Arm) -> Iterator[Finding]:
    invalid = [t for t in arm.trials if not t.valid]
    if not invalid:
        return
    counted_all = stats.score(arm.outcomes(counting=Counting.ALL)).mean
    kept = arm.outcomes(counting=Counting.VALID)
    counted_valid = stats.score(kept).mean if kept else None
    reasons = Counter(t.exception or "no exception recorded" for t in invalid)
    if counted_valid is None:
        gap, moves = " No trial has a valid reward.", True
    else:
        gap = (
            f" The score is {100 * counted_valid:.1f}% over valid trials (Harbor's rule) and "
            f"{100 * counted_all:.1f}% when they count as failures (Terminal-Bench leaderboard rule)."
        )
        moves = abs(counted_valid - counted_all) >= COUNTING_MATTERS
    yield Finding(
        "invalid-trials",
        "warn" if moves else "note",
        f"{_share(len(invalid), len(arm.trials))} trials have no valid reward ({_top(reasons, 3)}).{gap}",
        arm.name,
        {
            "count": len(invalid),
            "by_exception": dict(reasons),
            "score_valid": counted_valid,
            "score_all": counted_all,
        },
    )


def check_jobs(arm: Arm) -> Iterator[Finding]:
    for job in arm.jobs:
        found = sum(1 for t in arm.trials if t.path.parent == job.path)
        if job.n_total_trials is not None and found < job.n_total_trials:
            yield Finding(
                "incomplete-job",
                "error",
                f"Job {job.path.name} planned {job.n_total_trials} trials and {found} have a result here. "
                "The job did not finish, or results were removed.",
                arm.name,
                {"job": str(job.path), "planned": job.n_total_trials, "found": found},
            )
    retried = sum(job.n_retries or 0 for job in arm.jobs)
    if retried:
        yield Finding(
            "retried-trials",
            "note",
            f"Harbor ran {retried} attempts again and deleted the earlier ones. Their results are not in the directory.",
            arm.name,
            {"retries": retried},
        )


def check_attempts(arm: Arm) -> Iterator[Finding]:
    counts = Counter(len(trials) for trials in arm.by_task().values())
    if not counts:
        return
    if len(counts) > 1:
        yield _uneven_attempts(arm, counts)
    if max(counts) == 1:
        yield Finding(
            "single-attempt",
            "note",
            "Every task has 1 trial. The rerun noise of this arm cannot be measured from its own data.",
            arm.name,
        )


def _uneven_attempts(arm: Arm, counts: Counter) -> Finding:
    assert len(counts) > 1
    outcomes = arm.outcomes()
    message = f"Tasks have different numbers of trials ({_top(counts)} as attempts ×tasks)."
    if not outcomes:
        return Finding(
            "uneven-attempts",
            "warn",
            f"{message} No trial has a valid reward, so the score has no mean.",
            arm.name,
            {"attempts": dict(counts), "macro": None, "micro": None},
        )
    score = stats.score(outcomes)
    return Finding(
        "uneven-attempts",
        "warn",
        f"{message} The mean over tasks is {100 * score.mean:.1f}% and the mean over trials is {100 * score.micro:.1f}%.",
        arm.name,
        {"attempts": dict(counts), "macro": score.mean, "micro": score.micro},
    )


def _is_control(agent: str | None) -> bool:
    return isinstance(agent, str) and all(s.rpartition("=")[2] in CONTROL_AGENTS for s in agent.split(","))


def check_control_agents(arm: Arm) -> Iterator[Finding]:
    agents = Counter(t.agent for t in arm.trials)
    controls = {a: n for a, n in agents.items() if _is_control(a)}
    if controls and len(controls) < len(agents):
        passed = sum(1 for t in arm.trials if _is_control(t.agent) and (t.reward or 0) > 0)
        yield Finding(
            "control-agent-trials",
            "error",
            f"{sum(controls.values())} trials ran Harbor's control agents ({_top(Counter(controls))}) inside an arm "
            f"of real attempts. {passed} of them have a reward above 0 and count toward the score.",
            arm.name,
            {
                "agents": controls,
                "passed": passed,
                "trials": [t.trial_name for t in arm.trials if _is_control(t.agent)][:20],
            },
        )


def check_mixed(arm: Arm) -> Iterator[Finding]:
    fields = (
        ("agent", "warn", lambda t: t.agent),
        ("agent version", "warn", lambda t: t.agent_version if t.agent_version != "unknown" else None),
        ("model", "warn", lambda t: t.model),
        ("environment", "warn", lambda t: t.environment),
        ("dataset", "warn", lambda t: t.source),
        ("Harbor version", "warn", lambda t: t.harbor_version),
        ("agent arguments", "note", lambda t: t.conditions.get("agent.kwargs")),
    )
    for label, severity, getter in fields:
        values = Counter(v for t in arm.trials if (v := getter(t)) is not None)
        if len(values) > 1:
            shown = Counter({render.shorten(str(k), 60): n for k, n in values.items()})
            yield Finding(
                "mixed-arm",
                severity,
                f"The arm mixes {len(values)} values of {label}: {_top(shown)}.",
                arm.name,
                {"field": label, "values": {str(k): v for k, v in values.items()}},
            )


def check_limits(arm: Arm) -> Iterator[Finding]:
    changed: Counter[str] = Counter()
    for trial in arm.trials:
        for key, value in trial.conditions.items():
            if key != "agent.kwargs":
                changed[f"{key}={value}"] += 1
    if changed:
        yield Finding(
            "non-default-limits",
            "warn",
            f"Trials ran with limits that differ from the task defaults: {_top(changed, 8)}.",
            arm.name,
            {"settings": dict(changed)},
        )


def check_task_identity(arm: Arm) -> Iterator[Finding]:
    names: dict[str, set[str]] = {}
    checksums: dict[str, set[str]] = {}
    for trial in arm.trials:
        names.setdefault(trial.task, set()).add(trial.task_name)
        if trial.task_checksum:
            checksums.setdefault(trial.task, set()).add(trial.task_checksum)
    merged = {key: sorted(v) for key, v in names.items() if len(v) > 1}
    if merged:
        yield Finding(
            "merged-task-names",
            "note",
            f"{len(merged)} tasks appear under more than 1 name and are treated as one task.",
            arm.name,
            {"tasks": dict(list(merged.items())[:10])},
        )
    drift = {key: sorted(v) for key, v in checksums.items() if len(v) > 1}
    if drift:
        yield Finding(
            "checksum-drift",
            "warn",
            f"{len(drift)} tasks have more than 1 checksum inside this arm. Their trials ran different task files.",
            arm.name,
            {"tasks": sorted(drift)[:20]},
        )


def check_task_key(arm: Arm) -> Iterator[Finding]:
    missed = Counter(t.task_name for t in arm.trials if t.key_matched is False)
    if missed:
        count = sum(missed.values())
        yield Finding(
            "unmatched-task-key",
            "note",
            f"{_share(count, len(arm.trials))} trials have a task name that --task-key does not match "
            f"({_top(missed, 3)}). They keep the key of their own name.",
            arm.name,
            {"count": count, "names": sorted(missed)[:20]},
        )


def check_dispersion(arm: Arm) -> Iterator[Finding]:
    by_task = {t: v for t, v in arm.outcomes().items() if len(v) >= 3}
    if len(by_task) < 20:
        return
    if all(len(set(values)) == 1 for values in by_task.values()):
        yield Finding(
            "zero-dispersion",
            "warn",
            f"No task changes its outcome between attempts ({len(by_task)} tasks with 3 or more attempts). "
            "Independent agent runs almost never agree this well. Check that the attempts are separate runs.",
            arm.name,
            {"tasks": len(by_task)},
        )


def check_duplicates(arm: Arm) -> Iterator[Finding]:
    seen: Counter[tuple[Any, ...]] = Counter()
    for trial in arm.trials:
        if trial.started_at and trial.finished_at:
            seen[(trial.task, trial.started_at, trial.finished_at)] += 1
    copies = sum(count - 1 for count in seen.values() if count > 1)
    if copies:
        yield Finding(
            "duplicate-trials",
            "error",
            f"{copies} trials have the same task, start time and end time as another trial. They look like copies.",
            arm.name,
            {"copies": copies},
        )


def check_pass_with_exception(arm: Arm) -> Iterator[Finding]:
    passed = Counter(t.exception for t in arm.trials if t.exception and (t.reward or 0) > 0)
    if passed:
        raised = sum(1 for t in arm.trials if t.exception)
        yield Finding(
            "pass-with-exception",
            "note",
            f"{sum(passed.values())} of {raised} trials that raised an exception still have a reward above 0 "
            f"({_top(passed, 3)}). They count as passes.",
            arm.name,
            {"by_exception": dict(passed)},
        )


def check_accounting(arm: Arm) -> Iterator[Finding]:
    total = len(arm.trials)
    if not total:
        return
    no_tokens = sum(1 for t in arm.trials if not t.tokens_in)
    no_cost = sum(1 for t in arm.trials if t.cost_usd is None)
    partial = sum(1 for t in arm.trials if t.partial_usage)
    if no_tokens or no_cost or partial:
        teams = (
            f" In {_share(partial, total)} trials only a part of the seats that used a model report a value. "
            "That value is left out: the sum over a part of a team is not the usage of the team."
        )
        yield Finding(
            "missing-usage",
            "note",
            f"Token counts are missing or 0 in {_share(no_tokens, total)} trials and cost is missing in "
            f"{_share(no_cost, total)}. Cost and token figures cover only the rest.{teams if partial else ''}",
            arm.name,
            {"no_tokens": no_tokens, "no_cost": no_cost} | ({"partial": partial} if partial else {}),
        )


def _secs(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.0f} s"


def _describe(fault: SeatFault) -> str:
    evidence = fault.evidence
    if fault.kind == "stalled":
        steps = evidence["agent_steps"]
        done = "an unknown number of agent steps" if steps is None else render.count(steps, "agent step")
        return f"{fault.seat} silent {_secs(evidence['silent_s'])} after {done}"
    if fault.kind == "lost":
        return (
            f"{fault.seat} exit code {evidence['exit_code']} after {_secs(evidence['played_s'])} "
            f"of a {_secs(evidence['phase_s'])} agent phase"
        )
    if fault.kind == "turn-cap":
        return f"{fault.seat} {evidence['turns']} of {evidence['max_turns']} turns"
    if fault.kind == "early-end":
        return f"{fault.seat} played {_secs(evidence['played_s'])}, seat median {_secs(evidence['median_s'])}"
    return f"{fault.seat} ({evidence['exception'] or 'no exception'})"


SEAT_TRIAL_FINDINGS = {
    "lost": (
        "lost-seats",
        "error",
        "exited with a non-zero code, and result.json records no exception for it",
        "The team played without it, and the reward does not show it.",
    ),
    "stalled": (
        "stalled-seats",
        "warn",
        "ended with AgentTimeoutError and made no agent step in the last {stall} of the seat run",
        "The model call did not return.",
    ),
    "zero-calls": ("zero-call-seats", "warn", "with a model made 0 model calls", "The seat did not play."),
}
SEAT_ARM_FINDINGS = {
    "turn-cap": ("turn-cap-seats", "reached their turn cap. Their reward is real, but the cap bounded it"),
    "early-end": (
        "early-seat-end",
        f"with a model stopped without an error before {EARLY_SHARE:.0%} of the median play time of the "
        "seats in their trial. Check whether they stopped on purpose",
    ),
}


def _verdict(trial: Trial) -> str:
    if trial.valid and trial.reward is not None:
        return f"Treat the trial as unmeasured: its reward {trial.reward:g} counts now."
    return "Treat the trial as unmeasured. It has no valid reward."


def _trial_finding(arm: Arm, trial: Trial, kind: str, faults: list[SeatFault], stall: float) -> Finding:
    code, severity, what, why = SEAT_TRIAL_FINDINGS[kind]
    listed = "; ".join(_describe(f) for f in faults[:8])
    return Finding(
        code,
        severity,
        f"Trial {trial.trial_name}: {render.count(len(faults), 'seat')} {what.format(stall=_secs(stall))} "
        f"({listed}). {why} {_verdict(trial)}",
        arm.name,
        {
            "trial": trial.trial_name,
            "path": str(trial.path),
            "reward": trial.reward,
            "unmeasured": True,
            "seats": [f.as_dict() for f in faults],
        },
    )


def _arm_finding(arm: Arm, kind: str, hits: list[tuple[Trial, SeatFault]]) -> Finding:
    code, what = SEAT_ARM_FINDINGS[kind]
    trials = distinct(t.trial_name for t, _ in hits)
    listed = "; ".join(f"{t.trial_name} {_describe(f)}" for t, f in hits[:5])
    return Finding(
        code,
        "note",
        f"{render.count(len(hits), 'seat')} in {render.count(len(trials), 'trial')} {what} ({listed}).",
        arm.name,
        {
            "count": len(hits),
            "trials": trials[:50],
            "unmeasured": False,
            "seats": [{"trial": t.trial_name, **f.as_dict()} for t, f in hits[:200]],
        },
    )


def _seat_errors(arm: Arm, explained: set[tuple[Path, str]]) -> Iterator[Finding]:
    hit = [
        (t, seat, error)
        for t in arm.trials
        if t.reward is not None
        for seat, error in t.seat_exceptions.items()
        if (t.path, seat) not in explained
    ]
    trials = distinct(t.path for t, _, _ in hit)
    if hit:
        errors = Counter(f"{seat} {error}" for _, seat, error in hit)
        valid = sum(1 for t in arm.trials if t.path in trials and t.valid)
        yield Finding(
            "seat-errors",
            "note",
            f"{_share(len(trials), len(arm.trials))} trials have a reward, and a seat in them raised an exception "
            f"({_top(errors)}). That seat did not finish its work. The reward counts when Harbor marks it valid.",
            arm.name,
            {"count": len(trials), "valid": valid, "by_seat": dict(errors)},
        )


def check_seats(arm: Arm, stall_seconds: float = STALL_SECONDS) -> Iterator[Finding]:
    assert stall_seconds > 0
    found = [(t, f) for t in arm.trials for f in trial_faults(t, stall_seconds)]
    yield from _seat_errors(arm, {(t.path, f.seat) for t, f in found if f.kind == "stalled"})
    for kind in SEAT_TRIAL_FINDINGS:
        by_trial: dict[Path, tuple[Trial, list[SeatFault]]] = {}
        for trial, fault in found:
            if fault.kind == kind:
                by_trial.setdefault(trial.path, (trial, []))[1].append(fault)
        for trial, faults in by_trial.values():
            yield _trial_finding(arm, trial, kind, faults, stall_seconds)
    for kind in SEAT_ARM_FINDINGS:
        hits = [(t, f) for t, f in found if f.kind == kind]
        if hits:
            yield _arm_finding(arm, kind, hits)


def check_not_started(arm: Arm) -> Iterator[Finding]:
    failed = [t for t in arm.trials if t.not_started is not None]
    if not failed:
        return
    kinds = Counter(t.not_started for t in failed)
    names = [t.trial_name for t in failed]
    yield Finding(
        "not-started",
        "warn",
        f"{_share(len(failed), len(arm.trials))} trials never reached the agent phase ({_top(kinds)}): "
        f"{', '.join(names[:5])}{', …' if len(names) > 5 else ''}. They are unmeasured, never reward 0.",
        arm.name,
        {"count": len(failed), "by_kind": dict(kinds), "trials": names[:200], "unmeasured": True},
    )


def check_reward_values(arm: Arm) -> Iterator[Finding]:
    values = Counter(t.reward for t in arm.trials if t.reward is not None)
    outside = {v: c for v, c in values.items() if v < 0 or v > 1}
    if outside:
        yield Finding(
            "reward-out-of-range",
            "warn",
            f"{sum(outside.values())} trials have a reward below 0 or above 1: {_top(Counter(outside))}.",
            arm.name,
            {"values": {str(k): v for k, v in outside.items()}},
        )
    multi = Counter(len(t.rewards) for t in arm.trials if len(t.rewards) > 1 and t.reward is None)
    if multi:
        yield Finding(
            "no-primary-reward",
            "warn",
            f"{sum(multi.values())} trials have several reward values and none is named 'reward'. "
            "Choose one with --reward-key.",
            arm.name,
            {"keys": distinct(k for t in arm.trials for k in t.rewards)},
        )


ARM_CHECKS: tuple[Callable[[Arm], Iterator[Finding]], ...] = (
    check_unreadable,
    check_unfinished,
    check_duplicates,
    check_jobs,
    check_control_agents,
    check_invalid,
    check_attempts,
    check_mixed,
    check_limits,
    check_task_identity,
    check_task_key,
    check_dispersion,
    check_reward_values,
    check_pass_with_exception,
    check_not_started,
    check_accounting,
)


def check_task_sets(a: Arm, b: Arm) -> Iterator[Finding]:
    tasks_a, tasks_b = set(a.by_task()), set(b.by_task())
    only_a, only_b = sorted(tasks_a - tasks_b), sorted(tasks_b - tasks_a)
    if only_a or only_b:
        yield Finding(
            "task-set-mismatch",
            "warn",
            f"The arms share {len(tasks_a & tasks_b)} tasks. {len(only_a)} tasks are only in {a.name} "
            f"and {len(only_b)} only in {b.name}. Only shared tasks are compared.",
            None,
            {"only_a": only_a[:20], "only_b": only_b[:20]},
        )


def check_checksums(a: Arm, b: Arm) -> Iterator[Finding]:
    def sums(arm: Arm) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for trial in arm.trials:
            if trial.task_checksum:
                out.setdefault(_own(trial), set()).add(trial.task_checksum)
        return out

    sums_a, sums_b = sums(a), sums(b)
    shared = sorted(set(sums_a) & set(sums_b))
    different = [t for t in shared if not (sums_a[t] & sums_b[t])]
    if not different:
        return
    sources = f"{', '.join(map(str, distinct(t.source for t in a.trials)))} and {', '.join(map(str, distinct(t.source for t in b.trials)))}"
    if len(different) == len(shared):
        message = (
            f"No shared task has the same checksum in both arms (datasets: {sources}). "
            "The arms ran different task files, or different Harbor versions that hash tasks differently."
        )
    else:
        message = f"{len(different)} of {len(shared)} shared tasks have different checksums. Those tasks ran different files."
    yield Finding(
        "checksum-mismatch", "warn", message, None, {"tasks": different[:20], "count": len(different)}
    )


def check_conditions(a: Arm, b: Arm) -> Iterator[Finding]:
    def values(arm: Arm, key: str) -> set[Any]:
        if key == "environment":
            return {t.environment for t in arm.trials}
        if key == "Harbor version":
            return {t.harbor_version for t in arm.trials if t.harbor_version}
        return {t.conditions.get(key) for t in arm.trials}

    keys = {"environment", "Harbor version"} | {
        k for arm in (a, b) for t in arm.trials for k in t.conditions if k != "agent.kwargs"
    }
    for key in sorted(keys):
        va, vb = values(a, key), values(b, key)
        if va != vb and va and vb:
            yield Finding(
                "condition-mismatch",
                "warn",
                f"The arms ran under different conditions: {key} is {sorted(map(str, va))} in {a.name} "
                f"and {sorted(map(str, vb))} in {b.name}. This can change the score without any agent change.",
                None,
                {"key": key, "a": sorted(map(str, va)), "b": sorted(map(str, vb))},
            )


def check_attempt_balance(a: Arm, b: Arm) -> Iterator[Finding]:
    by_a, by_b = a.by_task(), b.by_task()
    shared = set(by_a) & set(by_b)
    unequal = sum(1 for t in shared if len(by_a[t]) != len(by_b[t]))
    if unequal:
        yield Finding(
            "attempts-mismatch",
            "note",
            f"{unequal} shared tasks have different numbers of trials in the 2 arms.",
            None,
            {"tasks": unequal},
        )


PAIR_CHECKS: tuple[Callable[[Arm, Arm], Iterator[Finding]], ...] = (
    check_task_sets,
    check_checksums,
    check_conditions,
    check_attempt_balance,
)


def audit_arm(arm: Arm, stall_seconds: float = STALL_SECONDS) -> list[Finding]:
    if not arm.trials:
        return [
            Finding("no-trials", "error", "No trial was found.", arm.name),
            *check_unreadable(arm),
            *check_unfinished(arm),
        ]
    findings = [finding for check in ARM_CHECKS for finding in check(arm)]
    return findings + list(check_seats(arm, stall_seconds))


def audit_pair(a: Arm, b: Arm) -> list[Finding]:
    return [finding for check in PAIR_CHECKS for finding in check(a, b)]


def _groups(arms: Sequence[Arm], signature: Callable[[Arm], Any]) -> list[list[str]]:
    groups: dict[Any, list[str]] = {}
    for arm in arms:
        groups.setdefault(signature(arm), []).append(arm.name)
    return sorted(groups.values(), key=lambda names: (-len(names), names))


def _sizes(groups: list[list[str]]) -> str:
    return ", ".join(f"{len(g)} arms" if len(g) > 1 else g[0] for g in groups[:6])


def audit_group(arms: Sequence[Arm]) -> list[Finding]:
    findings = []
    checks = (
        (
            "task-versions",
            "sets of task checksums",
            "Scores from different sets are not on the same tasks, or come from Harbor versions "
            "that hash tasks differently.",
            lambda arm: frozenset(
                (t.task, t.task_checksum) for t in arm.trials if t.task_checksum and t.task == _own(t)
            ),
        ),
        (
            "task-sets",
            "sets of tasks",
            "Only tasks that every arm has can be compared.",
            lambda arm: frozenset(arm.by_task()),
        ),
        (
            "environments",
            "environments",
            "The sandbox can change a score without any agent change.",
            lambda arm: frozenset(t.environment for t in arm.trials),
        ),
    )
    for code, what, why, signature in checks:
        groups = _groups(arms, signature)
        if len(groups) > 1:
            findings.append(
                Finding(
                    code,
                    "warn",
                    f"The arms use {len(groups)} different {what} ({_sizes(groups)}). {why}",
                    None,
                    {"groups": groups},
                )
            )
    return findings


def audit(arms: Sequence[Arm], stall_seconds: float = STALL_SECONDS) -> list[Finding]:
    findings = [f for arm in arms for f in audit_arm(arm, stall_seconds)]
    if len(arms) == 2:
        findings.extend(audit_pair(arms[0], arms[1]))
    elif len(arms) > 2:
        findings.extend(audit_group(arms))
    return findings


def worst(findings: Sequence[Finding]) -> str | None:
    for severity in SEVERITIES:
        if any(f.severity == severity for f in findings):
            return severity
    return None
