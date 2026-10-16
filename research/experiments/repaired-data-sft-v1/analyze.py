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

Uncertainty:
- exact Clopper–Pearson 95% intervals for every rate;
- an exact two-sided sign test on paired per-task solve rates, for B vs C (primary) and
  for each arm vs the base. With one attempt per task this is McNemar's exact test.

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
        --out results.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

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


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    return min(1.0, 2 * binom_cdf(min(b, c), n, 0.5))


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
    if unresolved == 0:
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
        "incorrect_pass_ci95": clopper_pearson(incorrect, n) if unresolved == 0 else None,
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

    report: dict[str, Any] = {
        "heldout_tasks": len(heldout),
        "incorrect_pass_replay_excluded": dict(sorted(replay_excluded.items())),
        "incorrect_pass_replay_mode": replay_mode,
        "incorrect_pass_replay_mode_reason": replay_mode_reason,
        "arms": {},
        "paired": {},
    }
    for name, by_task in arms.items():
        trials = [(t, n, v) for t, items in by_task.items() for n, v in items]
        measured = [v for _, _, v in trials if v is not None]
        solves = sum(measured)
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
            "measured": len(measured),
            "errors": len(trials) - len(measured),
            "solves": solves,
            "solve_rate": round(solves / len(measured), 4) if measured else None,
            "solve_ci95": clopper_pearson(solves, len(measured)),
            **incorrect_pass,
        }
        if len(trials) > len(by_task):
            report["arms"][name]["note"] = (
                "several attempts per task: the trial-level interval ignores within-task "
                "correlation; the paired test below works per task"
            )
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

    def task_rate(items: list[tuple[str, bool | None]]) -> float | None:
        values = [v for _, v in items if v is not None]
        return sum(values) / len(values) if values else None

    # Paired exact sign test per task (McNemar when there is one attempt per task).
    pairs = [("B", "C")] + [(n, "base") for n in arms if n != "base" and "base" in arms]
    for left, right in pairs:
        if left not in arms or right not in arms:
            continue
        rates = {
            t: (task_rate(arms[left][t]), task_rate(arms[right].get(t, []))) for t in arms[left]
        }
        common = {t: r for t, r in rates.items() if None not in r}
        better = sum(1 for lr, rr in common.values() if lr > rr)
        worse = sum(1 for lr, rr in common.values() if lr < rr)
        report["paired"][f"{left}_vs_{right}"] = {
            "tasks": len(common),
            f"{left}_better": better,
            f"{right}_better": worse,
            "mean_difference": round(sum(lr - rr for lr, rr in common.values()) / len(common), 4)
            if common
            else None,
            "exact_sign_p": round(mcnemar_exact(better, worse), 4),
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
