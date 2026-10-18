"""Pure, custody-preserving reporting for the admitted frozen33 v7 regrade.

The original v6 endpoint and independently qualified v7 endpoint are separate
nullable measurements. All native sources survive in ``source_results``; only
raw-validated official cells can enter any official comparison.
"""

from __future__ import annotations

import re
import runpy
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from typing import Any

ANALYSIS_PATH = Path(__file__).resolve().parents[1] / "repaired-data-sft-v2/analyze.py"
ANALYSIS = runpy.run_path(str(ANALYSIS_PATH))
Cell = ANALYSIS["Cell"]
coverage = ANALYSIS["coverage"]
paired_comparison = ANALYSIS["paired_comparison"]
paired_task_bootstrap = ANALYSIS["paired_task_bootstrap"]

ARMS = ("base", "A", "B", "C")
PRIMARY_ARMS = ("base", "B", "C")
PAIRS = (("B", "C"), ("B", "base"), ("C", "base"))
ATTEMPTS = {"base": [1], "A": [1], "B": [1, 2], "C": [1]}
POPULATIONS = ("official", "diagnostic", "quarantined", "supplementary")
TASK_ID = re.compile(r"mimo-v2\.6-rl/format-code-task-[0-9]{6}", re.ASCII)
SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


def _uid(value: Any, label: str) -> tuple[str, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(part, str) or not part.strip() for part in value)
    ):
        raise ValueError(f"{label}: source_uid must contain native job and trial IDs")
    return tuple(value)


def _identity(row: Any, tasks: set[str], label: str) -> tuple[str, str, int]:
    if not isinstance(row, dict):
        raise ValueError(f"{label} must be an object")
    arm, task, attempt = row.get("arm"), row.get("task_id"), row.get("attempt_index")
    if not isinstance(arm, str) or arm not in ARMS:
        raise ValueError(f"{label}: unknown arm {arm!r}")
    if not isinstance(task, str) or not TASK_ID.fullmatch(task):
        raise ValueError(f"{label}: task_id must be a full canonical parent ID")
    if type(attempt) is not int or attempt < 1:
        raise ValueError(f"{label}: attempt_index must be a positive integer")
    population = row.get("population")
    if not isinstance(population, str) or population not in POPULATIONS:
        raise ValueError(f"{label}: unknown population {population!r}")
    if population == "official":
        if task not in tasks:
            raise ValueError(f"{label}: unknown frozen task {task}")
        if attempt not in ATTEMPTS[arm]:
            raise ValueError(f"{label}: unauthorized official cell {arm}/{task}:a{attempt}")
    return arm, task, attempt


def _measurement(value: Any, error: Any, null_reason: str) -> tuple[bool | None, str | None]:
    if value is not None and type(value) is not bool:
        raise ValueError("normalized outcome must be a boolean or null")
    if error is not None and not isinstance(error, str):
        raise ValueError("normalized error must be a string or null")
    if error is not None:
        return None, error
    return value, null_reason if value is None else None


def _validate(
    inventory: dict[str, Any], observations: list[dict[str, Any]], admission: dict[str, Any]
) -> tuple[list[str], dict[str, list[str]]]:
    if not isinstance(admission, dict) or admission.get("scoring_allowed") is not True:
        raise ValueError("admitted scoring_allowed must be true")
    if not isinstance(inventory, dict) or inventory.get("schema") != "heldout-v7.inventory/v1":
        raise ValueError("expected heldout-v7.inventory/v1 inventory")
    tasks = inventory.get("frozen_task_ids")
    if (
        not isinstance(tasks, list)
        or len(tasks) != 33
        or any(not isinstance(task, str) or not TASK_ID.fullmatch(task) for task in tasks)
        or len(set(tasks)) != len(tasks)
    ):
        raise ValueError("frozen_task_ids must contain all 33 unique full canonical parent IDs")
    schedule = inventory.get("authorized_attempts")
    if (
        not isinstance(schedule, dict)
        or set(schedule) != set(ARMS)
        or any(
            not isinstance(schedule[arm], list)
            or any(type(attempt) is not int for attempt in schedule[arm])
            or sorted(schedule[arm]) != ATTEMPTS[arm]
            for arm in ARMS
        )
    ):
        raise ValueError("authorized_attempts must be base:[1], A:[1], B:[1,2], C:[1]")
    if "sources" not in inventory:
        raise ValueError("inventory must retain its input sources")
    rows = inventory.get("rows")
    non_native = inventory.get("non_native_cells", [])
    if not isinstance(rows, list) or not isinstance(non_native, list):
        raise ValueError("rows and non_native_cells must be arrays")
    task_set = set(tasks)
    official_cells: set[tuple[str, str, int]] = set()
    source_uids: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        label = f"inventory row {index}"
        cell = _identity(row, task_set, label)
        uid = _uid(row.get("source_uid"), label)
        if uid in source_uids:
            raise ValueError(f"{label}: duplicate source UID {uid}")
        source_uids.add(uid)
        if row["population"] == "official":
            if cell in official_cells:
                raise ValueError(f"{label}: duplicate official cell {cell}")
            official_cells.add(cell)
        trial_dir = row.get("source_trial_dir")
        if not isinstance(trial_dir, str) or not Path(trial_dir).is_absolute():
            raise ValueError(f"{label}: source_trial_dir must be an absolute retained native path")
        if "v6_outcome" not in row or "v6_error" not in row:
            raise ValueError(f"{label}: canonical v6 outcome and error are required")
        _measurement(row["v6_outcome"], row["v6_error"], "null_original_v6_outcome")
        digest = row.get("source_digest")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError(f"{label}: source_digest must be the retained cf.source_digest")
        if "diff" not in row:
            raise ValueError(f"{label}: retained diff custody is required, including explicit null")
        diff = row["diff"]
        if diff is not None and (
            not isinstance(diff, dict)
            or not isinstance(diff.get("path"), str)
            or not Path(diff["path"]).is_absolute()
            or not isinstance(diff.get("sha256"), str)
            or not diff["sha256"].startswith("sha256:")
            or not SHA256.fullmatch(diff["sha256"][7:])
        ):
            raise ValueError(f"{label}: diff must retain its absolute path and sha256 digest")
    for index, row in enumerate(non_native):
        label = f"non_native_cells row {index}"
        cell = _identity(row, task_set, label)
        if row.get("source_uid") is not None:
            raise ValueError(f"{label}: non-native cells cannot claim a native source UID")
        if not isinstance(row.get("reason"), str) or not row["reason"]:
            raise ValueError(f"{label}: scheduled non-native cell requires its actual reason")
        if row["population"] == "official":
            if cell in official_cells:
                raise ValueError(f"{label}: duplicate official cell {cell}")
            official_cells.add(cell)
    unscheduled_raw = inventory.get("unscheduled_tasks_by_arm", {})
    if not isinstance(unscheduled_raw, dict) or set(unscheduled_raw) - set(ARMS):
        raise ValueError("unscheduled_tasks_by_arm must map known arms to frozen task IDs")
    unscheduled: dict[str, list[str]] = {}
    for arm in ARMS:
        values = unscheduled_raw.get(arm, [])
        if (
            not isinstance(values, list)
            or any(not isinstance(task, str) or task not in task_set for task in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError(f"unscheduled_tasks_by_arm.{arm}: unknown or duplicate frozen tasks")
        if any((arm, task, attempt) in official_cells for task in values for attempt in ATTEMPTS[arm]):
            raise ValueError(f"arm {arm}: unscheduled task conflicts with an official source cell")
        unscheduled[arm] = sorted(values)
    if not isinstance(observations, list):
        raise ValueError("observations must be an array")
    observed_uids: set[tuple[str, str]] = set()
    for index, observation in enumerate(observations):
        label = f"observation {index}"
        if not isinstance(observation, dict):
            raise ValueError(f"{label} must be an object")
        uid = _uid(observation.get("source_uid"), label)
        if uid in observed_uids:
            raise ValueError(f"{label}: duplicate observation UID {uid}")
        if uid not in source_uids:
            raise ValueError(f"{label}: foreign observation UID {uid}")
        observed_uids.add(uid)
        if "v7_outcome" not in observation or "v7_error" not in observation:
            raise ValueError(f"{label}: qualified v7 outcome and error are required")
        _measurement(observation["v7_outcome"], observation["v7_error"], "null_v7_outcome")
        if not isinstance(observation.get("native_evidence"), dict):
            raise ValueError(f"{label}: native_evidence custody must be retained")
        if observation.get("replayed") is not None and type(observation["replayed"]) is not bool:
            raise ValueError(f"{label}: replayed must be a boolean when known")
    return sorted(tasks), unscheduled


def _rate(values: list[bool]) -> dict[str, Any]:
    fraction = Fraction(sum(values), len(values)) if values else None
    return {
        "status": "measured" if values else "unmeasured",
        "passes": sum(values),
        "denominator": len(values),
        "rate": float(fraction) if fraction is not None else None,
        "fraction": str(fraction) if fraction is not None else None,
    }


def _dual(records: list[dict[str, Any]]) -> dict[str, Any]:
    common = [row for row in records if row["v6_outcome"] is not None and row["v7_outcome"] is not None]
    flips = [row for row in common if row["v6_outcome"] is True and row["v7_outcome"] is False]
    original_passes = sum(row["v6_outcome"] for row in common)
    fraction = Fraction(len(flips), original_passes) if original_passes else None
    return {
        "role": "secondary_same_dual_measured_native_cells",
        "status": "measured" if common else "unmeasured",
        "n": len(common),
        "item_ids": [row["item_id"] for row in common],
        "v6": _rate([row["v6_outcome"] for row in common]),
        "v7": _rate([row["v7_outcome"] for row in common]),
        "transitions": {
            f"{int(old)}->{int(new)}": sum(
                row["v6_outcome"] is old and row["v7_outcome"] is new for row in common
            )
            for old in (False, True)
            for new in (False, True)
        },
        "pass_to_fail": len(flips),
        "pass_to_fail_item_ids": [row["item_id"] for row in flips],
        "dual_measured_original_passes": original_passes,
        "pass_to_fail_fraction": str(fraction) if fraction is not None else None,
    }


def _cohort(
    name: str, tasks: list[str], requirements: dict[str, tuple[dict, list[int]]]
) -> dict[str, Any]:
    missing = {
        name: [
            task
            for task in tasks
            if any((task, attempt) not in cells or cells[(task, attempt)].outcome is None for attempt in attempts)
        ]
        for name, (cells, attempts) in requirements.items()
    }
    excluded = set().union(*(set(values) for values in missing.values()))
    common = [task for task in tasks if task not in excluded]
    return {
        "name": name,
        "requested_task_ids": tasks,
        "requested_tasks": len(tasks),
        "common_task_ids": common,
        "n": len(common),
        "common_task_cohort": f"{len(common)}/{len(tasks)}",
        "comparison_coverage": "unmeasured" if not common else "complete" if len(common) == len(tasks) else "observed_subset",
        "excluded_task_ids": [task for task in tasks if task in excluded],
        "missing_task_ids_by_measurement": missing,
        "required_attempt_ids_by_measurement": {name: attempts for name, (_, attempts) in requirements.items()},
    }


def _coverage(cells: dict, tasks: list[str], attempts: list[int], absent: dict) -> dict[str, Any]:
    result = coverage(cells, tasks, attempts)
    for row in result["missing_cells"]:
        row.update(absent.get((row["task_id"], row["attempt"]), {"reason": "missing_native_source"}))
    for row in result["unmeasured_cells"]:
        if (row["task_id"], row["attempt"]) not in cells:
            row.update(absent.get((row["task_id"], row["attempt"]), {"reason": "missing_native_source"}))
    reasons: dict[str, int] = {}
    for row in result["unmeasured_cells"]:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    result["unmeasured_reasons"] = reasons
    result["passes"] = result["solves"]
    result["denominator"] = result["measured_attempts"]
    result["role"] = "coverage_and_observed_rates_not_primary"
    return result


def _pair(
    left: dict, right: dict, tasks: list[str], names: tuple[str, str],
    attempts: tuple[list[int], list[int]], cohort_name: str, *,
    records_by_cell: dict, source_arms: tuple[str, str], role: str, replicates: int, seed: int,
) -> dict[str, Any]:
    result = paired_comparison(
        left, right, tasks, names, attempts, complete=True, replicates=replicates, seed=seed
    )
    result.update({"role": role, "cohort_name": cohort_name, "n": result["tasks"]})
    for task, values in result["per_task"].items():
        values["source_item_ids_by_arm"] = {
            name: [records_by_cell[(arm, task, attempt)]["item_id"] for attempt in ids]
            for name, arm, ids in zip(names, source_arms, attempts, strict=True)
        }
    return result


def build_report(
    inventory: dict, observations: list[dict], admission: dict, *, replicates: int = 10000,
    seed: int = 20261017,
) -> dict[str, Any]:
    """Report an already admitted campaign without dispatching or mutating files.

    The frozen33 roster is never replaced by an observed subset. The primary
    three comparisons receive exactly one Base/B/C qualified-v7 attempt1 cohort;
    four-arm, original-only, cross-version and full-B2 populations are named views.
    """
    tasks, unscheduled = _validate(inventory, observations, admission)
    bootstrap = paired_task_bootstrap([], replicates, seed)
    by_uid = {tuple(row["source_uid"]): row for row in observations}
    records: list[dict[str, Any]] = []
    for source in inventory["rows"]:
        uid = tuple(source["source_uid"])
        observation = by_uid.get(uid)
        old, old_reason = _measurement(source["v6_outcome"], source["v6_error"], "null_original_v6_outcome")
        new, new_reason = (
            _measurement(observation["v7_outcome"], observation["v7_error"], "null_v7_outcome")
            if observation is not None
            else (None, "missing_retained_diff" if source["diff"] is None else "missing_observation")
        )
        records.append({
            "item_id": f"{uid[0]}/{uid[1]}",
            **{key: deepcopy(source[key]) for key in ("source_uid", "source_trial_dir", "arm", "task_id", "attempt_index", "population")},
            "source": deepcopy(source),
            "observation": deepcopy(observation),
            "v6_outcome": old,
            "v6_error": source["v6_error"],
            "v6_reason": old_reason,
            "v7_outcome": new,
            "v7_error": observation["v7_error"] if observation is not None else None,
            "v7_reason": new_reason,
            "replayed": observation.get("replayed") if observation is not None else False,
            "native_evidence": deepcopy(observation["native_evidence"]) if observation is not None else None,
        })
    official = [row for row in records if row["population"] == "official"]
    records_by_cell = {(row["arm"], row["task_id"], row["attempt_index"]): row for row in official}
    cells = {
        version: {
            arm: {
                (row["task_id"], row["attempt_index"]): Cell(row["item_id"], row[f"{version}_outcome"], row[f"{version}_reason"])
                for row in official if row["arm"] == arm
            }
            for arm in ARMS
        }
        for version in ("v6", "v7")
    }
    non_native = inventory.get("non_native_cells", [])
    non_native_by_cell = {
        (row["arm"], row["task_id"], row["attempt_index"]): row
        for row in non_native if row["population"] == "official"
    }
    absent: dict[str, dict] = {arm: {} for arm in ARMS}
    for arm in ARMS:
        for task in tasks:
            for attempt in ATTEMPTS[arm]:
                key = (arm, task, attempt)
                if key in records_by_cell:
                    continue
                marker = non_native_by_cell.get(key)
                absent[arm][(task, attempt)] = (
                    {"reason": "not_in_historical_source_schedule", "source_status": "unscheduled"}
                    if task in unscheduled[arm]
                    else {"reason": marker["reason"], "source_status": "scheduled_without_native", "canonical_source": deepcopy(marker)}
                    if marker is not None
                    else {"reason": "missing_native_source", "source_status": "missing_native_source"}
                )
    original_cohort = _cohort("original_v6_base_B_C_attempt1", tasks, {arm: (cells["v6"][arm], [1]) for arm in PRIMARY_ARMS})
    primary_cohort = _cohort("qualified_v7_base_B_C_attempt1", tasks, {arm: (cells["v7"][arm], [1]) for arm in PRIMARY_ARMS})
    four_way = _cohort("qualified_v7_four_arm_attempt1", tasks, {arm: (cells["v7"][arm], [1]) for arm in ARMS})
    dual_cohort = _cohort("dual_v6_v7_base_B_C_attempt1", tasks, {
        f"{version}:{arm}": (cells[version][arm], [1])
        for version in ("v6", "v7") for arm in PRIMARY_ARMS
    })

    def version_view(version: str, cohort: dict, role: str) -> dict[str, Any]:
        common = cohort["common_task_ids"]
        return {
            "version": version,
            "role": role,
            "cohort": cohort,
            "attempt_ids": [1],
            "arms": {arm: _coverage(cells[version][arm], common, [1], absent[arm]) for arm in PRIMARY_ARMS},
            "paired": {
                f"{left}_vs_{right}": _pair(
                    cells[version][left], cells[version][right], common, (left, right), ([1], [1]), cohort["name"],
                    records_by_cell=records_by_cell, source_arms=(left, right), role=role, replicates=replicates, seed=seed,
                )
                for left, right in PAIRS
            },
        }

    report: dict[str, Any] = {
        "schema": "heldout-v7.report/v1",
        "scoring_allowed": True,
        "admission": deepcopy(admission),
        "inventory": deepcopy(inventory),
        "frozen_task_ids": tasks,
        "requested_tasks": len(tasks),
        "authorized_attempts": deepcopy(ATTEMPTS),
        "bootstrap": {key: value for key, value in bootstrap.items() if key not in {"ci95", "ci95_fraction", "independent_units"}},
        "estimand_notes": {
            "primary": "Qualified v7 attempt1 on one common Base/B/C task cohort for B-C, B-Base and C-Base; neither B2 nor A changes that cohort. Requested frozen33 and exclusions remain in the enclosing cohort object.",
            "versions": "Original canonical v6 and qualified new v7 are independent nullable endpoints. Old errors do not discard a valid new measurement; a new error always discards a stale new boolean.",
            "coverage": "No unscheduled source, scheduled-without-native cell, missing retained diff, missing observation, error or null is imputed as a failure. Native source UIDs are never invented for missing cells.",
            "cross_version": "Cross-version comparisons use one common cohort measured in BOTH versions for Base/B/C attempt1; per-arm dual summaries explicitly identify their own same-source cells.",
            "secondary": "Full B2 analyses require both B attempt IDs, weight tasks equally, and are secondary. Raw observed attempt rates and partial task means are coverage descriptions, not the primary estimand.",
            "sign_test": "Existing v1 exact two-sided task sign test via v2 paired_comparison; ties discarded, measured all ties p=1, empty cohorts unmeasured. The test concerns direction, not difference magnitude.",
            "ci95": "Existing seeded v2 paired-task percentile bootstrap of equal-weight task differences, not IID attempt resampling; exact fraction strings are retained. Small or degenerate cohorts do not guarantee nominal coverage.",
            "pass_labels": "Only canonical original v6 passes receive real_pass/wrong_pass/unmeasured correctness labels. Wrong pass does not establish a reward shortcut, cheating or intent. Non-official populations never enter primary denominators.",
            "custody": "source_results retains every native inventory row and joined observation. Comparison per-task item IDs resolve to those custody records; non-native markers retain canonical refs without native UIDs.",
        },
        "original_v6": version_view("v6", original_cohort, "original_v6_attempt1"),
        "primary": version_view("v7", primary_cohort, "primary"),
        "four_arm_common": {
            "version": "v7", "role": "separate_four_arm_common_attempt1", "cohort": four_way,
            "arms": {arm: _coverage(cells["v7"][arm], four_way["common_task_ids"], [1], absent[arm]) for arm in ARMS},
        },
        "cross_version_common": {
            "role": "separate_fixed_dual_version_attempt1", "cohort": dual_cohort,
            "versions": {version: version_view(version, dual_cohort, "cross_version_fixed_cohort") for version in ("v6", "v7")},
            "v7_vs_v6": {
                arm: _pair(
                    cells["v7"][arm], cells["v6"][arm], dual_cohort["common_task_ids"], (f"{arm}:v7", f"{arm}:v6"),
                    ([1], [1]), dual_cohort["name"], records_by_cell=records_by_cell, source_arms=(arm, arm),
                    role="cross_version_fixed_cohort", replicates=replicates, seed=seed,
                )
                for arm in PRIMARY_ARMS
            },
        },
        "arms": {}, "secondary": {}, "source_results": records, "per_pass_labels": [],
    }
    for row in records:
        reason = (
            "non_official_population" if row["population"] != "official"
            else "not_a_primary_arm" if row["arm"] not in PRIMARY_ARMS
            else "not_attempt1" if row["attempt_index"] != 1
            else "outside_qualified_primary_common_cohort" if row["task_id"] not in primary_cohort["common_task_ids"]
            else None
        )
        row["included_in_primary"] = reason is None
        row["excluded_from_primary"] = reason is not None
        row["primary_exclusion_reason"] = reason
        if row["v6_outcome"] is True:
            report["per_pass_labels"].append({
                **{key: deepcopy(row[key]) for key in (
                    "item_id", "source_uid", "source_trial_dir", "arm", "task_id", "attempt_index", "population",
                    "source", "observation", "native_evidence", "excluded_from_primary", "primary_exclusion_reason",
                )},
                "original_pass": True,
                "v7_outcome": row["v7_outcome"],
                "label": "real_pass" if row["v7_outcome"] is True else "wrong_pass" if row["v7_outcome"] is False else "unmeasured",
                "reason": row["v7_reason"],
            })
    for arm in ARMS:
        arm_rows = [row for row in official if row["arm"] == arm]
        observed = [row for row in arm_rows if row["observation"] is not None]
        requested_slots = len(tasks) * len(ATTEMPTS[arm])
        scheduled_missing = [row for row in non_native if row["population"] == "official" and row["arm"] == arm]
        report["arms"][arm] = {
            "requested_task_ids": tasks,
            "authorized_attempt_ids": ATTEMPTS[arm],
            "coverage": {
                "requested_slots": requested_slots,
                "source_schedule_slots": requested_slots - len(unscheduled[arm]) * len(ATTEMPTS[arm]),
                "unscheduled_slots": len(unscheduled[arm]) * len(ATTEMPTS[arm]),
                "unscheduled_task_ids": unscheduled[arm],
                "scheduled_without_native_slots": len(scheduled_missing),
                "scheduled_without_native_cells": deepcopy(scheduled_missing),
                "unclassified_missing_native_slots": sum(row["source_status"] == "missing_native_source" for row in absent[arm].values()),
                "native_sources": len(arm_rows),
                "kept_diff_sources": sum(row["source"]["diff"] is not None for row in arm_rows),
                "native_sources_without_kept_diff": sum(row["source"]["diff"] is None for row in arm_rows),
                "observation_sources": len(observed),
                "replayed_native_sources": sum(row["replayed"] is True for row in arm_rows),
                "replay_status_unknown_sources": sum(row["replayed"] is None for row in observed),
                "qualified_v7_sources": sum(row["v7_outcome"] is not None for row in arm_rows),
            },
            "v6": {
                **_coverage(cells["v6"][arm], tasks, ATTEMPTS[arm], absent[arm]),
                "role": "secondary_full_authorized_schedule",
            },
            "v7": {
                **_coverage(cells["v7"][arm], tasks, ATTEMPTS[arm], absent[arm]),
                "role": "secondary_full_authorized_schedule",
            },
            "attempt1": {version: _coverage(cells[version][arm], tasks, [1], absent[arm]) for version in ("v6", "v7")},
            "dual_measured": _dual(arm_rows),
            "populations": {
                population: {
                    "role": "official" if population == "official" else "excluded_from_primary",
                    "item_ids": [row["item_id"] for row in records if row["arm"] == arm and row["population"] == population],
                    "non_native_cells": deepcopy([row for row in non_native if row["arm"] == arm and row["population"] == population]),
                    "v6": _rate([row["v6_outcome"] for row in records if row["arm"] == arm and row["population"] == population and row["v6_outcome"] is not None]),
                    "v7": _rate([row["v7_outcome"] for row in records if row["arm"] == arm and row["population"] == population and row["v7_outcome"] is not None]),
                }
                for population in POPULATIONS
            },
        }
    for version in ("v6", "v7"):
        full_b = _cohort(f"secondary_{version}_B_complete_two_attempts", tasks, {"B": (cells[version]["B"], [1, 2])})
        complete_b = _coverage(cells[version]["B"], full_b["common_task_ids"], [1, 2], absent["B"])
        report["secondary"][version] = {
            "role": "secondary_full_B_two_attempts",
            "B_full_two_attempts": {
                "role": "secondary_equal_task_mean_complete_B2",
                "cohort": full_b,
                "requested_coverage": _coverage(cells[version]["B"], tasks, [1, 2], absent["B"]),
                "complete_cohort_coverage": complete_b,
                "task_mean": complete_b["observed_task_mean"],
                "task_mean_fraction": complete_b["observed_task_mean_fraction"],
            },
            "paired_full_schedule": {},
        }
        for right in ("C", "base", "A"):
            cohort = _cohort(f"secondary_{version}_B2_{right}1_complete", tasks, {"B": (cells[version]["B"], [1, 2]), right: (cells[version][right], [1])})
            pair = _pair(
                cells[version]["B"], cells[version][right], cohort["common_task_ids"], ("B", right), ([1, 2], [1]), cohort["name"],
                records_by_cell=records_by_cell, source_arms=("B", right), role="secondary", replicates=replicates, seed=seed,
            )
            report["secondary"][version]["paired_full_schedule"][f"B_vs_{right}"] = {"cohort": cohort, "comparison": pair}
    return report
