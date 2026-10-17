"""Analyze named repaired-data SFT arms without changing the frozen raw schedules.

Usage: python analyze.py --plan PLAN.json --out REPORT.json

Paths are relative to PLAN.json. Required plan fields are ``heldout_manifest`` and
``arms``. Each arm has ``results`` (rollout JSONL), ``authorized_attempts`` (positive
integer), optional ``authorized_task_ids`` (default: all frozen heldout tasks), and
optional ``model_metadata`` (object). Other plan fields:

* ``heldout_sha256``: optional expected hash of the heldout manifest bytes.
* ``package_pairs``: frozen package-pairs.csv; defaults beside the heldout manifest.
* ``cohort_task_ids``: requested view, defaulting to all frozen heldout tasks. It must
  be authorized in every arm, and never changes raw validation or raw denominators.
* ``selected_attempts``: distinct positive attempt IDs, default [1], authorized in
  every arm. Every selected attempt must be measured for a task to enter a headline
  comparison. Later attempts never fill a missing selected attempt.
* ``bootstrap_replicates``: positive integer, default 10000; ``seed``: integer,
  default 42. CIs resample equally weighted paired TASK differences, not attempts.
* ``pin_task_ids``: known heldout pins, default []. Only v1's grading_role may split
  their non-frozen-package rows into separately validated, diagnostic-only cells.
* ``paired_pin_regrades``: optional JSONL with arm, trial, repaired_pass (bool/null),
  and optional error; requires ``pin_manifest`` (v1's paired-grader CSV). Regrades
  never affect headline outcomes, cohorts, or missingness.

Optional ``schema`` is ``repaired-data-sft.analysis-plan/v2``. Unknown fields and
repeated JSON keys are refused. The complete report is constructed before any
output is written; a refused plan produces no new report. Existing output and
input files are never overwritten. This script imports v1's pure parsing, package
role, schedule-validation, sign-test and paired-pin helpers; v1 is not modified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import runpy
import sys
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import Any, NamedTuple


V1_PATH = Path(__file__).resolve().parents[1] / "repaired-data-sft-v1/analyze.py"
V1 = runpy.run_path(str(V1_PATH))
PLAN_SCHEMA = "repaired-data-sft.analysis-plan/v2"
REPORT_SCHEMA = "repaired-data-sft.analysis/v2"
Trials = dict[str, list[tuple[str, bool | None]]]


class Cell(NamedTuple):
    name: str
    outcome: bool | None
    reason: str | None


Cells = dict[tuple[str, int], Cell]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"repeated JSON key {key!r}")
        result[key] = value
    return result


def _json(data: str | bytes) -> Any:
    return json.loads(data, object_pairs_hook=_unique_object)


def _source(path: Path) -> tuple[bytes, dict[str, Any]]:
    data = path.read_bytes()
    return data, {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _path(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty path string")
    path = Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def _fields(value: Any, allowed: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label}: unknown fields {sorted(unknown)}")


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _task_ids(value: Any, label: str, *, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not empty):
        raise ValueError(f"{label} must be {'an' if empty else 'a nonempty'} array of task IDs")
    if any(not isinstance(task, str) or not task.strip() for task in value):
        raise ValueError(f"{label} must contain nonempty task ID strings")
    tasks = [V1["norm"](task) for task in value]
    if len(set(tasks)) != len(tasks):
        raise ValueError(f"{label} contains repeated normalized task IDs")
    return sorted(tasks)


def _subset(tasks: list[str], authorized: list[str], label: str) -> None:
    extra = sorted(set(tasks) - set(authorized))
    if extra:
        raise ValueError(f"{label}: unauthorized task IDs {extra}")


def _fraction(value: Fraction | None) -> str | None:
    return str(value) if value is not None else None


def _coverage_status(measured: int, planned: int) -> str:
    if not measured:
        return "unmeasured"
    return "complete" if measured == planned else "observed_subset"


def coverage(cells: Cells, tasks: list[str], attempts: list[int]) -> dict[str, Any]:
    """Describe explicit slots of an already raw-validated arm; never impute outcomes."""
    measured: list[bool] = []
    per_task: dict[str, Any] = {}
    missing: list[dict[str, Any]] = []
    unmeasured: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    present_count = 0
    for task in tasks:
        values: list[bool] = []
        measured_ids: list[int] = []
        present_ids: list[int] = []
        for attempt in attempts:
            cell = cells.get((task, attempt))
            if cell is None:
                missing.append({"task_id": task, "attempt": attempt})
                reason = "missing_row"
            else:
                present_count += 1
                present_ids.append(attempt)
                reason = cell.reason
                if cell.outcome is not None:
                    values.append(cell.outcome)
                    measured_ids.append(attempt)
            if reason is not None:
                unmeasured.append({"task_id": task, "attempt": attempt, "reason": reason})
                reasons[reason] = reasons.get(reason, 0) + 1
        measured.extend(values)
        rate = Fraction(sum(values), len(values)) if values else None
        per_task[task] = {
            "planned_attempts": len(attempts),
            "present_attempt_ids": present_ids,
            "measured_attempt_ids": measured_ids,
            "measured_attempts": len(values),
            "solves": sum(values),
            "solve_rate": float(rate) if rate is not None else None,
            "solve_fraction": _fraction(rate),
        }
    planned = len(tasks) * len(attempts)
    solves = sum(measured)
    rate = Fraction(solves, len(measured)) if measured else None
    measured_tasks = [task for task in tasks if per_task[task]["measured_attempts"]]
    complete_tasks = [
        task for task in tasks if per_task[task]["measured_attempts"] == len(attempts)
    ]
    task_mean = (
        sum((Fraction(per_task[task]["solve_fraction"]) for task in measured_tasks), Fraction())
        / len(measured_tasks)
        if measured_tasks
        else None
    )
    return {
        "solve_coverage": _coverage_status(len(measured), planned),
        "task_ids": tasks,
        "attempt_ids": attempts,
        "planned_slots": planned,
        "present_rows": present_count,
        "missing_rows": len(missing),
        "error_or_null_rows": present_count - len(measured),
        "measured_attempts": len(measured),
        "missing_task_attempts": planned - len(measured),
        "measured_tasks": len(measured_tasks),
        "complete_tasks": len(complete_tasks),
        "complete_task_ids": complete_tasks,
        "unmeasured_task_ids": sorted(set(tasks) - set(measured_tasks)),
        "incomplete_task_ids": sorted(set(tasks) - set(complete_tasks)),
        "solves": solves,
        "solve_rate": float(rate) if rate is not None else None,
        "solve_fraction": _fraction(rate),
        "observed_task_mean": float(task_mean) if task_mean is not None else None,
        "observed_task_mean_fraction": _fraction(task_mean),
        "unmeasured_reasons": reasons,
        "missing_cells": missing,
        "unmeasured_cells": unmeasured,
        "per_task": per_task,
    }


def paired_task_bootstrap(
    differences: list[Fraction], replicates: int, seed: int
) -> dict[str, Any]:
    """Percentile CI by resampling n equal-weight task differences n times per draw.

    Integer-scaled rational differences avoid accumulated float rounding, including
    degenerate CIs. Percentiles use linear interpolation at (R - 1) * p (type 7).
    """
    _positive_int(replicates, "bootstrap_replicates")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    count = len(differences)
    out: dict[str, Any] = {
        "method": "paired-task percentile bootstrap",
        "resampling_unit": "task",
        "task_weighting": "equal",
        "confidence_level": 0.95,
        "percentile_method": "linear interpolation at (replicates - 1) * p (type 7)",
        "replicates": replicates,
        "seed": seed,
        "independent_units": count,
        "ci95": None,
        "ci95_fraction": None,
    }
    if not count:
        return out
    denominator = math.lcm(*(difference.denominator for difference in differences))
    weights = [int(difference * denominator) for difference in differences]
    rng = random.Random(seed)
    sums = sorted(
        sum(weights[rng.randrange(count)] for _ in range(count)) for _ in range(replicates)
    )

    def percentile(probability: Fraction) -> Fraction:
        position = (replicates - 1) * probability
        low = position.numerator // position.denominator
        high = min(low + 1, replicates - 1)
        numerator = sums[low] + (sums[high] - sums[low]) * (position - low)
        return numerator / (count * denominator)

    bounds = [percentile(Fraction(1, 40)), percentile(Fraction(39, 40))]
    out["ci95"] = [float(bound) for bound in bounds]
    out["ci95_fraction"] = [str(bound) for bound in bounds]
    return out


def _task_rate(cells: Cells, task: str, attempts: list[int], *, complete: bool) -> Fraction | None:
    values = [
        cells[(task, attempt)].outcome
        for attempt in attempts
        if (task, attempt) in cells and cells[(task, attempt)].outcome is not None
    ]
    if not values or (complete and len(values) != len(attempts)):
        return None
    return Fraction(sum(values), len(values))


def _arm_metrics(cells: Cells, tasks: list[str], attempts: list[int]) -> dict[str, Any]:
    values = [
        cells[(task, attempt)].outcome
        for task in tasks
        for attempt in attempts
        if (task, attempt) in cells and cells[(task, attempt)].outcome is not None
    ]
    rates = [_task_rate(cells, task, attempts, complete=False) for task in tasks]
    measured_rates = [rate for rate in rates if rate is not None]
    rate = Fraction(sum(values), len(values)) if values else None
    mean = sum(measured_rates, Fraction()) / len(measured_rates) if measured_rates else None
    return {
        "tasks": len(measured_rates),
        "measured_attempts": len(values),
        "solves": sum(values),
        "solve_rate": float(rate) if rate is not None else None,
        "solve_fraction": _fraction(rate),
        "task_mean": float(mean) if mean is not None else None,
        "task_mean_fraction": _fraction(mean),
    }


def paired_comparison(
    left: Cells,
    right: Cells,
    tasks: list[str],
    names: tuple[str, str],
    attempts: tuple[list[int], list[int]],
    *,
    complete: bool,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    """Compare only explicit common task rates; primary tasks require all selected IDs."""
    left_name, right_name = names
    left_ids, right_ids = attempts
    rates = {
        task: (
            _task_rate(left, task, left_ids, complete=complete),
            _task_rate(right, task, right_ids, complete=complete),
        )
        for task in tasks
    }
    common = [task for task in tasks if all(rate is not None for rate in rates[task])]
    differences = [rates[task][0] - rates[task][1] for task in common]
    wins = sum(difference > 0 for difference in differences)
    losses = sum(difference < 0 for difference in differences)
    p = V1["exact_sign_p"](wins, losses) if common else None
    if common and not wins + losses:
        p = Fraction(1)
    difference = sum(differences, Fraction()) / len(common) if common else None
    bootstrap = paired_task_bootstrap(differences, replicates, seed)
    return {
        "role": "primary" if complete else "secondary",
        "left": left_name,
        "right": right_name,
        "direction": f"{left_name} minus {right_name}",
        "status": "measured" if common else "unmeasured",
        "comparison_coverage": _coverage_status(len(common), len(tasks)),
        "requested_task_ids": tasks,
        "tasks": len(common),
        "common_task_cohort": f"{len(common)}/{len(tasks)}",
        "common_task_ids": common,
        "missing_or_unpaired_task_ids": sorted(set(tasks) - set(common)),
        "incomplete_task_ids_by_arm": {
            left_name: [task for task in tasks if rates[task][0] is None],
            right_name: [task for task in tasks if rates[task][1] is None],
        },
        "attempt_ids_by_arm": {left_name: left_ids, right_name: right_ids},
        "matched_attempt_ids": left_ids if left_ids == right_ids else None,
        "attempts_counted": {left_name: len(left_ids), right_name: len(right_ids)},
        "require_every_selected_attempt": complete,
        "arms": {
            left_name: _arm_metrics(left, common, left_ids),
            right_name: _arm_metrics(right, common, right_ids),
        },
        "task_wins": wins,
        "task_losses": losses,
        "tied_tasks": len(common) - wins - losses,
        "mean_difference": float(difference) if difference is not None else None,
        "mean_difference_fraction": _fraction(difference),
        "exact_sign_p": float(p) if p is not None else None,
        "exact_sign_fraction": _fraction(p),
        "mean_difference_ci95": bootstrap["ci95"],
        "bootstrap": bootstrap,
        "per_task": {
            task: {
                left_name: str(rates[task][0]),
                right_name: str(rates[task][1]),
                "difference_fraction": str(delta),
            }
            for task, delta in zip(common, differences, strict=True)
        },
    }


def _read_arm(
    path: Path, tasks: list[str], attempts: int, frozen: dict[str, str], pins: list[str]
) -> dict[str, Any]:
    data, source = _source(path)
    trials: dict[str, Trials] = {"headline": {}, "pin_diagnostic": {}}
    staged: dict[str, list[tuple[str, int, Cell]]] = {"headline": [], "pin_diagnostic": []}
    diagnostic_digests: dict[str, set[str]] = {}
    skipped = 0
    for line_number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = _json(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: result row must be an object")
        task = V1["norm"](V1["task_of"](row))
        role = V1["grading_role"](row, frozen, set(pins))
        name = str(row.get("name") or task)
        attempt = V1["attempt_of"](name)
        if "attempt" in row and (type(row["attempt"]) is not int or row["attempt"] != attempt):
            raise ValueError(f"{path}:{line_number}: explicit attempt conflicts with trial name")
        # A null reward is no measurement, even if a stale passed field says false.
        value = None if "reward" in row and row["reward"] is None else V1["passed"](row)
        error = row.get("error")
        budget_skip = isinstance(error, str) and (
            error == "skipped_by_budget" or error.startswith("skipped_by_budget:")
        )
        reason = (
            "skipped_by_budget" if budget_skip else "error" if error else "null_reward"
        ) if value is None else None
        trials[role].setdefault(task, []).append((name, value))
        staged[role].append((task, attempt, Cell(name, value, reason)))
        if role == "pin_diagnostic":
            skipped += budget_skip
            diagnostic_digests.setdefault(task, set()).add(
                V1["task_of"](row).rstrip("/").rsplit("/", 1)[-1]
            )
    # Validate ORIGINAL rows (including errored/skipped cells), before any view filtering
    # or conversion to dictionaries that would otherwise hide duplicate cells.
    V1["validate_cells"](trials["headline"], tasks, attempts)
    diagnostic_tasks = sorted(set(pins) & set(tasks))
    V1["validate_cells"](trials["pin_diagnostic"], diagnostic_tasks, attempts)
    headline = {(task, attempt): cell for task, attempt, cell in staged["headline"]}
    diagnostic = {(task, attempt): cell for task, attempt, cell in staged["pin_diagnostic"]}
    return {
        "source": source,
        "headline": headline,
        "diagnostic": diagnostic,
        "diagnostic_task_ids": diagnostic_tasks,
        "diagnostic_package_digests": {task: sorted(digests) for task, digests in diagnostic_digests.items()},
        "skipped_diagnostic_rows": skipped,
        "raw_rows": len(headline) + len(diagnostic),
    }


def _paired_pins(
    path: Path,
    manifest_path: Path,
    arms: dict[str, Any],
    pins: list[str],
    selected: list[int],
) -> dict[str, Any]:
    manifest_data, manifest_source = _source(manifest_path)
    graders = {
        V1["norm"](row["task_id"]): row["final_digest"].removeprefix("sha256:")[:12]
        for row in csv.DictReader(io.StringIO(manifest_data.decode("utf-8")))
    }
    if set(pins) - set(graders):
        raise ValueError("pin_manifest lacks a paired grader for one or more pin_task_ids")
    data, source = _source(path)
    sources: dict[tuple[str, str], tuple[str, int, bool | None]] = {}
    for arm, state in arms.items():
        for (task, attempt), cell in state["headline"].items():
            key = (arm, cell.name)
            if key in sources:
                raise ValueError(f"ambiguous paired pin source trial {key}")
            sources[key] = (task, attempt, cell.outcome)
    repaired: dict[str, dict[str, bool]] = {arm: {} for arm in arms}
    seen: set[tuple[str, str]] = set()
    unmeasured_rows = 0
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = _json(line)
        if not isinstance(row, dict):
            raise ValueError("paired pin verdict must be an object")
        key = (row.get("arm"), row.get("trial"))
        if key in seen or key not in sources:
            raise ValueError(f"duplicate or unmatched paired pin verdict {key}")
        seen.add(key)
        task, _, passed = sources[key]
        if task not in pins or passed is not True:
            raise ValueError(f"paired pin verdict {key} is not from a measured frozen pin pass")
        verdict = row.get("repaired_pass")
        if verdict is not None and type(verdict) is not bool:
            raise ValueError(f"paired pin verdict {key}: repaired_pass must be boolean or null")
        if verdict is None or row.get("error"):
            unmeasured_rows += 1
        else:
            repaired[key[0]][key[1]] = verdict
    summaries = {}
    for arm, state in arms.items():
        trials = [
            (cell.name, cell.outcome)
            for (task, attempt), cell in state["headline"].items()
            if task in pins and attempt in selected and cell.outcome is not None
        ]
        summary = V1["paired_pin_summary"](trials, repaired[arm], attempt_1_only=False)
        summary["attempts_counted"] = selected
        measured = summary["regrade_measured"]
        flips = summary["pin_incorrect_passes"]
        summary["pin_incorrect_pass_fraction"] = str(Fraction(flips, measured)) if measured else None
        summary["unmeasured_pin_passes"] = summary["v6_pin_passes"] - measured
        summaries[arm] = summary
    return {
        "role": "paired_pin_only_not_headline",
        "task_ids": pins,
        "selected_attempts": selected,
        "sources": {"verdicts": source, "pin_manifest": manifest_source},
        "paired_graders": {task: graders[task] for task in pins},
        "unmeasured_verdict_rows": unmeasured_rows,
        "arms": summaries,
    }


def analyze_plan(plan_path: Path) -> dict[str, Any]:
    """Validate every original authorized schedule, then construct all requested views."""
    plan_path = plan_path.resolve()
    plan_data, plan_source = _source(plan_path)
    plan = _json(plan_data)
    _fields(plan, {
        "schema", "heldout_manifest", "heldout_sha256", "package_pairs", "cohort_task_ids",
        "selected_attempts", "arms", "bootstrap_replicates", "seed", "pin_task_ids",
        "paired_pin_regrades", "pin_manifest",
    }, "plan")
    if plan.get("schema", PLAN_SCHEMA) != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA}")
    base = plan_path.parent
    heldout_path = _path(base, plan.get("heldout_manifest"), "heldout_manifest")
    heldout_data, heldout_source = _source(heldout_path)
    if "heldout_sha256" in plan and plan["heldout_sha256"] != heldout_source["sha256"]:
        raise ValueError("heldout_sha256 does not match the frozen manifest bytes")
    held = _json(heldout_data)
    if not isinstance(held, dict) or held.get("scoring_allowed") is not True:
        raise ValueError("held-out manifest does not set scoring_allowed: true")
    heldout = _task_ids(held.get("heldout"), "heldout manifest heldout")
    cohort = _task_ids(plan.get("cohort_task_ids", heldout), "cohort_task_ids")
    _subset(cohort, heldout, "cohort_task_ids")
    pins = _task_ids(plan.get("pin_task_ids", []), "pin_task_ids", empty=True)
    _subset(pins, heldout, "pin_task_ids")
    selected = plan.get("selected_attempts", [1])
    if not isinstance(selected, list) or not selected:
        raise ValueError("selected_attempts must be a nonempty array")
    for attempt in selected:
        _positive_int(attempt, "selected_attempts entry")
    if len(set(selected)) != len(selected):
        raise ValueError("selected_attempts contains repeated attempt IDs")
    selected = sorted(selected)
    replicates = _positive_int(plan.get("bootstrap_replicates", 10000), "bootstrap_replicates")
    seed = plan.get("seed", 42)
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    package_path = _path(base, plan["package_pairs"], "package_pairs") if "package_pairs" in plan else heldout_path.parent / "package-pairs.csv"
    _, package_source = _source(package_path)
    frozen = V1["v6_digests"](package_path)
    if set(heldout) - set(frozen):
        raise ValueError("package_pairs lacks frozen v6 package digests for heldout tasks")
    arm_specs = plan.get("arms")
    if not isinstance(arm_specs, dict) or len(arm_specs) < 2:
        raise ValueError("arms must explicitly name at least two arm objects")
    arms: dict[str, Any] = {}
    for name, spec in arm_specs.items():
        if not name or "_vs_" in name:
            raise ValueError("arm names must be nonempty and cannot contain _vs_")
        _fields(spec, {"results", "authorized_attempts", "authorized_task_ids", "model_metadata"}, f"arm {name}")
        attempts = _positive_int(spec.get("authorized_attempts"), f"arm {name} authorized_attempts")
        tasks = _task_ids(spec.get("authorized_task_ids", heldout), f"arm {name} authorized_task_ids")
        _subset(tasks, heldout, f"arm {name} authorized_task_ids")
        _subset(cohort, tasks, f"arm {name} cohort_task_ids")
        if max(selected) > attempts:
            raise ValueError(f"arm {name}: selected attempts exceed its authorized schedule")
        metadata = spec.get("model_metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError(f"arm {name} model_metadata must be an object")
        results_path = _path(base, spec.get("results"), f"arm {name} results")
        try:
            state = _read_arm(results_path, tasks, attempts, frozen, pins)
        except ValueError as exc:
            raise ValueError(f"arm {name}: {exc}") from exc
        state.update({
            "authorized_task_ids": tasks,
            "authorized_attempt_ids": list(range(1, attempts + 1)),
            "model_metadata": metadata,
        })
        arms[name] = state

    # Every arm's original headline AND diagnostic rows have now been validated.
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "sources": {
            "plan": plan_source,
            "heldout_manifest": heldout_source,
            "package_pairs": package_source,
            "v1_helpers": _source(V1_PATH)[1],
            "analyzer": _source(Path(__file__))[1],
        },
        "heldout_task_ids": heldout,
        "heldout_tasks": len(heldout),
        "cohort_task_ids": cohort,
        "cohort_tasks": len(cohort),
        "selected_attempts": selected,
        "arm_order": list(arms),
        "bootstrap": {
            key: value for key, value in paired_task_bootstrap([], replicates, seed).items()
            if key not in {"ci95", "ci95_fraction", "independent_units"}
        },
        "estimand_notes": {
            "primary": "Equal-weight mean paired task solve-rate difference; every selected attempt ID must be measured in both arms. Each comparison prints its own common measured task IDs.",
            "shared_table": "all_arms_common uses the intersection of complete selected-attempt tasks across ALL named arms; coverage denominator always remains the requested cohort, never the intersection.",
            "secondary": "Raw arm rates and full-schedule comparisons are secondary; observed full-schedule task means can use unequal or incomplete attempt counts. They never replace primary attempt-matched comparisons.",
            "missingness": "Missing rows, errored rows and null rewards are UNMEASURED, never failures or imputed zeros. A completely unmeasured rate, mean, p-value or CI is null.",
            "sign_test": "Exact two-sided task-level sign test, ties discarded (all measured ties: p=1). This tests direction, not mean-difference magnitude.",
            "ci95": "95% percentile paired-TASK bootstrap for the equal-weight mean difference; not IID attempt resampling. Small or degenerate cohorts can give degenerate intervals; nominal coverage is not guaranteed.",
            "pin_ci95": "Optional pin Clopper-Pearson intervals concern binomial rates only, ignore within-task correlation, are not cluster-adjusted and are not difference intervals; no guaranteed width ordering is claimed.",
        },
        "arms": {},
        "paired": {},
        "secondary": {"paired_full_schedule": {}},
    }
    for name, state in arms.items():
        raw_attempts = state["authorized_attempt_ids"]
        raw = coverage(state["headline"], state["authorized_task_ids"], raw_attempts)
        raw["role"] = "secondary_full_authorized_schedule"
        view = coverage(state["headline"], cohort, selected)
        view["role"] = "view_coverage_and_observed_subset_rate_not_pairwise_headline"
        diagnostic = coverage(state["diagnostic"], state["diagnostic_task_ids"], raw_attempts)
        diagnostic.update({
            "role": "diagnostic_only_never_headline",
            "skipped_by_budget_rows": state["skipped_diagnostic_rows"],
            "package_digests": state["diagnostic_package_digests"],
        })
        report["arms"][name] = {
            "source": state["source"],
            "model_metadata": state["model_metadata"],
            "authorized_task_ids": state["authorized_task_ids"],
            "authorized_attempt_ids": raw_attempts,
            "raw_rows_including_diagnostic": state["raw_rows"],
            "raw": raw,
            "view": view,
            "diagnostic": diagnostic,
        }
    for left, right in combinations(arms, 2):
        key = f"{left}_vs_{right}"
        report["paired"][key] = paired_comparison(
            arms[left]["headline"], arms[right]["headline"], cohort, (left, right),
            (selected, selected), complete=True, replicates=replicates, seed=seed,
        )
        full_tasks = sorted(set(arms[left]["authorized_task_ids"]) & set(arms[right]["authorized_task_ids"]))
        report["secondary"]["paired_full_schedule"][key] = paired_comparison(
            arms[left]["headline"], arms[right]["headline"], full_tasks, (left, right),
            (arms[left]["authorized_attempt_ids"], arms[right]["authorized_attempt_ids"]),
            complete=False, replicates=replicates, seed=seed,
        )
    shared = [
        task for task in cohort
        if all(_task_rate(state["headline"], task, selected, complete=True) is not None for state in arms.values())
    ]
    report["all_arms_common"] = {
        "role": "shared_selected_attempt_table",
        "comparison_coverage": _coverage_status(len(shared), len(cohort)),
        "requested_task_ids": cohort,
        "requested_tasks": len(cohort),
        "common_task_ids": shared,
        "common_tasks": len(shared),
        "common_task_cohort": f"{len(shared)}/{len(cohort)}",
        "missing_or_unpaired_task_ids": sorted(set(cohort) - set(shared)),
        "matched_attempt_ids": selected,
        "arms": {name: _arm_metrics(state["headline"], shared, selected) for name, state in arms.items()},
        "table": [
            {
                "task_id": task,
                "shared_measured": task in shared,
                "arm_solve_fractions": {
                    name: _fraction(_task_rate(state["headline"], task, selected, complete=True))
                    for name, state in arms.items()
                },
            }
            for task in cohort
        ],
    }
    if "paired_pin_regrades" in plan:
        if not pins or "pin_manifest" not in plan:
            raise ValueError("paired_pin_regrades requires pin_task_ids and pin_manifest")
        report["paired_pins"] = _paired_pins(
            _path(base, plan["paired_pin_regrades"], "paired_pin_regrades"),
            _path(base, plan["pin_manifest"], "pin_manifest"), arms, pins, selected,
        )
    elif "pin_manifest" in plan:
        raise ValueError("pin_manifest requires paired_pin_regrades")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = analyze_plan(args.plan)
        serialized = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
        # Exclusive creation protects both existing evidence and plan/input paths.
        with args.out.open("x", encoding="utf-8") as handle:
            handle.write(serialized)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
