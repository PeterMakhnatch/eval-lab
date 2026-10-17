"""Analyze pinned M3 policies without executing models, sandboxes, or graders.

    python research/experiments/test-repair-performance-20261017/analyze.py \
        --plan /tmp/envcheck-m3-plan.json --out /tmp/envcheck-m3-analysis.json
    # Add M2 canonical replay joins and actual correction-completion joins:
    # --replay-rows /path/partition.jsonl --correction-rows /path/completions.jsonl

JSONL uses partition.py's (execution_id, repaired_digest) join contract. In particular
provenance.receipts[] retains the untouched canonical QA/native receipt row, full
source-file hash, row_sha256, valid, and problems. The analyzer checks those bytes
and observations; flattened rewards alone are insufficient. Original-failing rows
can have measured repaired rewards even though their original-pass partition is
'unmeasured'. B a2 is an independent attempt, NEVER a correction.

Correction joins must additionally bind provenance.correction (or top-level
correction): source_execution_id, protocol='reset-context-feedback', source_patch,
feedback, feedback_result, safe_feedback, task_prompt and extra_instruction file
references {path,sha256}, allowed_model_inputs=['task_prompt','source_patch',
'repaired_failure_stdout'], hidden_test_source_included=false,
reference_source_included=false and full_historical_context_included=false.
The actual native input must equal task prompt + bounded patch/feedback; its
experiment-spec.json must bind the extra instruction. Safe stdout projection and
instruction rendering reuse correction_data.py only when correction evidence exists.
No prior assistant message is synthesized and no teacher output counts as correction.

Optional --independent-rows JSONL contains execution_id, task, reward, grader_digest,
independent_of_selection=true and evidence:{path,sha256}. Different grader identity
plus the explicit independence attestation is REQUIRED; the report labels this as
separately supplied evidence, not proof that a different grader is semantically sound.
Absent independent evidence stays unmeasured. It never changes candidate selection.

Paired estimates use the SAME measured tasks for both policies. Deterministic
percentile bootstrap resamples whole paired tasks; the exact two-sided sign test
ignores ties and tests direction, not effect magnitude. Neither an observed subset
nor same-grader selection reward is a causal/independent semantic validation claim.
"""

from __future__ import annotations

import argparse
import math
import random
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

from study_plan import (
    MODEL,
    SCHEMA,
    binary_reward,
    canonical_task,
    digest,
    file_ref,
    jsonl,
    load_json,
    native_source,
    object_digest,
)

POLICIES = ("first_attempt", "best_of_2", "reset_context_feedback")


class Evidence:
    """Cache immutable byte checks, not outcomes or replay results."""

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], dict[str, Any]] = {}

    def check(self, ref: dict[str, Any]) -> dict[str, Any]:
        key = str(Path(ref["path"]).expanduser().resolve()), digest(ref["sha256"])
        if key not in self.files:
            self.files[key] = file_ref(Path(key[0]), key[1])
        return self.files[key]


def validate_plan(plan: dict[str, Any], evidence: Evidence) -> None:
    if plan.get("schema") != SCHEMA or plan.get("model") != MODEL:
        raise ValueError("not a pinned EnvCheck B policy plan")
    if plan.get("n") != 2 or plan.get("attempt_order") != [1, 2]:
        raise ValueError("this analysis requires the frozen N=2, a1 then a2 schedule")
    population = plan["population"]
    frozen = set(population["frozen_heldout_parent_ids"])
    planned = set(population["planned_task_ids"])
    if (len(frozen) != 33 or len(planned) != 26 or not planned <= frozen
            or set(population["training_exclusion_parent_ids"]) != frozen):
        raise ValueError("plan has lost common26 membership or full frozen33 exclusion lineage")
    statistics = plan["statistics"]
    if (statistics.get("confidence") != 0.95 or type(statistics.get("seed")) is not int
            or type(statistics.get("replicates")) is not int or statistics["replicates"] < 1):
        raise ValueError("plan must bind a deterministic 95% paired-task bootstrap")
    for value in plan["inputs"].values():
        for ref in value if isinstance(value, list) else [value]:
            evidence.check(ref)
    tasks = plan["tasks"]
    if len(tasks) != 26 or {task["task"] for task in tasks} != planned:
        raise ValueError("plan task rows differ from the accepted26")
    executions = set()
    for task in tasks:
        canonical_task(task["task"])
        if task.get("training_allowed") is not False or task.get("heldout") is not True:
            raise ValueError("heldout task cannot be a training correction")
        if [candidate["attempt"] for candidate in task["candidates"]] != [1, 2]:
            raise ValueError("plan candidates are not exactly a1/a2")
        for candidate in task["candidates"]:
            native = candidate["native"]
            if native:
                if native["execution_id"] in executions:
                    raise ValueError("one native execution occupies two schedule cells")
                executions.add(native["execution_id"])
                if object_digest(native["settings"]) != native["settings_digest"]:
                    raise ValueError("plan settings digest differs from settings bytes")
                for ref in native["sources"].values():
                    evidence.check(ref)


def raw_receipt_value(
    entry: dict[str, Any], joined: dict[str, Any], native: dict[str, Any], evidence: Evidence,
) -> int:
    """Read existing canonical observations, not a new replay or grader adapter."""
    evidence.check(entry)
    raw = entry["row"]
    if object_digest(raw) != digest(entry["row_sha256"]):
        raise ValueError("canonical receipt row hash differs")
    document = load_json(Path(entry["path"]))
    rows = document.get("rows") if isinstance(document, dict) else None
    if isinstance(rows, list):
        if raw not in rows:
            raise ValueError("receipt row is not present in the bound canonical source")
    elif document != raw:
        raise ValueError("embedded receipt does not equal the bound canonical source")
    if entry.get("valid") is not True or entry.get("problems"):
        raise ValueError("canonical join marked this receipt invalid")
    if raw.get("engine") == "qa-trace-counterfactual/2":
        if digest(raw["grader_digest"]) != digest(joined["repaired_digest"]):
            raise ValueError("canonical repaired package differs from the plan")
        if raw.get("task") != joined["task"]:
            raise ValueError("canonical receipt task differs")
        if (raw.get("valid_grader") is not True
                or raw.get("controls") != {"nop": 0.0, "reference": 1.0}
                or raw.get("execution_error") or raw.get("refusals") or raw.get("outcome") == "refused"):
            raise ValueError("unmeasured/invalid canonical QA replay")
        source_path = Path(raw["trial"]) / "result.json"
        source = load_json(source_path)
        if source.get("id") != native["execution_id"]:
            raise ValueError("canonical replay is of another execution")
        recorded = binary_reward(raw.get("recorded"))
        value = binary_reward(raw.get("regraded"))
    elif raw.get("schema_version") == "regrade-receipt/v1":
        ident = raw.get("source") or {}
        if digest(ident["result_digest"]) != native["sources"]["result"]["sha256"]:
            raise ValueError("native regrade source result bytes differ")
        if (raw.get("verdict") == "refused" or raw.get("refusals")
                or (raw.get("execution") or {}).get("exit_code") != 0):
            raise ValueError("native replay was refused or failed")
        variant = joined.get("variant") or {}
        if (
            not (variant.get("qualified") is True or variant.get("measurement_eligible") is True)
            or variant.get("repaired_digest") != joined["repaired_digest"]
        ):
            raise ValueError("native replay needs a measurement-admitted canonical package join")
        verifier_path = (raw.get("verifier") or {}).get("task_dir")
        if not verifier_path or Path(verifier_path).resolve() != Path(variant["package_path"]).resolve():
            raise ValueError("native replay package differs from the admitted join")
        recorded = binary_reward(((raw.get("recorded") or {}).get("rewards") or {}).get("reward"))
        value = binary_reward(((raw.get("regraded") or {}).get("rewards") or {}).get("reward"))
    else:
        raise ValueError("unknown canonical replay receipt; supply partition.py's validated join")
    if recorded != binary_reward(joined.get("original_reward")):
        raise ValueError("canonical replay's recorded reward differs from joined source")
    if value is None:
        raise ValueError("canonical replay has no measured repaired reward")
    return value


def joined_grade(
    row: dict[str, Any], native: dict[str, Any], task: dict[str, Any], evidence: Evidence,
) -> tuple[int | None, list[str]]:
    problems = []
    if row.get("execution_id") != native["execution_id"]:
        problems.append("execution_id_mismatch")
    if row.get("task") != task["task"] or row.get("model") != MODEL:
        problems.append("source_task_or_model_mismatch")
    if row.get("heldout") is not True:
        problems.append("missing_heldout_lineage")
    flags = row.get("flags") or (row.get("provenance") or {}).get("original_flags") or {}
    if flags.get("teacher") or flags.get("copied_fix") or flags.get("alias_conflicts"):
        problems.append("teacher_copied_or_conflicting_source")
    if row.get("repaired_digest") != task["repaired_digest"]:
        problems.append("repaired_digest_mismatch")
    if row.get("status") != "measured":
        problems.append(f"replay_status:{row.get('status')}")
    expected = binary_reward(row.get("repaired_reward"))
    if expected is None:
        problems.append("missing_repaired_reward")
    entries = (row.get("provenance") or {}).get("receipts") or []
    values = set()
    for entry in entries:
        if entry.get("valid") is not True:
            continue
        try:
            values.add(raw_receipt_value(entry, row, native, evidence))
        except (KeyError, ValueError, OSError) as exc:
            problems.append(f"receipt:{type(exc).__name__}:{exc}")
    if values != {expected}:
        problems.append("missing_or_conflicting_bound_observation")
    return (expected if not problems else None), problems


def candidate_grades(
    plan: dict[str, Any], supplied: list[dict[str, Any]], evidence: Evidence,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    source_map = {candidate["native"]["execution_id"]: (task, candidate)
                  for task in plan["tasks"] for candidate in task["candidates"] if candidate["native"]}
    grouped: dict[str, list[dict[str, Any]]] = {}
    excluded = []
    for row in [*plan["retained_pin_regrades"], *supplied]:
        execution = row.get("execution_id")
        pair = source_map.get(execution)
        if pair is None:
            excluded.append({"execution_id": execution, "task": row.get("task"),
                             "reason": "not a pinned B candidate execution"})
            continue
        task, candidate = pair
        if row.get("repaired_digest") != task["repaired_digest"]:
            excluded.append({"execution_id": execution, "task": row.get("task"),
                             "repaired_digest": row.get("repaired_digest"),
                             "reason": "different or unresolved planned repaired digest"})
            continue
        if binary_reward(row.get("original_reward")) != candidate["original_reward"]:
            excluded.append({"execution_id": execution, "task": row.get("task"),
                             "reason": "original reward differs from accepted schedule (do not resurrect unmeasured cells)"})
            continue
        if row.get("attempt") not in (None, candidate["attempt"]):
            raise ValueError("M2 observed attempt differs from pinned schedule")
        grouped.setdefault(execution, []).append(row)
    grades = {}
    for execution, (task, candidate) in source_map.items():
        observations = []
        problems = []
        for row in grouped.get(execution, []):
            value, issues = joined_grade(row, candidate["native"], task, evidence)
            problems.extend(issues)
            if value is not None:
                observations.append(value)
        values = set(observations)
        conflict = len(values) > 1
        grades[execution] = {
            "reward": next(iter(values)) if len(values) == 1 else None,
            "status": "conflicting_receipts" if conflict else "measured" if values else "unmeasured",
            "problems": sorted(set(problems + (["conflicting_repaired_observations"] if conflict else []))),
            "rows": grouped.get(execution, []),
        }
    return grades, excluded


def correction_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return (row.get("provenance") or {}).get("correction") or row.get("correction") or {}


def validate_correction_input(
    meta: dict[str, Any], native: dict[str, Any], base: dict[str, Any],
    baseline_grade: dict[str, Any], evidence: Evidence,
) -> None:
    # Import only for actual corrections: retained-candidate offline analysis has
    # no dependency on the training/export implementation or its runtime imports.
    from correction_data import feedback_trial, render_extra_instruction, safe_feedback

    evidence.check(file_ref(Path(__file__).with_name("correction_data.py")))
    if meta.get("full_historical_context_included") is not False:
        raise ValueError("correction must explicitly exclude full historical context")
    for field in ("source_patch", "feedback", "feedback_result", "safe_feedback", "task_prompt", "extra_instruction"):
        evidence.check(meta[field])
    if "patch" in base["sources"]:
        if digest(meta["source_patch"]["sha256"]) != base["sources"]["patch"]["sha256"]:
            raise ValueError("input patch is not the pinned source execution's actual patch")
    else:
        # Some native separate-verifier runs retained full/kept diffs and the
        # pre-hidden snapshot, not agent.diff. Require checked equivalent custody;
        # the missing original patch is never a license to skip source identity.
        from evaluation_corrections import validate_source_patch

        validate_source_patch(meta, base)
    feedback_result = Path(meta["feedback_result"]["path"]).resolve()
    feedback_path = Path(meta["feedback"]["path"]).resolve()
    if feedback_path != feedback_result.parent / "verifier/test_output.log":
        raise ValueError("feedback stdout and repaired native result have different custody")
    if baseline_grade.get("reward") != 0:
        raise ValueError("correction needs an observed repaired-failing baseline")
    allowed_feedback_trials = set()
    for row in baseline_grade["rows"]:
        for entry in (row.get("provenance") or {}).get("receipts") or []:
            if entry.get("valid") is not True:
                continue
            try:
                adapted = {**row, "provenance": {**row["provenance"], "selected_receipt": entry}}
                allowed_feedback_trials.add(feedback_trial(adapted).resolve())
            except (KeyError, ValueError, OSError):
                continue
    if feedback_result.parent not in allowed_feedback_trials:
        raise ValueError("feedback is not from the source execution's validated repaired-failure replay")
    safe, _ = safe_feedback(feedback_path.read_text())
    if safe != Path(meta["safe_feedback"]["path"]).read_text():
        raise ValueError("feedback input is not the source-free projection of observed failing stdout")
    prompt = Path(meta["task_prompt"]["path"]).read_text().strip()
    patch = Path(meta["source_patch"]["path"]).read_text()
    extra = Path(meta["extra_instruction"]["path"]).read_text()
    if extra != render_extra_instruction(patch, safe):
        raise ValueError("extra model input contains more than the bounded patch/feedback protocol")
    for source, expected in ((base, prompt), (native, None)):
        trajectory = load_json(Path(source["sources"]["native_trajectory"]["path"]))
        messages = trajectory["trajs"]["main"]["messages"]
        first = next((i for i, message in enumerate(messages) if message.get("role") == "user"), None)
        if first is None or any(message.get("role") != "system" for message in messages[:first]):
            raise ValueError("actual native initial input includes prior non-system history")
        content = messages[first].get("content")
        if not isinstance(content, str):
            raise ValueError("actual native initial input is not text")
        if expected is not None and content.strip() != expected:
            raise ValueError("task prompt differs from the original execution's actual input")
        if expected is None and (not content.strip().startswith(prompt)
                                 or content.strip()[len(prompt):].strip() != extra.strip()):
            raise ValueError("executed correction did not receive exactly task prompt + bounded extra instruction")
    config = load_json(Path(native["sources"]["result"]["path"]))["config"]
    agent = config.get("agent") or {}
    if agent.get("resume_trajectory") or agent.get("load_trajectory"):
        raise ValueError("restored native history is not reset-context correction")


def correction_grades(
    plan: dict[str, Any], rows: list[dict[str, Any]], grades: dict[str, dict[str, Any]], evidence: Evidence,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    sources = {task["candidates"][0]["native"]["execution_id"]: task
               for task in plan["tasks"] if task["candidates"][0]["native"]}
    candidate_ids = {candidate["native"]["execution_id"] for task in plan["tasks"]
                     for candidate in task["candidates"] if candidate["native"]}
    grouped: dict[str, list[dict[str, Any]]] = {}
    rejected = []
    for row in rows:
        meta = correction_metadata(row)
        source_id = meta.get("source_execution_id") or row.get("source_execution_id")
        task = sources.get(source_id)
        issues = []
        if task is None:
            rejected.append({"execution_id": row.get("execution_id"), "reason": "not a planned first-attempt correction"})
            continue
        base = task["candidates"][0]["native"]
        if (meta.get("protocol") or row.get("protocol")) != "reset-context-feedback":
            issues.append("not_reset_context_feedback")
        if row.get("execution_id") in candidate_ids:
            issues.append("independent_candidate_is_not_a_correction")
        if sorted(meta.get("allowed_model_inputs") or []) != sorted([
            "task_prompt", "source_patch", "repaired_failure_stdout",
        ]):
            issues.append("missing_bounded_model_input_contract")
        if meta.get("hidden_test_source_included") is not False or meta.get("reference_source_included") is not False:
            issues.append("missing_no_test_or_reference_source_attestation")
        for field in ("source_patch", "feedback", "extra_instruction"):
            try:
                evidence.check(meta[field])
            except (KeyError, ValueError, OSError) as exc:
                issues.append(f"{field}:{type(exc).__name__}:{exc}")
        flags = row.get("flags") or (row.get("provenance") or {}).get("original_flags") or {}
        if flags.get("teacher") or flags.get("copied_fix") or flags.get("alias_conflicts"):
            issues.append("teacher_copied_or_conflicting_source")
        try:
            actual_postgrade = row.get("schema") == "envcheck.actual-evaluation-correction/v1" or bool(
                (row.get("provenance") or {}).get("native_evaluation_postgrade")
            )
            if actual_postgrade:
                # Revalidate the real NEW native student UUID, kept-diff custody,
                # prior controls and actual repaired replay. This is not the
                # original-a1-only validator and never spoofs that source.
                from postgrade_evaluation import checked_analysis_row

                native = checked_analysis_row(row, plan, task, evidence)
            else:
                native = native_source(Path(row["source_trial"]))
            if native["execution_id"] != row.get("execution_id") or native["model"] != MODEL:
                issues.append("not_an_executed_same_model_output")
            if native["task"] != task["task"] or native["task_digest"] != task["source_task_digest"]:
                issues.append("correction_source_task_mismatch")
            if native["settings_digest"] != base["settings_digest"] and not actual_postgrade:
                issues.append("correction_runtime_settings_differ")
            if native["completion"] == "infra_or_incomplete":
                issues.append("correction_infra_or_incomplete")
            if "native_trajectory" not in native["sources"]:
                issues.append("missing_actual_native_correction_trajectory")
            if binary_reward(row.get("original_reward")) != native["native_reward"]:
                issues.append("correction_native_original_reward_mismatch")
            spec_ref = native["sources"].get("experiment_spec")
            spec = load_json(Path(spec_ref["path"])) if spec_ref else {}
            if (not spec.get("extra_instruction_sha256") or not meta.get("extra_instruction")
                    or digest(spec["extra_instruction_sha256"]) != digest(meta["extra_instruction"]["sha256"])):
                issues.append("native_request_does_not_bind_extra_instruction")
            validate_correction_input(meta, native, base, grades.get(source_id, {}), evidence)
            value, replay_issues = joined_grade(row, native, task, evidence)
            issues.extend(replay_issues)
        except (ImportError, KeyError, ValueError, OSError) as exc:
            native, value = None, None
            issues.append(f"native_correction:{type(exc).__name__}:{exc}")
        grouped.setdefault(source_id, []).append({
            "execution_id": row.get("execution_id"), "reward": value if not issues else None,
            "native": native, "problems": issues, "row": row,
        })
    corrections = {}
    for source_id, entries in grouped.items():
        unique = {entry["execution_id"] for entry in entries}
        values = {entry["reward"] for entry in entries if entry["reward"] is not None}
        if len(unique) != 1 or len(values) != 1:
            problems = [problem for entry in entries for problem in entry["problems"]]
            if len(unique) != 1:
                problems.append("one conditional correction, not best-of-multiple corrections, is required")
            if len(values) > 1:
                problems.append("conflicting repaired observations for the corrected output")
            corrections[source_id] = {"reward": None, "problems": sorted(set(problems)), "entries": entries}
        else:
            valid = next(entry for entry in entries if entry["reward"] is not None)
            corrections[source_id] = {**valid, "entries": entries}
    return corrections, rejected


def outcome(
    reward: int | None, selected: dict[str, Any] | None, involved: list[dict[str, Any]],
    reasons: list[str], *, mode: str,
) -> dict[str, Any]:
    natives = [candidate["native"] for candidate in involved if candidate.get("native")]
    return {
        "reward": reward, "status": "measured" if reward is not None else "unmeasured",
        "selected_execution_id": selected["execution_id"] if selected else None,
        "selected_attempt": selected.get("attempt") if selected else None,
        "mode": mode, "prerequisites": sorted(set(reasons)),
        "involved_execution_ids": [native["execution_id"] for native in natives],
        "source_budget_stop": any(native["completion"] == "budget_stop" for native in natives),
        "all_sources_clean": bool(natives) and len(natives) == len(involved)
                             and all(native["completion"] == "clean" for native in natives),
    }


def task_outcomes(
    task: dict[str, Any], grades: dict[str, dict[str, Any]], corrections: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    candidates = task["candidates"]
    values = []
    details = []
    for candidate in candidates:
        native = candidate["native"]
        grade = grades.get(native["execution_id"], {}) if native else {}
        issues = list(candidate["reuse_issues"])
        if task["repaired_digest"] is None:
            issues.append("missing_qualified_repaired_digest")
        if grade.get("reward") is None:
            issues.append(f"missing_repaired_regrade:a{candidate['attempt']}")
        value = grade.get("reward") if not issues else None
        values.append(value)
        details.append({"attempt": candidate["attempt"], "execution_id": native["execution_id"] if native else None,
                        "original_reward": candidate["original_reward"], "repaired_reward": value,
                        "completion": native["completion"] if native else None,
                        "reuse_issues": candidate["reuse_issues"], "prerequisites": issues,
                        "replay": grade})
    first_native = candidates[0]["native"]
    first = outcome(values[0], {**first_native, "attempt": 1} if first_native else None,
                    candidates[:1], details[0]["prerequisites"], mode="first_attempt")
    if all(value is not None for value in values):
        index = values.index(1) if 1 in values else 0
        selected = {**candidates[index]["native"], "attempt": index + 1}
        best = outcome(values[index], selected, candidates, [],
                       mode="earliest_passing_candidate" if 1 in values else "no_passing_candidate_return_a1")
    else:
        best = outcome(None, None, candidates,
                       [issue for detail in details for issue in detail["prerequisites"]], mode="incomplete_fixed_n_schedule")
    source_id = first_native["execution_id"] if first_native else None
    correction = corrections.get(source_id, {})
    if values[0] == 1:
        corrected = outcome(1, {**first_native, "attempt": 1}, candidates[:1], [], mode="baseline_pass_no_correction_needed")
    elif values[0] == 0 and correction.get("reward") is not None:
        native = correction["native"]
        corrected = outcome(correction["reward"], {**native, "attempt": 2},
                            [candidates[0], {"native": native}], [], mode="executed_same_model_reset_context_correction")
    else:
        reasons = list(details[0]["prerequisites"])
        if values[0] == 0:
            reasons.extend(correction.get("problems") or [
                "missing_genuine_same_model_reset_context_correction_execution_and_repaired_regrade",
            ])
        else:
            reasons.append("baseline_repaired_grade_required_before_feedback_branch")
        corrected = outcome(None, None, candidates[:1], reasons, mode="unmeasured_correction_branch")
    return {"task": task["task"], "heldout": True, "repaired_digest": task["repaired_digest"],
            "repair_status": (task.get("repair") or {}).get("repair_status"),
            "declared_survivors": (task.get("repair") or {}).get("declared_survivors"),
            "candidates": details, "correction_evidence": correction,
            "policies": dict(zip(POLICIES, (first, best, corrected), strict=True))}


def quantile(values: list[float], fraction: float) -> float:
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def comparison(
    tasks: list[dict[str, Any]], treatment: str, baseline: str, statistics: dict[str, Any],
    *, clean_only: bool = False, independent: bool = False,
) -> dict[str, Any]:
    pairs = []
    excluded = []
    for task in sorted(tasks, key=lambda row: row["task"]):
        policies = task["policies"]
        left, right = policies[treatment], policies[baseline]
        lvalue = left.get("independent_reward") if independent else left["reward"]
        rvalue = right.get("independent_reward") if independent else right["reward"]
        reasons = []
        if lvalue is None:
            reasons.append(f"{treatment}:unmeasured")
        if rvalue is None:
            reasons.append(f"{baseline}:unmeasured")
        if independent and left.get("independent_grader_digest") != right.get("independent_grader_digest"):
            reasons.append("independent_grader_differs_between_paired_outputs")
        if clean_only and not (left["all_sources_clean"] and right["all_sources_clean"]):
            reasons.append("not_all_policy_sources_clean")
        if reasons:
            excluded.append({"task": task["task"], "reasons": reasons})
        else:
            pairs.append((task["task"], lvalue, rvalue))
    differences = [left - right for _, left, right in pairs]
    n = len(differences)
    wins = sum(value > 0 for value in differences)
    losses = sum(value < 0 for value in differences)
    discordant = wins + losses
    # Same exact two-sided formula as repaired-data-sft-v1/analyze.py; retain fraction.
    p = (min(Fraction(1), Fraction(2 * sum(math.comb(discordant, k)
                                         for k in range(min(wins, losses) + 1)), 2**discordant))
         if discordant else Fraction(1) if n else None)
    interval = None
    if n:
        generator = random.Random(statistics["seed"])
        samples = sorted(sum(differences[generator.randrange(n)] for _ in range(n)) / n
                         for _ in range(statistics["replicates"]))
        tail = (1 - statistics["confidence"]) / 2
        interval = [quantile(samples, tail), quantile(samples, 1 - tail)]
    return {
        "treatment": treatment, "baseline": baseline,
        "endpoint": "separately_supplied_independent_evidence" if independent else "same_repaired_selection_grader",
        "selection_evaluation_dependent": not independent,
        "status": "unmeasured" if not n else "complete" if n == len(tasks) else "observed_subset",
        "planned_tasks": len(tasks), "paired_measured_tasks": n,
        "paired_task_ids": [task for task, _, _ in pairs], "excluded": excluded,
        "treatment_solves": sum(left for _, left, _ in pairs),
        "baseline_solves": sum(right for _, _, right in pairs),
        "treatment_rate": sum(left for _, left, _ in pairs) / n if n else None,
        "baseline_rate": sum(right for _, _, right in pairs) / n if n else None,
        "mean_change": sum(differences) / n if n else None,
        "mean_change_fraction": str(Fraction(sum(differences), n)) if n else None,
        "paired_task_bootstrap_ci95": interval,
        "bootstrap": {**statistics, "unit": "paired task", "method": "percentile, linear-interpolated quantiles",
                      "scope": "measured paired subset; missingness uncertainty is not covered"},
        "wins": wins, "losses": losses, "ties": n - discordant,
        "exact_sign_p": float(p) if p is not None else None,
        "exact_sign_fraction": str(p) if p is not None else None,
        "sign_test_note": "two-sided exact task-direction test, ties omitted; not a test of mean-change magnitude",
        "clean_sources_only": clean_only,
    }


def independent_evidence(
    tasks: list[dict[str, Any]], rows: list[dict[str, Any]], evidence: Evidence,
) -> list[dict[str, Any]]:
    observed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    rejected = []
    for row in rows:
        try:
            canonical_task(row["task"])
            value = binary_reward(row.get("reward"))
            evaluator = digest(row["grader_digest"])
            if value is None or row.get("independent_of_selection") is not True:
                raise ValueError("independence attestation and observed reward are required")
            evidence.check(row["evidence"])
            observed.setdefault((row["task"], row["execution_id"]), []).append({**row, "reward": value, "grader_digest": evaluator})
        except (KeyError, ValueError, OSError) as exc:
            rejected.append({"row": row, "reason": str(exc)})
    for task in tasks:
        for policy in task["policies"].values():
            entries = observed.get((task["task"], policy["selected_execution_id"]), []) if policy["reward"] is not None else []
            independent = [entry for entry in entries if entry["grader_digest"] != task["repaired_digest"]]
            values = {entry["reward"] for entry in independent}
            graders = {entry["grader_digest"] for entry in independent}
            policy["independent_reward"] = next(iter(values)) if len(values) == 1 and len(graders) == 1 else None
            policy["independent_grader_digest"] = next(iter(graders)) if len(graders) == 1 else None
            policy["independent_evidence"] = independent
    return rejected


def analyze(
    plan: dict[str, Any], replay_rows: list[dict[str, Any]], correction_rows: list[dict[str, Any]],
    evaluation_rows: list[dict[str, Any]], evidence: Evidence,
) -> dict[str, Any]:
    validate_plan(plan, evidence)
    grades, excluded_replays = candidate_grades(plan, replay_rows, evidence)
    corrections, rejected_corrections = correction_grades(plan, correction_rows, grades, evidence)
    tasks = [task_outcomes(task, grades, corrections) for task in plan["tasks"]]
    rejected_evaluations = independent_evidence(tasks, evaluation_rows, evidence)
    pairs = (("best_of_2", "first_attempt"), ("reset_context_feedback", "first_attempt"),
             ("reset_context_feedback", "best_of_2"))
    summaries = {}
    for name in POLICIES:
        measured = [task for task in tasks if task["policies"][name]["reward"] is not None]
        solves = sum(task["policies"][name]["reward"] for task in measured)
        summaries[name] = {
            "planned_tasks": 26, "measured_tasks": len(measured), "solves": solves,
            "solve_rate": solves / len(measured) if measured else None,
            "coverage": "complete" if len(measured) == 26 else "observed_subset" if measured else "unmeasured",
            "measured_task_ids": [task["task"] for task in measured],
            "unmeasured_task_ids": [task["task"] for task in tasks if task not in measured],
            "budget_stop_tasks": [task["task"] for task in measured if task["policies"][name]["source_budget_stop"]],
            "planned_population_solve_rate_bounds": [solves / 26, (solves + 26 - len(measured)) / 26],
            "bound_note": "missing outcomes set to 0/1 only for bounds, never imputed into measured rates",
        }
    candidates = [candidate for task in plan["tasks"] for candidate in task["candidates"]]
    native_candidates = [candidate for candidate in candidates if candidate["native"]]
    budget_stops = [{"task": candidate["task"], "attempt": candidate["attempt"],
                     "execution_id": candidate["native"]["execution_id"],
                     "batch_error": candidate["batch_error"], "original_reward": candidate["original_reward"],
                     "exception_info": candidate["native"]["exception_info"],
                     "native_exit_status": candidate["native"]["native_exit_status"],
                     "stop_reason": candidate["native"]["stop_reason"],
                     "infra_error": candidate["native"]["infra_error"]}
                    for candidate in native_candidates if candidate["native"]["completion"] == "budget_stop"]
    executed_branches = [task for task in tasks if task["policies"]["reset_context_feedback"]["mode"]
                         == "executed_same_model_reset_context_correction"]
    return {
        "schema": "envcheck.m3-analysis/v1", "model": MODEL, "n": 2,
        "population": plan["population"], "interpretation": plan["interpretation"],
        "policy_definitions": plan["policies"], "policies": summaries,
        "paired": {f"{left}_minus_{right}": comparison(tasks, left, right, plan["statistics"])
                   for left, right in pairs},
        "clean_completion_sensitivity": {
            f"{left}_minus_{right}": comparison(tasks, left, right, plan["statistics"], clean_only=True)
            for left, right in pairs
        },
        "independent_evaluation": {
            "status": "separately_supplied_attestations" if evaluation_rows else "unmeasured",
            "caveat": "a different digest and explicit input attestation are not an audit of semantic grader quality",
            "paired": {f"{left}_minus_{right}": comparison(tasks, left, right, plan["statistics"], independent=True)
                       for left, right in pairs},
            "rejected_rows": rejected_evaluations,
        },
        "source_schedule": {
            "planned_tasks": 26, "planned_attempts": 52,
            "batch_measured_attempts": sum(candidate["original_reward"] is not None for candidate in candidates),
            "two_batch_scored_attempt_tasks": sum(all(candidate["original_reward"] is not None for candidate in task["candidates"])
                                                 for task in plan["tasks"]),
            "reusable_attempts": sum(candidate["reusable"] for candidate in candidates),
            "completion_counts": dict(Counter(candidate["native"]["completion"] if candidate["native"] else "missing_native"
                                              for candidate in candidates)),
            "budget_stops": budget_stops,
            "missing_or_unusable": [{"task": candidate["task"], "attempt": candidate["attempt"],
                                     "original_reward": candidate["original_reward"], "batch_error": candidate["batch_error"],
                                     "reasons": candidate["reuse_issues"]}
                                    for candidate in candidates if not candidate["reusable"]],
            "note": "65/66 batch-scored frozen33 attempts are not 65 clean native completions; no budget stop is silently cleaned",
        },
        "correction_execution": {
            "genuine_executed_branches_measured": len(executed_branches),
            "task_ids": [task["task"] for task in executed_branches],
            "actual_native_student_execution_ids": sorted({
                task["correction_evidence"]["execution_id"] for task in executed_branches
                if task["correction_evidence"]["row"].get("schema") == "envcheck.actual-evaluation-correction/v1"
            }),
            "observed_native_model_requests": {
                task["correction_evidence"]["execution_id"]:
                    task["correction_evidence"]["row"]["flags"].get("agent_metadata", {}).get("model_requests")
                for task in executed_branches
                if task["correction_evidence"]["row"].get("schema") == "envcheck.actual-evaluation-correction/v1"
            },
            "new_model_executions_by_analyzer": 0,
            "new_model_executions_by_postgrade": 0,
            "runtime_protocol_confounds": [
                task["correction_evidence"]["row"]["provenance"]["native_evaluation_postgrade"]["runtime_confound"]
                for task in executed_branches
                if task["correction_evidence"]["row"].get("schema") == "envcheck.actual-evaluation-correction/v1"
            ],
            "patch_replays_count_as_model_executions": False,
            "same_grader_endpoint_reuse_is_independent_evaluation": False,
            "pass_through_baseline_pass_tasks": [task["task"] for task in tasks if task["policies"]["reset_context_feedback"]["mode"]
                                                 == "baseline_pass_no_correction_needed"],
            "note": "baseline passes require no correction; they are not evidence of executed correction lift; independent B a2/Codex teachers never count",
            "rejected_rows": rejected_corrections,
        },
        "tasks": tasks, "excluded_replay_rows": excluded_replays,
        "remaining_prerequisites": [{"task": task["task"], "policy": name, "requires": policy["prerequisites"]}
                                    for task in tasks for name, policy in task["policies"].items()
                                    if policy["reward"] is None],
        "runtime_prerequisites": plan["runtime_prerequisites"],
        "execution": {"mode": "offline_read_only", "model_calls": 0, "sandbox_calls": 0, "regrades": 0},
    }


def main() -> int:
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--replay-rows", type=Path, action="append", default=[])
    parser.add_argument("--correction-rows", type=Path, action="append", default=[])
    parser.add_argument("--independent-rows", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        evidence = Evidence()
        inputs = {"plan": file_ref(args.plan), "replay_rows": [file_ref(path) for path in args.replay_rows],
                  "correction_rows": [file_ref(path) for path in args.correction_rows],
                  "independent_rows": [file_ref(path) for path in args.independent_rows]}
        result = analyze(load_json(args.plan),
                         [row for path in args.replay_rows for row in jsonl(path)],
                         [row for path in args.correction_rows for row in jsonl(path)],
                         [row for path in args.independent_rows for row in jsonl(path)], evidence)
        result["inputs"] = inputs
        result["provenance"] = {"bound_evidence_files": list(evidence.files.values()),
                                "analyzer_source": file_ref(Path(__file__)),
                                "plan_builder_source": file_ref(Path(__file__).with_name("study_plan.py"))}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x") as stream:
            stream.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (KeyError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"analysis": file_ref(args.out), "policies": result["policies"],
                      "genuine_correction_branches": result["correction_execution"]["genuine_executed_branches_measured"],
                      "model_calls": 0, "sandbox_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
