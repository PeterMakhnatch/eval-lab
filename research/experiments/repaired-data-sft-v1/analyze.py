"""Score the held-out evaluation of the repaired-data SFT arms.

Inputs:
- one `evallab rollout` `results.jsonl` per arm (`--arm NAME=PATH`, repeatable; include the
  untrained base as its own arm);
- the frozen held-out manifest (`--heldout`): MiMo data's `mimo.training-manifest/v1`
  (`heldout: list[str]` of bare source ids). Scoring is refused unless it says
  `scoring_allowed: true`;
- the arm exports' `summary.json` (`--exports`), used to prove that no held-out task was
  trained on;
- optionally, `--original-regrade` JSONL rows `{arm, trial, original_pass}`, written by
  `incorrect_pass.py`. Each row comes from replaying one held-out trial's submission on
  the task's *original* v5 grader.

Two definitions:
- **Solve:** reward 1 on the v6@10 held-out package, which is the only grade the trial
  receives.
- **Incorrect pass:** the original grader passes a submission that the v6@10 grader
  rejects. The model would have been rewarded for wrong work under the original grader.
  - Only v6-failing trials need a replay; a v6 pass cannot be an incorrect pass.
  - **Population:** held-out trials minus the tasks given as `--replay-exclude
    TASK=REASON`. These are out of population by design. On 2026-10-16 that means the
    four environment-invalid tasks 001199, 001246, 001590 and 002451, which QA Trace
    excludes at plan as `g1x`. Their trials never hold the rate at unmeasured. They are
    counted, by reason, in `incorrect_pass_excluded`.
  - `incorrect_pass_rate` is reported only when every in-population measured v6 failure
    has a measured original-grader replay. Its denominator is the in-population measured
    trials. An arm that passes every trial on v6 is therefore structurally 0.
  - With any unresolved in-population v6 failure, the rate is `null` and
    `incorrect_pass_status` says `partial` or `unmeasured`. `incorrect_pass_bounds` then
    gives the range: every unresolved failure counted as correct, and every one counted
    as an incorrect pass. Missing replays are never rendered as 0.
  - `incorrect_pass_unresolved_failures` and `incorrect_pass_excluded` are always printed.
  - **Replay mode** (`--replay-plan`, QA Trace's `itemized-plan.json`):
    - In `attempt-1` mode, used when the full plan exceeds the execution envelope, only
      attempt 1 of each task is replayed. The population, and so the denominator, is
      then the attempt-1 trials. Later attempts are counted as excluded with reason
      `attempt>1 (attempt-1 replay mode)`, so they neither dilute the rate nor hold it at
      unmeasured.
    - An `all attempts` plan changes nothing.
    - The mode must be one of QA Trace's exact producer strings (`REPLAY_MODES`):
      `attempt-1` or `all attempts`, with a space. Anything else is refused, including
      near-misses such as `all-attempts`. The plan's `mode_reason` is printed beside the
      mode.
    - Attempts come from rollout trial names (`…-a<N>`). A name without that suffix is
      attempt 1.
    - The paired pin block follows the same mode. The headline and unpaired blocks are
      unaffected.

Validity-flagged pins (`--pin`, repeatable; 2026-10-16: 000657, 000371, 001686). Research
wants these reported both ways:
- **Headline:** every metric above uses only trials on each task's frozen v6@10 package,
  identified by its digest directory in `package-pairs.csv`, across all 33 tasks, pins
  included.
- **Diagnostic, unpaired** (`pins.arms.<arm>.unpaired`): Infra's extra pin trials on the
  EnvCheck *raw* repaired variants, which are any other package of a pin. Per arm it
  gives the v6 and repaired pin solve rates and their gap. These are separate trials of
  different submissions, so the gap only estimates the pin incorrect-pass rate.
- **Diagnostic, paired** (`pins.arms.<arm>.paired`): `--pin-regrade` rows
  `{arm, trial, repaired_pass}`. QA Trace's `pin_pairs.py` writes the regrade list and
  `incorrect_pass.py collect --field repaired_pass` collects the results. Each v6-passing
  frozen-v6 pin trial is regraded from its own snapshot on EnvCheck's *v6-ported*
  repaired variant (`--pin-manifest`, i.e. `heldout-pin-repaired-graders.csv`). A pin
  incorrect pass is a v6 pass that variant rejects. The denominator is the v6 pin passes
  with a measured regrade. In attempt-1 replay mode both terms count attempt-1 trials
  only (`attempts_counted`).
- **Required caveat:** the paired and unpaired pin numbers come from sibling repairs,
  not byte-identical graders. `pins.grader_note` states this with both digest sets.
  Print it wherever pin numbers appear.
- A row on any other package of a task that is not a pin is refused.

Training exposure (`--receipt NAME=receipts/<run>.json` for each trained arm; required for
B and C). Each arm reports rows, assistant label tokens, epochs, optimizer steps and
assistant-token presentations (label tokens × epochs). Equal steps do not mean equal
token exposure: final-v4 B saw 1,141,500 presentations and C 844,320. `B_vs_C` therefore
carries an `exposure_caveat` with the ratio.

Coverage and uncertainty (`estimand_notes` in the output states them):
- every arm reports `solve_coverage` (`complete`, `observed_subset` or `unmeasured`),
  `measured_tasks` (n/33), `measured_attempts` (n/planned), `missing_task_attempts`, and
  the missing and unmeasured task ids. Planned attempts come from the required
  `--attempts`, in QA Trace's canonical grammar: a uniform count (`--attempts 2`, the
  authorized schedule) or one count per arm with each of base, A, B and C exactly once
  (`--attempts base=1,A=1,B=2,C=2`). Anything else is refused. The schedule is printed as
  `planned_attempts`: an integer when uniform, a per-arm map otherwise. Coverage is
  judged per arm against its own schedule, so with the mixed schedule base and A are
  complete at 33 cells and B and C at 66. An observed subset is never presented as a
  full 33-task result, and missing slots are never imputed.
- every headline row must be a distinct cell of its arm's schedule: held-out task x
  attempt 1..N, read from the trial name's `-a<N>` suffix. A row on an unauthorized cell
  (such as a3 under a 2-attempt schedule) or a second row on the same cell makes the
  scorer refuse with exit 2. Rows are never filtered or clamped, so
  measured attempts can't exceed planned ones, and a missing a2 plus an extra a3 can't
  pass as complete.
- exact Clopper–Pearson 95% intervals over measured attempts. They ignore within-task
  correlation between attempts, are not cluster-adjusted, and are not intervals for any
  difference between arms.
- paired comparisons (B vs C, primary, and each arm vs base) work on the common
  measured-task cohort. They report its size, the attempts each arm contributes and
  whether attempts per task are equal, plus the equal-task mean difference and an exact
  two-sided sign test of task wins and losses (ties discarded). The sign test is not a
  test of the size of the mean difference.
  - With no common measured task, the status is `unmeasured` and the difference and p
    are null.
  - Observed all-tied tasks give p = 1.
  - p is printed at full precision with its exact fraction, never rounded to 0.

    uv run python research/experiments/repaired-data-sft-v1/analyze.py \
        --arm base=… --arm A=… --arm B=… --arm C=… \
        --heldout heldout.json --exports <exports>/summary.json [--original-regrade rows.jsonl] \
        --pin format-code-task-000657 --pin format-code-task-000371 \
        --pin format-code-task-001686 \
        [--pin-regrade pin-regrade.jsonl --pin-manifest heldout-pin-repaired-graders.csv] \
        --receipt A=receipts/rds1-a.json --receipt B=receipts/rds1-b-v4e4.json \
        --receipt C=receipts/rds1-c-v4e4.json \
        --replay-exclude format-code-task-001199=g1x --replay-exclude format-code-task-001246=g1x \
        --replay-exclude format-code-task-001590=g1x --replay-exclude format-code-task-002451=g1x \
        --attempts 2 --out results.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

ESTIMAND_NOTES = {
    "solve_rate": "passes / measured attempts on the frozen v6 packages; not pass-at-k per task",
    "solve_ci95": (
        "Clopper-Pearson over measured attempts: it ignores within-task correlation between "
        "attempts, is not cluster-adjusted, and is not an interval for any between-arm "
        "difference"
    ),
    "paired": (
        "equal-weight mean of per-task observed solve-rate differences on the common "
        "measured-task cohort; exact_sign_p is a two-sided sign test of task win/loss "
        "direction (ties discarded), not a CI or test of the mean-difference magnitude. "
        "No interval for a between-arm difference is produced"
    ),
    "coverage": (
        "missing task-attempts (no row) and errored rows are unmeasured and never imputed; "
        "solve_coverage 'observed_subset' means the rate covers only the measured subset"
    ),
}
ATTEMPT_MODE_REASON = "attempt>1 (attempt-1 replay mode)"
#: QA Trace itemized-plan.json `mode` enum: the producer's exact strings (Research,
#: 2026-10-16). Anything else, near-misses included, is refused.
REPLAY_MODES = frozenset({"attempt-1", "all attempts"})


def binom_cdf(k: int, n: int, p: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))


def _root(f, target: float) -> float:
    """p in [0, 1] with f(p) == target, for f increasing in p (bisection)."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < target else (lo, mid)
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float] | None:
    """Exact two-sided interval: P(X >= k | lower) = alpha/2, P(X <= k | upper) = alpha/2."""
    if n == 0:
        return None
    lower = 0.0 if k == 0 else _root(lambda p: 1 - binom_cdf(k - 1, n, p), alpha / 2)
    upper = 1.0 if k == n else _root(lambda p: 1 - binom_cdf(k, n, p), 1 - alpha / 2)
    return round(lower, 4), round(upper, 4)


def exact_sign_p(wins: int, losses: int) -> Fraction | None:
    """Exact two-sided sign-test p from per-task wins/losses (ties discarded), or None
    when no task was decided. Kept as a Fraction so tiny values are never rounded to 0."""
    n = wins + losses
    if n == 0:
        return None
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1))
    return min(Fraction(1), Fraction(2 * tail, 2**n))


Trials = dict[str, list[tuple[str, bool | None]]]


ARMS = ("base", "A", "B", "C")
_COUNT = r"[1-9][0-9]*"
_ARM_COUNT = re.compile(rf"(base|A|B|C)=({_COUNT})")


def parse_attempts(spec: str) -> int | dict[str, int]:
    """QA Trace's canonical attempts grammar (prepare_batch sha256 65e35a56…), exactly:
    `[1-9][0-9]*` for a uniform count, or comma-separated `(base|A|B|C)=[1-9][0-9]*`
    naming each arm exactly once, in any order. Anything else raises ValueError: 0, a
    leading zero, a sign, whitespace, a missing, repeated or unknown arm, an empty or
    trailing-comma part, or a mix such as `2,base=1`."""
    if re.fullmatch(_COUNT, spec, flags=re.ASCII):
        return int(spec)
    schedule: dict[str, int] = {}
    for part in spec.split(","):
        match = _ARM_COUNT.fullmatch(part)
        if match is None or match.group(1) in schedule:
            raise ValueError(f"--attempts {spec!r}: invalid or repeated part {part!r}")
        schedule[match.group(1)] = int(match.group(2))
    if set(schedule) != set(ARMS):
        raise ValueError(f"--attempts {spec!r}: needs each of {', '.join(ARMS)} exactly once")
    return schedule


def validate_cells(by_task: Trials, heldout: list[str], planned_attempts: int) -> None:
    """Refuse unless every row is a distinct cell of the authorized schedule.

    The schedule is held-out task x attempt 1..planned_attempts. A row on a task outside
    it, a row on a later attempt (e.g. a3 under a 2-attempt schedule) and a second row on
    the same task-attempt cell are all invalid. They raise, and are never clamped or
    silently filtered, so a run with an extra a3 and a missing a2 cannot pass as complete.
    """
    authorized = set(heldout)
    seen: set[tuple[str, int]] = set()
    unauthorized: list[str] = []
    duplicate: list[str] = []
    for task, items in by_task.items():
        for name, _ in items:
            attempt = attempt_of(name)
            if task not in authorized or not 1 <= attempt <= planned_attempts:
                unauthorized.append(f"{task}:a{attempt}")
            elif (task, attempt) in seen:
                duplicate.append(f"{task}:a{attempt}")
            seen.add((task, attempt))
    if unauthorized or duplicate:
        raise ValueError(
            f"cells outside the authorized {len(heldout)}-task x attempts 1..{planned_attempts} "
            f"schedule: unauthorized {sorted(unauthorized)}, duplicate {sorted(duplicate)}"
        )


def arm_coverage(by_task: Trials, heldout: list[str], planned_attempts: int) -> dict[str, Any]:
    """Solve rate with explicit observed-subset coverage for one arm.

    Rows must be distinct cells of the authorized schedule (`validate_cells`), so
    measured attempts never exceed planned ones. Missing slots (cells with no row) and
    errored rows are both unmeasured, never imputed. `solve_coverage` is `complete` only
    when every authorized cell was measured, `observed_subset` when some were, and
    `unmeasured` when none were.
    """
    validate_cells(by_task, heldout, planned_attempts)
    present = [v for t in heldout for _, v in by_task.get(t, [])]
    measured = [v for v in present if v is not None]
    rates = {
        t: sum(v for _, v in items if v) / sum(v is not None for _, v in items)
        for t, items in by_task.items()
        if any(v is not None for _, v in items)
    }
    planned = len(heldout) * planned_attempts
    solves = sum(measured)
    if not measured:
        coverage = "unmeasured"
    elif len(measured) == planned and len(rates) == len(heldout):
        coverage = "complete"
    else:
        coverage = "observed_subset"
    return {
        "solve_coverage": coverage,
        "planned_slots": planned,
        "present_rows": len(present),
        "missing_rows": planned - len(present),
        "error_rows": len(present) - len(measured),
        "measured_tasks": f"{len(rates)}/{len(heldout)}",
        "measured_attempts": f"{len(measured)}/{planned}",
        "missing_task_attempts": planned - len(measured),
        "missing_task_ids": sorted(set(heldout) - set(by_task)),
        "unmeasured_task_ids": sorted(set(heldout) - set(rates)),
        "solves": solves,
        "solve_rate": round(solves / len(measured), 4) if measured else None,
        "solve_ci95": clopper_pearson(solves, len(measured)),
        "observed_task_mean": round(sum(rates.values()) / len(rates), 4) if rates else None,
    }


def paired_comparison(
    left: Trials,
    right: Trials,
    heldout: list[str],
    names: tuple[str, str],
    planned_attempts: int | tuple[int, int],
) -> dict[str, Any]:
    """Task-level paired comparison on the common measured-task cohort.

    Each task is weighted equally by its observed per-arm solve rate; the sign test counts
    task wins and losses, discarding ties. With no common measured task the comparison is
    `unmeasured` (difference and p null). All-tied observed tasks give p = 1. Both arms
    must hold only distinct cells of their own schedule (`validate_cells`); a single
    count applies to both arms, a pair is (left, right).
    """
    left_n, right_n = (
        planned_attempts if isinstance(planned_attempts, tuple) else (planned_attempts,) * 2
    )
    validate_cells(left, heldout, left_n)
    validate_cells(right, heldout, right_n)
    ln, rn = names

    def rate(items: list[tuple[str, bool | None]]) -> tuple[float, int] | None:
        values = [v for _, v in items if v is not None]
        return (sum(values) / len(values), len(values)) if values else None

    per_task = {t: (rate(left.get(t, [])), rate(right.get(t, []))) for t in heldout}
    common = {t: (lr, rr) for t, (lr, rr) in per_task.items() if lr and rr}
    wins = sum(1 for lr, rr in common.values() if lr[0] > rr[0])
    losses = sum(1 for lr, rr in common.values() if lr[0] < rr[0])
    p = exact_sign_p(wins, losses) if common else None
    if common and wins + losses == 0:
        p = Fraction(1)
    l_attempts = sum(lr[1] for lr, _ in common.values())
    r_attempts = sum(rr[1] for _, rr in common.values())
    equal = all(lr[1] == rr[1] for lr, rr in common.values())
    out: dict[str, Any] = {
        "status": "measured" if common else "unmeasured",
        "tasks": len(common),
        "common_task_cohort": f"{len(common)}/{len(heldout)}",
        "missing_or_unpaired_task_ids": sorted(set(heldout) - set(common)),
        f"{ln}_attempts_in_cohort": l_attempts,
        f"{rn}_attempts_in_cohort": r_attempts,
        "equal_attempts_per_task": equal if common else None,
        f"{ln}_better": wins,
        f"{rn}_better": losses,
        "tied_tasks": len(common) - wins - losses,
        "mean_difference": round(sum(lr[0] - rr[0] for lr, rr in common.values()) / len(common), 4)
        if common
        else None,
        "exact_sign_p": float(p) if p is not None else None,
        "exact_sign_fraction": str(p) if p is not None else None,
    }
    if common and not equal:
        out["attempts_caveat"] = (
            f"unequal attempts per task ({ln} {l_attempts} vs {rn} {r_attempts} in the cohort): "
            "the per-task rates average different numbers of attempts, so this equal-task "
            "comparison is not the same estimand as the per-arm trial-level solve rates"
        )
    return out


def task_of(row: dict[str, Any]) -> str:
    task = row.get("task") or row.get("task_id") or row.get("task_name")
    if not task:
        raise ValueError(f"result row has no task: {sorted(row)}")
    return str(task)


def passed(row: dict[str, Any]) -> bool | None:
    """Rollout `passed` (reward >= 1), or None for an errored or unmeasured trial."""
    if row.get("error"):
        return None
    if row.get("passed") is not None:
        return bool(row["passed"])
    reward = row.get("reward")
    return None if reward is None else float(reward) >= 1.0


def norm(task: str) -> str:
    """Bare MiMo source id from an id, a namespaced id, or a task-store package path."""
    for part in reversed(task.split("/")):
        if "__" in part:
            return part.split("__", 1)[1]
    return task.rsplit("/", 1)[-1]


def v6_digests(package_pairs: Path) -> dict[str, str]:
    """Bare task id -> 12-hex digest directory of its frozen v6@10 held-out package."""
    with package_pairs.open(encoding="utf-8") as handle:
        return {
            r["task_id"]: r["v6_package_digest"].removeprefix("sha256:")[:12]
            for r in csv.DictReader(handle)
        }


def grading_role(row: dict[str, Any], v6: dict[str, str], pins: set[str]) -> str:
    """`headline` for a trial on the task's frozen v6 package; `pin_diagnostic` for a
    trial on any other package of a validity-flagged pin (Infra's repaired-variant cells).

    Rollout rows name the package path, ending in its 12-hex digest directory. Anything
    else is refused, so a non-v6 grade can never enter the headline.
    """
    task = task_of(row)
    tid, digest = norm(task), task.rstrip("/").rsplit("/", 1)[-1]
    if digest == v6.get(tid):
        return "headline"
    if tid in pins:
        return "pin_diagnostic"
    raise ValueError(f"{task}: not the frozen v6 package of {tid} and not a pin")


def attempt_of(name: str) -> int:
    """Attempt index from an `evallab rollout` trial name (`…-a<N>`; no suffix: 1)."""
    match = re.search(r"-a(\d+)$", name)
    return int(match.group(1)) if match else 1


def incorrect_pass_summary(
    verdicts: list[tuple[str, int, bool, bool | None]],
    excluded: dict[str, str],
    *,
    attempt_1_only: bool = False,
) -> dict[str, Any]:
    """Per-arm incorrect-pass fields from (task, attempt, v6_pass, original_pass) per
    measured trial.

    `original_pass` is the original-grader replay verdict, or None when no replay was
    measured. Trials of tasks in `excluded` (task -> reason) are outside the replay
    population: they are counted by reason and nothing else. With `attempt_1_only` (QA
    Trace's attempt-1 replay mode) so are attempts after the first. Within the population
    a v6 pass is never an incorrect pass and needs no replay. The rate is reported only
    when every in-population v6 failure is resolved. Otherwise the result is `null` with
    bounds, so missing replays never read as a measured 0.
    """
    out_of_population: dict[str, int] = {}
    population: list[tuple[bool, bool | None]] = []
    for task, attempt, v6, orig in verdicts:
        reason = excluded.get(task)
        if reason is None and attempt_1_only and attempt > 1:
            reason = ATTEMPT_MODE_REASON
        if reason is not None:
            out_of_population[reason] = out_of_population.get(reason, 0) + 1
        else:
            population.append((v6, orig))
    n = len(population)
    passes = sum(1 for v6, _ in population if v6)
    failures = [orig for v6, orig in population if not v6]
    resolved = [orig for orig in failures if orig is not None]
    unresolved = len(failures) - len(resolved)
    incorrect = sum(resolved)
    if n == 0:
        status = "unmeasured"  # an empty population is no evidence, not a complete zero
    elif unresolved == 0:
        status = "complete"
    elif resolved:
        status = "partial"
    else:
        status = "unmeasured"
    return {
        "incorrect_pass_status": status,
        "incorrect_pass_population": n,
        "incorrect_pass_excluded": out_of_population,
        "incorrect_pass_measured": passes + len(resolved),
        "incorrect_pass_v6_passes": passes,
        "incorrect_pass_replayed_failures": len(resolved),
        "incorrect_pass_unresolved_failures": unresolved,
        "incorrect_passes": incorrect,
        "incorrect_pass_rate": round(incorrect / n, 4) if n and unresolved == 0 else None,
        "incorrect_pass_ci95": clopper_pearson(incorrect, n) if n and unresolved == 0 else None,
        "incorrect_pass_bounds": [round(incorrect / n, 4), round((incorrect + unresolved) / n, 4)]
        if n and unresolved
        else None,
    }


def paired_pin_summary(
    v6_trials: list[tuple[str, bool]], repaired: dict[str, bool], *, attempt_1_only: bool
) -> dict[str, Any]:
    """Paired pin counts for one arm from its frozen-v6 pin trials (name, v6_pass) and the
    repaired-variant regrades (name -> repaired_pass).

    A pin incorrect pass is a v6 pass that the repaired variant rejects; the denominator
    is the v6 pin passes with a measured regrade. In attempt-1 replay mode only attempt-1
    trials count, in both numerator and denominator, even if later attempts were regraded.
    """
    v6_passes = [n for n, v in v6_trials if v and not (attempt_1_only and attempt_of(n) > 1)]
    measured = [n for n in v6_passes if n in repaired]
    flips = sum(1 for n in measured if not repaired[n])
    return {
        "attempts_counted": "attempt 1 only" if attempt_1_only else "all",
        "v6_pin_passes": len(v6_passes),
        "regrade_measured": len(measured),
        "pin_incorrect_passes": flips,
        "pin_incorrect_pass_rate": round(flips / len(measured), 4) if measured else None,
        "pin_incorrect_pass_ci95": clopper_pearson(flips, len(measured)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arm", action="append", required=True, help="NAME=results.jsonl")
    ap.add_argument("--heldout", type=Path, required=True)
    ap.add_argument("--exports", type=Path, required=True, help="to_export.py summary.json")
    ap.add_argument("--original-regrade", type=Path)
    ap.add_argument("--pin-regrade", type=Path, help="paired pin regrade rows (repaired_pass)")
    ap.add_argument(
        "--pin-manifest", type=Path, help="heldout-pin-repaired-graders.csv (paired graders)"
    )
    ap.add_argument(
        "--package-pairs",
        type=Path,
        help="MiMo data's package-pairs.csv (default: beside --heldout)",
    )
    ap.add_argument(
        "--pin",
        action="append",
        default=[],
        help="validity-flagged held-out task; its non-v6 rows are the repaired-variant diagnostic",
    )
    ap.add_argument(
        "--attempts",
        type=str,
        required=True,
        help="planned attempts per task: a uniform count (2, the authorized schedule) or "
        "base=N,A=N,B=N,C=N with each arm exactly once",
    )
    ap.add_argument(
        "--replay-plan",
        type=Path,
        help="QA Trace itemized-plan.json; its `mode` is 'attempt-1' or 'all attempts'",
    )
    ap.add_argument(
        "--replay-exclude",
        action="append",
        default=[],
        help="TASK=REASON: held-out task outside the incorrect-pass replay population",
    )
    ap.add_argument(
        "--receipt",
        action="append",
        default=[],
        help="NAME=training receipt JSON (sft.py), for each trained arm",
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    held = json.loads(args.heldout.read_text())
    if held.get("scoring_allowed") is not True:
        print("REFUSED: held-out manifest does not set scoring_allowed: true", file=sys.stderr)
        return 2
    heldout = {norm(t) for t in held["heldout"]}
    exports = json.loads(args.exports.read_text())
    trained_tasks: set[str] = set()
    for arm in ("A", "B", "C", "all"):
        manifest_path = args.exports.parent / arm / "manifest.json"
        if manifest_path.is_file():
            trained_tasks |= {
                norm(t) for t in json.loads(manifest_path.read_text()).get("tasks", [])
            }
    source = exports.get("source_manifest", {})
    # All of build_arms' tasks (42 on 2026-10-16), including rows the eligibility rule dropped.
    trained_tasks |= {norm(t) for t in source.get("tasks", [])}
    if not source.get("tasks"):
        print("REFUSED: exports summary lacks build_arms' task list", file=sys.stderr)
        return 2
    leak = sorted(heldout & trained_tasks)
    if leak:
        print(f"REFUSED: {len(leak)} held-out task(s) were trained on: {leak[:5]}", file=sys.stderr)
        return 2

    v6 = v6_digests(args.package_pairs or args.heldout.parent / "package-pairs.csv")
    pins = {norm(p) for p in args.pin}
    if not pins <= heldout:
        print(f"REFUSED: pins not held out: {sorted(pins - heldout)}", file=sys.stderr)
        return 2
    replay_excluded: dict[str, str] = {}
    for spec in args.replay_exclude:
        task, _, reason = spec.partition("=")
        if not reason or norm(task) not in heldout:
            print(
                f"REFUSED: --replay-exclude needs a held-out TASK=REASON: {spec}", file=sys.stderr
            )
            return 2
        replay_excluded[norm(task)] = reason

    attempt_1_only = False
    replay_mode = replay_mode_reason = None
    if args.replay_plan:
        plan = json.loads(args.replay_plan.read_text())
        replay_mode, replay_mode_reason = plan.get("mode"), plan.get("mode_reason")
        # Exact enum tokens only; a prose value is never the contract.
        if replay_mode not in REPLAY_MODES:
            print(f"REFUSED: unknown replay plan mode {replay_mode!r}", file=sys.stderr)
            return 2
        attempt_1_only = replay_mode == "attempt-1"
    # One list of (trial name, passed) per task: `evallab rollout --attempts` gives several.
    # Headline: trials on the frozen v6 package. Diagnostic: pin trials on a repaired variant.
    arms: dict[str, dict[str, list[tuple[str, bool | None]]]] = {}
    diagnostic: dict[str, dict[str, list[tuple[str, bool | None]]]] = {}
    unpaired_digests: dict[str, set[str]] = {}
    for spec in args.arm:
        name, _, path = spec.partition("=")
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        by_task: dict[str, list[tuple[str, bool | None]]] = {}
        pin_rows: dict[str, list[tuple[str, bool | None]]] = {}
        for row in rows:
            task = norm(task_of(row))
            if task not in heldout:
                print(f"REFUSED: arm {name} ran non-held-out task {task}", file=sys.stderr)
                return 2
            try:
                role = grading_role(row, v6, pins)
            except ValueError as exc:
                print(f"REFUSED: arm {name}: {exc}", file=sys.stderr)
                return 2
            target = by_task if role == "headline" else pin_rows
            target.setdefault(task, []).append((str(row.get("name") or task), passed(row)))
            if role == "pin_diagnostic":
                unpaired_digests.setdefault(task, set()).add(task_of(row).rstrip("/")[-12:])
        arms[name] = by_task
        diagnostic[name] = pin_rows

    # Incorrect-pass replays, keyed by (arm, trial name): original-grader verdict.
    original: dict[tuple[str, str], bool] = {}
    if args.original_regrade:
        for line in args.original_regrade.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                original[(r["arm"], r["trial"])] = bool(r["original_pass"])

    heldout_ids = sorted(heldout)
    try:
        schedule = parse_attempts(args.attempts)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    def attempts_for(arm: str) -> int:
        return schedule if isinstance(schedule, int) else schedule[arm]

    for name, by_task in arms.items():
        if not isinstance(schedule, int) and name not in schedule:
            print(
                f"REFUSED: arm {name} has no entry in --attempts {args.attempts}", file=sys.stderr
            )
            return 2
        try:
            validate_cells(by_task, heldout_ids, attempts_for(name))
        except ValueError as exc:
            print(f"REFUSED: arm {name}: {exc}", file=sys.stderr)
            return 2
    report: dict[str, Any] = {
        "heldout_tasks": len(heldout),
        "planned_attempts": schedule,
        "estimand_notes": ESTIMAND_NOTES,
        "incorrect_pass_replay_excluded": dict(sorted(replay_excluded.items())),
        "incorrect_pass_replay_mode": replay_mode,
        "incorrect_pass_replay_mode_reason": replay_mode_reason,
        "arms": {},
        "paired": {},
    }
    if not isinstance(schedule, int):
        # Canonical per-arm authorized attempt cells, keys sorted (A, B, C, base).
        report["authorized_attempts"] = {
            arm: list(range(1, schedule[arm] + 1)) for arm in sorted(schedule)
        }
    for name, by_task in arms.items():
        trials = [(t, n, v) for t, items in by_task.items() for n, v in items]
        verdicts = [
            (t, attempt_of(n), v, original.get((name, n))) for t, n, v in trials if v is not None
        ]
        incorrect_pass = incorrect_pass_summary(
            verdicts, replay_excluded, attempt_1_only=attempt_1_only
        )
        report["arms"][name] = {
            "examples": exports.get("arms", {}).get(name, {}).get("rows"),
            "tasks": len(by_task),
            "trials": len(trials),
            "measured": sum(v is not None for _, _, v in trials),
            "errors": sum(v is None for _, _, v in trials),
            **arm_coverage(by_task, heldout_ids, attempts_for(name)),
            **incorrect_pass,
        }
    exposure: dict[str, dict[str, int]] = {}
    for spec in args.receipt:
        name, _, path = spec.partition("=")
        receipt = json.loads(Path(path).read_text())
        remote, epochs = receipt["remote"], int(receipt["train"]["sft"]["num_epochs"])
        exposure[name] = {
            "rows": int(remote["train_rows"]),
            "assistant_label_tokens": int(remote["trained_tokens"]),
            "epochs": epochs,
            "optimizer_steps": int(remote["parameter_update"]["optimizer_steps"]),
            "assistant_token_presentations": int(remote["trained_tokens"]) * epochs,
        }
    for name, entry in exposure.items():
        if name in report["arms"]:
            report["arms"][name]["training_exposure"] = entry
    if {"B", "C"} <= set(arms) and not {"B", "C"} <= set(exposure):
        print("REFUSED: B vs C needs --receipt for both B and C", file=sys.stderr)
        return 2

    # Task-level paired comparisons on the common measured-task cohort.
    pairs = [("B", "C")] + [(n, "base") for n in arms if n != "base" and "base" in arms]
    for left, right in pairs:
        if left not in arms or right not in arms:
            continue
        pair = (attempts_for(left), attempts_for(right))
        report["paired"][f"{left}_vs_{right}"] = paired_comparison(
            arms[left], arms[right], heldout_ids, (left, right), pair
        )
        if not isinstance(schedule, int):
            report["paired"][f"{left}_vs_{right}"]["attempts_counted"] = {
                left: pair[0],
                right: pair[1],
            }
    if "B_vs_C" in report["paired"]:
        b, c = (exposure[n]["assistant_token_presentations"] for n in ("B", "C"))
        report["paired"]["B_vs_C"]["exposure_caveat"] = (
            f"Equal rows, epochs and optimizer steps are not equal token exposure: B saw "
            f"{b:,} assistant-token presentations vs C {c:,} ({b / c:.2f}x). A B-vs-C "
            "difference mixes data selection with the difference in supervised tokens."
        )

    # Pin diagnostics on the validity-flagged pins: unpaired (Infra's separate trials on the
    # raw repaired variants) and paired (each v6 pass regraded on the v6-ported variant).
    if pins:
        if args.pin_regrade and not args.pin_manifest:
            print("REFUSED: --pin-regrade needs --pin-manifest", file=sys.stderr)
            return 2
        paired_digests: dict[str, str] = {}
        if args.pin_manifest:
            with args.pin_manifest.open(encoding="utf-8") as handle:
                paired_digests = {
                    r["task_id"]: r["final_digest"].removeprefix("sha256:")[:12]
                    for r in csv.DictReader(handle)
                }
        repaired: dict[tuple[str, str], bool] = {}
        if args.pin_regrade:
            for line in args.pin_regrade.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    repaired[(r["arm"], r["trial"])] = bool(r["repaired_pass"])
        unpaired = {t: sorted(unpaired_digests.get(t, ())) for t in sorted(pins)}
        paired = {t: paired_digests.get(t) for t in sorted(pins)}
        report["pins"] = {
            "tasks": sorted(pins),
            "grader_note": (
                "Paired and unpaired pin numbers come from sibling repairs, not "
                "byte-identical graders: paired regrades use EnvCheck's v6-ported repaired "
                f"variants {paired}; Infra's unpaired diagnostic cells use the raw repaired "
                f"variants {unpaired}."
            ),
            "paired_graders": paired,
            "unpaired_graders": unpaired,
            "arms": {},
        }
        for name in arms:
            v6_trials = [(n, v) for t in pins for n, v in arms[name].get(t, []) if v is not None]
            v6_pin = [v for _, v in v6_trials]
            rep_pin = [v for t in pins for _, v in diagnostic[name].get(t, []) if v is not None]
            v6_rate = sum(v6_pin) / len(v6_pin) if v6_pin else None
            rep_rate = sum(rep_pin) / len(rep_pin) if rep_pin else None
            report["pins"]["arms"][name] = {
                "unpaired": {
                    "v6_measured": len(v6_pin),
                    "v6_solves": sum(v6_pin),
                    "v6_solve_ci95": clopper_pearson(sum(v6_pin), len(v6_pin)),
                    "repaired_measured": len(rep_pin),
                    "repaired_solves": sum(rep_pin),
                    "repaired_solve_ci95": clopper_pearson(sum(rep_pin), len(rep_pin)),
                    "pass_gap": round(v6_rate - rep_rate, 4)
                    if v6_rate is not None and rep_rate is not None
                    else None,
                    "repaired_trials_by_task": {
                        t: len(diagnostic[name].get(t, [])) for t in sorted(pins)
                    },
                },
                "paired": paired_pin_summary(
                    v6_trials,
                    {n: rp for (arm, n), rp in repaired.items() if arm == name},
                    attempt_1_only=attempt_1_only,
                )
                if args.pin_regrade
                else "pending: no --pin-regrade rows",
            }
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
