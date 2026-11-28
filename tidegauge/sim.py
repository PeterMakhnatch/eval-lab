from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from io import StringIO
from itertools import pairwise, product
from pathlib import Path
from random import Random
from statistics import fmean
from tempfile import TemporaryDirectory
from typing import Any, TypedDict

from tidegauge import _bespoke_sim as bfx
from tidegauge import stats
from tidegauge.cli import main as tidegauge_main
from tidegauge.sim_fixture import (
    STALL_CHOICES,
    UNMEASURED_KINDS,
    Scenario,
    TimedTrial,
    TrialSpec,
    draw,
    health_plan,
    scenario,
    stamp,
    timeline_plan,
    write_arm,
    write_fault_arm,
    write_health_job,
    write_job,
    write_live_job,
    write_timeline,
    write_trial,
)

_FIXED_SEEDS = range(24)
_ALPHA = 0.05
_NULL_SAMPLES = 768
_REGRESSIONS = Path(__file__).with_name("sim_regressions.json")
_MAX_REGRESSIONS = 1024
_MAX_SEED = 2**64 - 1
_ALL_INVALID_SEED = _MAX_SEED
_DEFAULT_HUNT_SEED = 261121
_JSON_NODE_LIMIT = 50_000
_EXIT = {"ok": 0, "regressed": 1, "undecided": 2, "invalid": 3}
_RESOLVED = {"candidate is higher", "baseline is higher"}
_QUESTIONS = ("rerun", "tasks")
Report = dict[str, Any]


class Regression(TypedDict):
    seed: int
    step: str
    invariant: str
    known: bool


@dataclass(frozen=True)
class Violation:
    seed: int
    step: str
    invariant: str
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"seed={self.seed} step={self.step} invariant={self.invariant}{suffix}"

    def matches(self, row: Regression) -> bool:
        return (self.seed, self.step, self.invariant) == (row["seed"], row["step"], row["invariant"])


@dataclass
class Run:
    seed: int
    scenarios: int = 0
    checks: int = 0
    failures: list[Violation] = field(default_factory=list)

    def require(self, step: str, invariant: str, holds: bool, detail: str = "") -> None:
        self.checks += 1
        if not holds:
            self.failures.append(Violation(self.seed, step, invariant, detail))

    def exercise(self, step: str, action: Callable[[], None]) -> None:
        self.checks += 1
        try:
            action()
        except Exception as error:
            self.failures.append(Violation(self.seed, step, "no-exception", f"{type(error).__name__}: {error}"))


@dataclass(frozen=True)
class Capture:
    status: int | None
    stdout: str
    stderr: str
    exception: str = ""


def _capture(args: list[str]) -> Capture:
    stdout, stderr = StringIO(), StringIO()
    status = None
    error = ""
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            status = tidegauge_main(args)
        except (Exception, SystemExit) as exc:
            error = f"{type(exc).__name__}: {exc}"
    return Capture(status, stdout.getvalue(), stderr.getvalue(), error)


def _reject_constant(value: str) -> float:
    raise ValueError(f"non-JSON constant: {value}")


def _inspect_json(run: Run, step: str, report: Report) -> None:
    pending: list[tuple[str, Any]] = [("", report)]
    bad: list[str] = []
    probabilistic = {"p", "alpha", "power", "level", "cached_share", "resent_share"}
    for _ in range(_JSON_NODE_LIMIT):
        if not pending:
            break
        key, value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.items())
        elif isinstance(value, list):
            pending.extend(("", item) for item in value)
        elif value is not None and (key.startswith("p_") or key in probabilistic):
            if type(value) not in (float, int) or not 0 <= value <= 1:
                bad.append(f"{key}={value}")
        elif isinstance(value, float) and not math.isfinite(value):
            bad.append(f"{key}={value}")
    run.require(step, "bounded-json", not pending)
    run.require(step, "probabilities-in-range-and-numbers-finite", not bad, ", ".join(bad[:8]))


def _report(
    run: Run, step: str, command: str, paths: list[Path], *, codes: tuple[int, ...] = (0,),
    options: tuple[str, ...] = (),
) -> Report | None:
    argv = [command, *(str(path) for path in paths), *options]
    outcomes: list[Capture] = []
    for form, flags in (("text", []), ("json", ["--json"])):
        first, second = _capture([*argv, *flags]), _capture([*argv, *flags])
        label = f"{step}/{form}"
        run.require(label, "no-uncaught-exception", not first.exception, first.exception)
        run.require(label, "documented-exit", first.status in codes, f"got {first.status}; {first.stderr.strip()}")
        run.require(label, "no-traceback", "Traceback (most recent call last)" not in first.stderr + first.stdout)
        run.require(label, "byte-identical-output", first == second)
        outcomes.append(first)
    text, encoded = outcomes
    run.require(step, "same-exit-for-text-and-json", text.status == encoded.status)
    if encoded.exception or encoded.status not in codes:
        return None
    if not encoded.stdout and encoded.status == 3 and command == "plan":
        diagnostic = "no task has 2 or more trials"
        run.require(step, "data-error-has-diagnostic", diagnostic in encoded.stderr and diagnostic in text.stderr)
        return None
    run.require(step, "nonempty-text", bool(text.stdout.strip()))
    try:
        payload = json.loads(encoded.stdout, parse_constant=_reject_constant)
    except ValueError as exc:
        run.require(step, "parseable-json", False, str(exc))
        return None
    run.require(step, "parseable-json", True)
    correct = isinstance(payload, dict) and payload.get("command") == command
    run.require(step, "report-identifies-command", correct)
    if not correct:
        return None
    _inspect_json(run, step, payload)
    if command == "gate":
        run.require(step, "gate-exit-matches-decision", _EXIT.get(payload["decision"]) == encoded.status)
    return payload


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12)


def _interval(run: Run, step: str, interval: list[float] | None, point: float) -> None:
    holds = interval is None or len(interval) == 2 and interval[0] <= point <= interval[1]
    run.require(step, "interval-contains-estimate", holds, f"{interval} versus {point}")


def _effect(run: Run, step: str, report: Report) -> None:
    for question in _QUESTIONS:
        _interval(run, f"{step}/{question}", report[question]["interval"], report["difference"])


def _score_and_audit(run: Run, case: Scenario, paths: list[Path]) -> None:
    options = ("--trials", "valid")
    counts = [sum(len(xs) for xs in arm.values()) for arm in case.arms]
    if report := _report(run, "score", "score", paths, options=options):
        run.require("score", "one-report-per-arm", len(report["arms"]) == len(paths))
        for row, arm, n in zip(report["arms"], case.arms, counts, strict=True):
            mean = row["score"]
            step = f"score/{row['name']}"
            run.require(step, "all-clean-trials-counted", row["trials"] == n and row["tasks"] == len(arm))
            run.require(step, "score-is-task-macro-mean", _close(mean, fmean(fmean(xs) for xs in arm.values())))
            run.require(step, "reward-probability-in-range", 0 <= mean <= 1 and 0 <= row["mean_over_trials"] <= 1)
            for measure in ("ci_rerun", "ci_tasks"):
                _interval(run, f"{step}/{measure}", row[measure], mean)
        if case.kind == "extremes":
            run.require("score", "all-pass-and-all-fail-endpoints", [r["score"] for r in report["arms"][:2]] == [0, 1])
    if report := _report(run, "audit", "audit", paths):
        run.require("audit", "clean-arms-have-no-errors", report["counts"]["error"] == 0)
        run.require("audit", "every-clean-reward-valid", [a["valid"] for a in report["arms"]] == counts)


def _compare(run: Run, case: Scenario, paths: list[Path]) -> None:
    first, second = paths[0], paths[min(1, len(paths) - 1)]
    forward = _report(run, "compare", "compare", [first, second])
    reverse = _report(run, "compare/swapped", "compare", [second, first])
    self_check = _report(run, "compare/self", "compare", [first, first])
    for name, report in (("compare", forward), ("compare/swapped", reverse), ("compare/self", self_check)):
        if report is not None:
            _effect(run, name, report)
            a, b = report["score_baseline"], report["score_candidate"]
            run.require(name, "scores-are-probabilities", 0 <= a <= 1 and 0 <= b <= 1)
            run.require(name, "difference-is-candidate-minus-baseline", _close(report["difference"], b - a))
    if self_check is not None:
        run.require("compare/self", "self-difference-zero", self_check["difference"] == 0)
        run.require("compare/self", "self-never-resolved", all(self_check[q]["verdict"] not in _RESOLVED for q in _QUESTIONS))
    if forward is None or reverse is None:
        return
    run.require("compare/swapped", "difference-negates", _close(forward["difference"], -reverse["difference"]))
    for question, fields in (("rerun", ("p", "p_t", "p_permutation")), ("tasks", ("p", "p_signflip"))):
        for name in fields:
            a, b = forward[question][name], reverse[question][name]
            holds = a is b if a is None or b is None else _close(a, b)
            run.require(f"compare/swapped/{question}/{name}", "p-values-unchanged", holds, f"{a} versus {b}")
    if case.kind == "power":
        for question in _QUESTIONS:
            run.require(f"compare/{question}", "large-effect-resolved-in-correct-direction",
                        forward[question]["verdict"] == "candidate is higher", str(forward[question]["verdict"]))


def _rank_and_plan(run: Run, case: Scenario, paths: list[Path]) -> None:
    if len(paths) > 1 and (report := _report(run, "rank", "rank", paths)):
        rows = report["arms"]
        run.require("rank", "every-arm-ranked", len(rows) == len(paths))
        for (position, row) in enumerate(rows, start=1):
            step = f"rank/{row['name']}"
            low, high = row["rank_interval"]
            run.require(step, "rank-interval-within-field", 1 <= low <= high <= len(rows))
            _interval(run, step, row["rank_interval"], position)
            run.require(step, "rank-index-and-reward-range", row["rank"] == position and 0 <= row["score"] <= 1)
        run.require("rank", "ranked-by-descending-score", all(a["score"] >= b["score"] for a, b in pairwise(rows)))
        probabilities = [row["p_first"] for row in rows]
        defined = [p for p in probabilities if p is not None]
        run.require("rank", "first-place-probabilities-sum-to-one",
                    not defined or len(defined) == len(rows) and _close(sum(defined), 1))
    measurable = max(case.attempts) > 1
    codes = (0,) if measurable else (3,)
    report = _report(run, "plan", "plan", paths[:2], codes=codes)
    if report is not None:
        rows = report["rows"]
        run.require("plan", "trial-budget-equals-tasks-times-attempts", bool(rows) and all(
            row["trials_per_arm"] == row["attempts"] * len(case.attempts) for row in rows
        ))
        run.require("plan", "detectable-rerun-effect-never-increases-with-attempts", all(
            a["detectable_rerun"] >= b["detectable_rerun"] >= 0 for a, b in pairwise(rows)
        ))


def _gate_and_decompose(run: Run, paths: list[Path]) -> None:
    pair = [paths[0], paths[min(1, len(paths) - 1)]]
    if report := _report(run, "gate", "gate", pair, codes=(0, 1, 2), options=("--require-gain",)):
        _interval(run, "gate", report["interval"], report["difference"])
        if other := report["other_counting"]:
            _interval(run, "gate/other-counting", other["interval"], other["difference"])
    if len(paths) < 3:
        return
    if report := _report(run, "decompose", "decompose", paths[:3]):
        rows = report["arms"]
        run.require("decompose", "role-order", [a["role"] for a in rows] == ["solo", "indep", "team"])
        effects = report["effects"]
        run.require("decompose", "headcount-plus-coordination-is-total", _close(
            effects["headcount"]["difference"] + effects["coordination"]["difference"], effects["total"]["difference"]
        ))
        for key, effect in effects.items():
            _effect(run, f"decompose/{key}", effect)
        for row in rows:
            step = f"decompose/{row['role']}"
            run.require(step, "reward-probability-in-range", 0 <= row["score"] <= 1)
            for measure in ("ci_rerun", "ci_tasks"):
                _interval(run, f"{step}/{measure}", row[measure], row["score"])


def _context(run: Run, paths: list[Path], seats: list[int]) -> None:
    if report := _report(run, "context", "context", paths):
        for arm, count in zip(report["arms"], seats, strict=True):
            step = f"context/{arm['name']}"
            run.require(step, "all-clean-trajectories-usable", arm["trials"] == arm["usable"] > 0)
            for row in arm["per_trial"]:
                run.require(step, "per-seat-token-sums", row["calls"] == 2 * (count or 1)
                            and row["input_tokens"] == 280 * (count or 1)
                            and row["fresh_tokens"] == 180 * (count or 1)
                            and row["cached_tokens"] == 130 * (count or 1)
                            and row["output_tokens"] == 30 * (count or 1)
                            and row["peak_context"] == 180)


def _fault(run: Run, root: Path, fault: str, seats: int) -> None:
    run.scenarios += 1
    job = root / "fault-arm"
    write_fault_arm(job, fault, seats)
    step = f"fault/{fault}"
    unread = fault in ("malformed-result", "truncated-result", "non-utf8-result")
    unfinished = fault == "unfinished-directory"
    duplicate = fault == "duplicate-trial"
    missing_traj = fault == "missing-trajectory"
    invalid = fault in ("missing-reward-file", "null-reward", "nan-reward", "infinite-reward",
                        "out-of-range-reward", "exception")
    findings = set()
    if unread or unfinished:
        findings.add("incomplete-job")
        findings.add("unreadable-result" if unread else "unfinished-trials")
    if invalid:
        findings.add("invalid-trials")
    if duplicate:
        findings.add("duplicate-trials")
    if fault == "out-of-range-reward":
        findings.add("reward-out-of-range")
    codes = (1,) if unread or unfinished or duplicate else (0,)
    if audit := _report(run, f"{step}/audit", "audit", [job], codes=codes):
        seen = {finding["code"] for finding in audit["findings"]}
        run.require(step, "fault-has-audit-finding", findings <= seen, f"missing {sorted(findings - seen)}")
        if fault == "empty-directory":
            run.require(step, "empty-directory-ignored", audit["counts"]["error"] == 0)
        row = audit["arms"][0]
        trials = 1 + int(invalid or duplicate or missing_traj)
        run.require(step, "only-trial-results-counted", row["trials"] == trials)
    if score := _report(run, f"{step}/score", "score", [job], options=("--trials", "valid")):
        row = score["arms"][0]
        expected = 1 + int(duplicate or missing_traj)
        run.require(step, "invalid-trials-not-counted-as-zero", row["score"] == 1 and row["trials"] == expected,
                    f"score={row['score']}, trials={row['trials']}, expected {expected} valid")
    if codes == (1,) and (gate := _report(run, f"{step}/gate", "gate", [job, job], codes=(3,))):
        run.require(step, "gate-refuses-audit-errors", gate["decision"] == "invalid")
    if missing_traj and (report := _report(run, f"{step}/context", "context", [job])):
        states = [r["trajectory"] for r in report["arms"][0]["per_trial"]]
        codes_seen = {finding["code"] for finding in report["findings"]}
        run.require(step, "missing-trajectory-reported", sorted(states) == ["missing", "usable"]
                    and "trajectory-missing" in codes_seen)


def _all_invalid(run: Run, root: Path) -> None:
    run.scenarios += 1
    job = root / "all-invalid"
    write_job(job, 3)
    for task, attempt in (("t000", 0), ("t000", 1), ("t001", 0)):
        spec = TrialSpec(task, attempt, None, 0, "missing-reward-file")
        write_trial(job / spec.name, spec)
    if audit := _report(run, "all-invalid/audit", "audit", [job], codes=(0, 1)):
        run.require("all-invalid/audit", "invalid-rewards-diagnosed", audit["arms"][0]["valid"] == 0
                    and "invalid-trials" in {f["code"] for f in audit["findings"]})
    if score := _report(run, "all-invalid/score", "score", [job], options=("--trials", "valid")):
        arm = score["arms"][0]
        run.require("all-invalid/score", "no-estimate-without-valid-rewards", arm["score"] is None and arm["trials"] == 0)


def _numeric(run: Run) -> None:
    step = "statistics"
    run.require(step, "cauchy-two-sided-p", _close(stats.t_two_sided_p(1, 1), 0.5))
    result = stats.score({"t0": [1, 1, 1], "t1": [0]})
    run.require(step, "uneven-attempts-macro-and-micro", result.mean == 0.5 and result.micro == 0.75)
    p, exact = stats.stratified_permutation_p([([0, 0], [1, 1])])
    run.require(step, "exact-permutation-six-allocations", exact and p is not None and _close(p, 1 / 3))
    p, exact = stats.sign_flip_p([1, 0.5, 0.5], [1, 2, 2])
    run.require(step, "exact-sign-flip-two-of-eight", exact and p == 0.25, f"p={p}, exact={exact}")
    rng = Random(run.seed)
    integers = [rng.randint(-4, 4) for _ in range(rng.randint(1, 7))]
    hits = sum(abs(sum(s * d for s, d in zip(signs, integers, strict=True))) >= abs(sum(integers))
               for signs in product((-1, 1), repeat=len(integers)))
    expected = hits / 2 ** len(integers)
    p, exact = stats.sign_flip_p([i / 4 for i in integers], [4] * len(integers))
    run.require(step, "exact-sign-flip-matches-enumeration", exact and p == expected, f"{integers}: {p} versus {expected}")


def _calibration(run: Run) -> None:
    rng = Random(run.seed)
    probabilities = [0.0] * 8 + [1.0] * 7 + [(i + 0.5) / 15 for i in range(15)]
    rerun = tasks = 0
    for i in range(_NULL_SAMPLES):
        run.scenarios += 1
        attempts = [2 if i % 2 == 0 else 4] * len(probabilities)
        a, b = draw(rng, probabilities, attempts), draw(rng, probabilities, attempts)
        result = stats.compare(a, b)
        p_rerun, p_tasks = result.rerun_p(), result.tasks_p()
        step = f"calibration/null/{i}"
        run.require(step, "null-p-values-in-range", all(p is None or 0 <= p <= 1 for p in (p_rerun, p_tasks)))
        rerun += p_rerun is not None and p_rerun < _ALPHA
        tasks += p_tasks is not None and p_tasks < _ALPHA
    binomial_standard_error = math.sqrt(_ALPHA * (1 - _ALPHA) / _NULL_SAMPLES)
    upper = _ALPHA + 3 * binomial_standard_error
    for question, count in zip(_QUESTIONS, (rerun, tasks), strict=True):
        rate = count / _NULL_SAMPLES
        run.require(f"calibration/{question}", "null-resolution-below-alpha-plus-three-binomial-standard-errors",
                    rate <= upper, f"{count}/{_NULL_SAMPLES} = {rate:.4f}; limit {upper:.4f}")


_TRIAL_HEALTH_CODES = frozenset({"stalled-seats", "lost-seats", "zero-call-seats"})
_ARM_HEALTH_CODES = frozenset({"turn-cap-seats", "early-seat-end"})


def _health_seen(findings: list[dict]) -> dict[str, set[tuple[str, str | None]]]:
    seen: dict[str, set[tuple[str, str | None]]] = {}
    for finding in findings:
        code, detail = finding["code"], finding["detail"]
        if code in _TRIAL_HEALTH_CODES:
            seen.setdefault(detail["trial"], set()).update((code, s["seat"]) for s in detail["seats"])
        elif code in _ARM_HEALTH_CODES:
            for row in detail["seats"]:
                seen.setdefault(row["trial"], set()).add((code, row["seat"]))
        elif code == "not-started":
            for name in detail["trials"]:
                seen.setdefault(name, set()).add((code, None))
    return seen


def _health(run: Run, root: Path, rng: Random) -> None:
    run.scenarios += 1
    stall = rng.choice(STALL_CHOICES)
    plan = health_plan(rng, stall)
    job = root / "health"
    write_health_job(job, plan)
    options = () if stall == 300 else ("--stall-seconds", str(stall))
    report = _report(run, "health/audit", "audit", [job], codes=(1,), options=options)
    if report is None:
        return
    seen = _health_seen(report["findings"])
    for spec in plan:
        found, expected = seen.get(spec.name, set()), spec.expected()
        detail = f"{spec.name} at stall {stall}: expected {sorted(expected, key=str)}, saw {sorted(found, key=str)}"
        if expected:
            run.require(f"health/{spec.kind}", "injected-fault-flagged-with-right-class", found == expected, detail)
        else:
            run.require(f"health/{spec.kind}", "clean-trial-never-flagged", not found, detail)
    unmeasured = sum(1 for spec in plan if spec.kind in UNMEASURED_KINDS)
    run.require("health/audit", "unmeasured-count-equals-injected-faults", report["arms"][0]["unmeasured"] == unmeasured,
                f"{report['arms'][0]['unmeasured']} versus {unmeasured}")
    flagged = [f for f in report["findings"] if f["code"] in _TRIAL_HEALTH_CODES]
    run.require("health/audit", "trial-findings-say-unmeasured", all(f["detail"]["unmeasured"] is True for f in flagged))


def _brute_peaks(trials: list[TimedTrial]) -> tuple[int, int]:
    own = together = 0
    for moment in (t.start for t in trials):
        running = [u for u in trials if u.start <= moment < u.end]
        mine = sum(1 for u in running if u.own)
        own = max(own, mine)
        together = max(together, len(running)) if mine else together
    return own, together


def _process_plan(run: Run, root: Path, step: str, own: Path, also: Path, plan: dict, expected: set[str]) -> None:
    path = root / f"{step.replace('/', '-')}.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    options = ("--also", str(also), "--plan", str(path))
    report = _report(run, step, "process", [own], codes=(1,) if expected else (0,), options=options)
    if report is not None:
        kinds = {d["kind"] for d in report["deviations"]}
        holds = expected <= kinds if expected else not kinds
        run.require(step, "plan-deviations-match-injected-changes", holds, f"expected {sorted(expected)}, saw {sorted(kinds)}")


def _timeline(run: Run, root: Path, rng: Random) -> None:
    run.scenarios += 1
    plan = timeline_plan(rng)
    (root / "timeline").mkdir()
    own, also = write_timeline(root / "timeline", plan)
    own_peak, together = _brute_peaks(plan.trials)
    report = _report(run, "process", "process", [own], options=("--also", str(also)))
    if report is not None:
        run.require("process", "peak-equals-generator-truth", report["peak"]["count"] == own_peak,
                    f"{report['peak']['count']} versus {own_peak}")
        run.require("process", "peak-with-also-equals-generator-truth",
                    report["peak_with_also"]["count"] == together, f"{report['peak_with_also']['count']} versus {together}")
        jobs = {t.job for t in plan.trials if t.own}
        run.require("process", "one-row-per-job", {r["name"] for r in report["arms"]} == jobs)
        reruns = {r["name"]: (r["arm"], r["round"]) for r in report["reruns"]}
        run.require("process", "reruns-found", reruns == plan.reruns, f"{reruns} versus {plan.reruns}")
        rows = {r["name"]: r for r in report["arms"]}
        for lane in report["lanes"]:
            ordered = sorted(lane["arms"], key=lambda name: rows[name]["started"])
            run.require("process", "lane-arms-never-overlap", all(
                rows[b]["started"] >= rows[a]["finished"] for a, b in pairwise(ordered)
            ))
    lanes = {f"lane{i}": names for i, names in enumerate(plan.lanes)}
    exact = {"max_at_once": together, "lanes": lanes, "reruns": True}
    _process_plan(run, root, "process/plan-kept", own, also, exact, set())
    changed: dict[str, Any] = {"lanes": {k: list(reversed(v)) for k, v in lanes.items()}, "reruns": False}
    expected = {"arm-order"} if any(len(v) > 1 for v in lanes.values()) else set()
    expected |= {"rerun"} if plan.reruns else set()
    if together > 1:
        changed["max_at_once"] = together - 1
        expected.add("too-many-at-once")
    _process_plan(run, root, "process/plan-broken", own, also, changed, expected)


def _live(run: Run, root: Path, rng: Random) -> None:
    run.scenarios += 1
    stall = rng.choice(STALL_CHOICES)
    case = write_live_job(root / "live", rng, stall)
    options = ("--now", stamp(case.now), "--stall-seconds", str(stall))
    report = _report(run, "live", "live", [root / "live"], codes=(1,) if case.stalled else (0,), options=options)
    if report is None:
        return
    counts = {key: report[key] for key in case.counts}
    run.require("live", "trial-counts-equal-truth", counts == case.counts, f"{counts} versus {case.counts}")
    run.require("live", "planned-equals-truth", report["planned"] == case.planned
                and report["job_result_unreadable"] == case.job_unreadable)
    flagged = {(row["trial"], seat) for row in report["running_trials"] for seat in row["stalled"]}
    run.require("live", "silent-seats-flagged-exactly", flagged == case.stalled,
                f"{sorted(flagged)} versus {sorted(case.stalled)}")


def _bespoke(run: Run, case: Scenario, root: Path) -> None:
    run.scenarios += 1

    def rep(step: str, cmd: str, paths: list[Path], **kw: Any) -> Report | None:
        return _report(run, step, cmd, paths, **kw)

    def eff(step: str, report: Report) -> None:
        _effect(run, step, report)

    bfx.check_bespoke_suite(
        case, root, report_fn=rep, require_fn=run.require, effect_fn=eff, close_fn=_close
    )


def _run_seed(seed: int) -> Run:
    run = Run(seed)
    case = scenario(seed)
    with TemporaryDirectory(prefix="tidegauge-sim-") as directory:
        root = Path(directory)
        run.scenarios += 1
        paths = [root / f"arm-{i}" for i in range(len(case.arms))]
        for path, outcomes, seats in zip(paths, case.arms, case.seats, strict=True):
            write_arm(path, outcomes, seats)
        run.exercise("score-audit", lambda: _score_and_audit(run, case, paths))
        run.exercise("compare", lambda: _compare(run, case, paths))
        run.exercise("rank-plan", lambda: _rank_and_plan(run, case, paths))
        run.exercise("gate-decompose", lambda: _gate_and_decompose(run, paths))
        run.exercise("context", lambda: _context(run, paths, case.seats))
        run.exercise("bespoke", lambda: _bespoke(run, case, root))
        run.exercise(f"fault/{case.fault}", lambda: _fault(run, root, case.fault, case.seats[-1]))
        run.exercise("health", lambda: _health(run, root, Random(f"{seed}:health")))
        run.exercise("timeline", lambda: _timeline(run, root, Random(f"{seed}:timeline")))
        run.exercise("live", lambda: _live(run, root, Random(f"{seed}:live")))
        if seed == _ALL_INVALID_SEED:
            run.exercise("all-invalid", lambda: _all_invalid(run, root))
    run.exercise("statistics", lambda: _numeric(run))
    if seed == 0:
        run.exercise("calibration", lambda: _calibration(run))
    return run


def _regressions() -> list[Regression]:
    rows = json.loads(_REGRESSIONS.read_text(encoding="utf-8"))
    assert isinstance(rows, list) and len(rows) <= _MAX_REGRESSIONS
    for row in rows:
        assert type(row["seed"]) is int and 0 <= row["seed"] <= _MAX_SEED
        assert isinstance(row["step"], str) and row["step"]
        assert isinstance(row["invariant"], str) and row["invariant"]
        assert type(row["known"]) is bool
    return rows


def _check() -> int:
    started = time.monotonic()
    rows = _regressions()
    seeds = list(dict.fromkeys((*(row["seed"] for row in rows), *_FIXED_SEEDS, _ALL_INVALID_SEED)))
    scenarios = checks = unexpected = known = 0
    for seed in seeds:
        result = _run_seed(seed)
        scenarios += result.scenarios
        checks += result.checks
        for failure in result.failures:
            if any(row["known"] and failure.matches(row) for row in rows):
                known += 1
                print(f"Known regression: {failure}")
            else:
                unexpected += 1
                print(f"Failure: {failure}", file=sys.stderr)
    print(f"Seeds run: {len(seeds)}. Scenarios: {scenarios}. Invariants checked: {checks}. "
          f"Unexpected violations: {unexpected}. Known violations: {known}. Time: {time.monotonic() - started:.2f} s.")
    return int(unexpected > 0)


def _hunt(seconds: int, start_seed: int) -> int:
    started = time.monotonic()
    chooser = Random(start_seed)
    seed = start_seed
    rows = _regressions()
    for explored in range(1, 1_000_001):
        result = _run_seed(seed)
        new = [failure for failure in result.failures if not any(failure.matches(row) for row in rows)]
        if new:
            assert len(rows) + len(new) <= _MAX_REGRESSIONS
            rows.extend({"seed": f.seed, "step": f.step, "invariant": f.invariant, "known": False} for f in new)
            with TemporaryDirectory(prefix=".sim-regressions-", dir=_REGRESSIONS.parent) as directory:
                staged = Path(directory) / _REGRESSIONS.name
                staged.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
                staged.replace(_REGRESSIONS)
            for failure in new:
                print(f"Failure: {failure}", file=sys.stderr)
            print(f"Seeds explored: {explored}. Recorded violations: {len(new)}. Start seed: {start_seed}.")
            return 1
        if time.monotonic() - started >= seconds:
            break
        seed = chooser.getrandbits(64)
    print(f"Seeds explored: {explored}. No new violations. Start seed: {start_seed}. "
          f"Time: {time.monotonic() - started:.2f} s.")
    return 0


def _replay(seed: int) -> int:
    result = _run_seed(seed)
    for failure in result.failures:
        print(f"Failure: {failure}", file=sys.stderr)
    print(f"Seed: {seed}. Scenarios: {result.scenarios}. Invariants checked: {result.checks}.")
    return int(bool(result.failures))


def _seed(value: str) -> int:
    seed = int(value)
    if not 0 <= seed <= _MAX_SEED:
        raise argparse.ArgumentTypeError("use an unsigned 64-bit seed")
    return seed


def _seconds(value: str) -> int:
    seconds = int(value)
    if not 1 <= seconds <= 86_400:
        raise argparse.ArgumentTypeError("use a time between 1 and 86400 seconds")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tidegauge.sim")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="replay fixed and recorded seeds")
    hunt = commands.add_parser("hunt", help="search for and record new violations")
    hunt.add_argument("--seconds", type=_seconds, required=True)
    hunt.add_argument("--start-seed", type=_seed, default=_DEFAULT_HUNT_SEED)
    replay = commands.add_parser("replay", help="replay a single seed")
    replay.add_argument("seed", type=_seed)
    args = parser.parse_args(argv)
    if args.command == "check":
        return _check()
    if args.command == "hunt":
        return _hunt(args.seconds, args.start_seed)
    assert args.command == "replay"
    return _replay(args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
