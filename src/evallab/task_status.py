"""Build label-blind, DEV-only task status from retained task-side receipts.

Admission records and Gold certification receipts are authority; precomputed
status/label fields never are. Legacy Gold experiment summaries are read in
place, without converting their observations into stronger claims.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.hack_drop import _check_row_dev, _get_corpus_registry, check_dev_path_allowed
from evallab.task_admission import AdmissionRecord, default_steps
from evallab.trace_anatomy import RefusedRun

SCHEMA = "evallab.task_status/v1"
_FORBIDDEN_KEYS = frozenset(
    {
        "run_id",
        "item_id",
        "rollout",
        "rollouts",
        "rollout_path",
        "trajectory",
        "trajectory_path",
        "steps_taken",
        "outcome",
        "outcomes",
        "run_outcomes",
        "raw_reward",
        "reward",
        "is_cheat",
        "cheated",
        "cheating",
        "decision",
        "decisions",
        "real_submissions",
        "labels",
        "label",
        "ground_truth",
        "run",
        "runs",
        "trial",
        "trials",
        "trial_id",
        "score",
        "scores",
        "success",
        "correct",
        "human_verdict",
        "real_n",
        "real_flipped",
        "real_flip_rate",
    }
)


@dataclass(frozen=True)
class _Assessment:
    task_id: str
    digest: str | None
    package: str | None
    evidence: dict[str, str]
    gates_pass: bool = False
    weakness: str | None = None
    incomplete: str = (
        "INCOMPLETE_GATES: Task-side controls or mutation gates are missing or failed."
    )
    alternatives: tuple[int, int | None] | None = None
    reference_failed: bool = False
    alternative_rejections: int = 0


def _guard_paths(value: Any) -> None:
    if isinstance(value, str):
        check_dev_path_allowed(value)
    elif isinstance(value, list):
        for child in value:
            _guard_paths(child)


def _guard(value: Any, *, root: bool = True) -> None:
    """Inspect the whole document, including metadata ignored by parsers."""
    if isinstance(value, dict):
        _check_row_dev(value)
        for key, child in value.items():
            name = str(key).casefold().replace("-", "_")
            admission_verdict = (
                root and name == "verdict" and value.get("schema") == "evallab.task_admission/v1"
            )
            if (
                name in _FORBIDDEN_KEYS
                or any(
                    token in name
                    for token in (
                        "label",
                        "adjudicat",
                        "submission",
                        "exploit",
                        "rollout",
                        "trajector",
                        "decision",
                    )
                )
                or "verdict" in name
                and not admission_verdict
                or name.endswith("run_id")
                or name.startswith("run_")
                or name.endswith(("outcome", "outcomes"))
            ):
                raise RefusedRun(
                    f"circular task-status evidence carries run outcomes or labels: {key}"
                )
            if name in {"path", "task", "upgraded_package"} or name.endswith(
                (
                    "_path",
                    "_paths",
                    "_dir",
                    "_dirs",
                    "_file",
                    "_files",
                    "_record",
                    "_records",
                    "_patch",
                    "_patches",
                )
            ):
                _guard_paths(child)
            _guard(child, root=False)
    elif isinstance(value, list):
        for child in value:
            _guard(child, root=False)


def _digest(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:sha256:)?[a-fA-F0-9]{64}", value):
        raise ValueError("package_digest must be a sha256 digest or null")
    return "sha256:" + value.removeprefix("sha256:").lower()


def _store_task_id(value: str) -> str | None:
    """Recognize task-store layouts, never dataset namespaces or version dirs."""
    parts = Path(value).parts
    candidates: set[str] = set()
    for index, part in enumerate(parts):
        tail = parts[index + 1 :]
        if part == "variants":
            if len(tail) != 2 or not re.fullmatch(r"[0-9a-f]{12}(?:[0-9a-f]{52})?", tail[1], re.I):
                raise RefusedRun(f"ambiguous task identity in variant path: {value}")
            candidates.add(tail[0].rsplit("__", 1)[-1])
        elif part == "tasks":
            if (
                not tail
                or len(tail) > 2
                or (
                    len(tail) == 2
                    and not re.fullmatch(r"r\d+|[0-9a-f]{12}|[0-9a-f]{64}", tail[1], re.I)
                )
            ):
                raise RefusedRun(f"ambiguous task identity in task path: {value}")
            candidates.add(tail[0])
    if len(candidates) > 1 or "" in candidates:
        raise RefusedRun(f"ambiguous task identity in package path: {value}")
    return next(iter(candidates), None)


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("task-side evidence needs a nonempty task identifier")
    value = value.strip()
    if re.fullmatch(r"\d{6}", value):
        return f"format-code-task-{value}"
    if "/" in value:
        stored = _store_task_id(value)
        if stored is not None:
            return stored
        if value.startswith(("/", ".", "~")):
            name = Path(value).name
            if not name or re.fullmatch(r"r\d+|[0-9a-f]{12}|[0-9a-f]{64}", name, re.I):
                raise RefusedRun(f"ambiguous task identity in package path: {value}")
            return name
    return value


def _identity_candidates(assessment: _Assessment, row: Mapping[str, Any]) -> set[str]:
    candidates = {assessment.task_id}
    canonical = assessment.task_id.rsplit("/", 1)[-1]
    values: set[str] = set()
    for value in (assessment.package, row.get("task_id"), row.get("task")):
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("task/package identity must be a string")
        values.add(value)
        if "/" in value or value.startswith("~"):
            values.add(str(Path(value).expanduser().resolve()))
    for value in sorted(values):
        candidates.add(value)
        candidates.add(Path(value).name)
        stored = _store_task_id(value) if "/" in value else None
        if stored is not None:
            candidates.add(stored)
            if stored != canonical:
                raise RefusedRun(f"ambiguous task identity: {assessment.task_id} versus {stored}")
        if re.fullmatch(r"r\d+|[0-9a-f]{12}|[0-9a-f]{64}", Path(value).name, re.I):
            candidates.add(Path(value).parent.name)
    return (
        candidates
        | {candidate.split("__", 1)[0] for candidate in candidates}
        | {candidate.rsplit("__", 1)[-1] for candidate in candidates}
    )


def _alternatives(row: Mapping[str, Any]) -> tuple[int, int | None] | None:
    total = row.get("alternative_solutions_total")
    accepted = row.get("alternative_solutions_accepted")
    if total is None and accepted is None:
        return None
    if (
        type(total) is not int
        or total < 0
        or (accepted is not None and (type(accepted) is not int or not 0 <= accepted <= total))
    ):
        raise ValueError(
            "alternative solution counts need a nonnegative total and accepted <= total (null means unmeasured)"
        )
    return total, accepted


def _validate_reward(value: Any) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
    ):
        raise ValueError("reward must be a finite number in [0, 1], or null")


def _gate7(row: Mapping[str, Any]) -> tuple[tuple[int, int | None] | None, int, bool]:
    alternatives = _alternatives(row)
    rewards = row.get("alternative_solution_rewards")
    rejected = 0
    if rewards is not None:
        if not isinstance(rewards, dict):
            raise ValueError("alternative_solution_rewards must be an object")
        for reward in rewards.values():
            _validate_reward(reward)
        if rewards or alternatives is not None:
            if alternatives is None or len(rewards) != alternatives[0]:
                raise ValueError("alternative reward count disagrees with Gate 7 total")
            rejected = sum(reward is not None and reward != 1 for reward in rewards.values())
            if alternatives[1] is not None and (
                any(reward is None for reward in rewards.values())
                or sum(reward == 1 for reward in rewards.values()) != alternatives[1]
            ):
                raise ValueError("alternative rewards disagree with Gate 7 accepted count")
    consistent = True
    if alternatives is not None and alternatives[0] >= 2 and alternatives[0] == alternatives[1]:
        consistent = row.get("gate7_alternatives_passed", True) is True
    return alternatives, rejected, consistent


def _count(row: Mapping[str, Any], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{key} must be a nonnegative integer")
    return value


def _mutation_pair(row: Mapping[str, Any], total: str, killed: str) -> tuple[bool, bool]:
    n, k = _count(row, total), _count(row, killed)
    if n is None or k is None:
        return False, False
    if k > n:
        raise ValueError(f"{killed} exceeds {total}")
    return n > 0 and k == n, k < n


def _filtered_gold(row: dict[str, Any], evidence: dict[str, str]) -> _Assessment:
    """Read the A4B shipped-round task receipt, never its status assertion.

    A4B's misleading upgraded_package_sha256 hashes the test file only. It is
    not used as a package digest. Its eval pairs cover baseline-accepted
    mutants only, so incomplete coverage cannot prove the full mutation gate.
    """
    task = _task_id(row["task_id"])
    package = row.get("package_dir")
    digest = _digest(row.get("package_digest"))
    alternatives, alternative_rejections, gate7_consistent = _gate7(row)
    gates, counts = row.get("gates"), row.get("counts")
    if gates is None or counts is None:
        return _Assessment(
            task,
            digest,
            package,
            evidence,
            alternatives=alternatives,
            alternative_rejections=alternative_rejections,
        )
    if not isinstance(gates, dict) or not isinstance(counts, dict):
        raise ValueError("filtered Gold receipt requires gates and counts objects")
    pairs = row.get("paired_eval_base_accepted", [])
    if not isinstance(pairs, list) or not all(isinstance(pair, dict) for pair in pairs):
        raise ValueError("filtered Gold eval pairs must be objects")
    train_pass, _ = _mutation_pair(counts, "train_total", "train_killed")
    probe_pass, _ = _mutation_pair(counts, "probe_total", "probe_killed")
    eval_total = _count(counts, "eval_total")
    paired_pass = eval_total is not None and eval_total > 0 and len(pairs) == eval_total
    paired_pass &= all(pair.get("arm2_killed") is True for pair in pairs)
    survived = any(pair.get("arm2_killed") is False for pair in pairs)
    # A false negative-control gate may mean infrastructure failure rather
    # than a reward of 1; only explicitly observed survivors establish weak.
    return _Assessment(
        task,
        digest,
        package,
        evidence,
        gates_pass=row.get("accepted_test") is True
        and gate7_consistent
        and all(
            gates.get(key) is True
            for key in (
                "reference_passed",
                "nop_passed",
                "witness_passed",
                "exit0_passed",
            )
        )
        and train_pass
        and probe_pass
        and paired_pass,
        weakness="MUTANT_SURVIVED: A held-out mutant survived on this grader."
        if survived
        else None,
        reference_failed=gates.get("reference_passed") is False,
        alternatives=alternatives,
        alternative_rejections=alternative_rejections,
    )


def _gold(
    row: dict[str, Any], evidence: dict[str, str], *, legacy: bool = False, batch: bool = False
) -> _Assessment:
    task = _task_id(row.get("task_id", row.get("task")))
    digest = _digest(row.get("package_digest"))
    package = row.get("upgraded_package")
    if package is not None and not isinstance(package, str):
        raise ValueError("upgraded_package must be a string")
    if not legacy and not batch and "env" in row and "accepted_test" in row:
        return _filtered_gold(row, evidence)
    alt, alternative_rejections, gate7_consistent = _gate7(row)
    if batch:
        # The retained A4 batch stopped at reference validation. Aggregate
        # status/is_weak assertions cannot establish a successful attack.
        gates = row.get("gates")
        counts = row.get("counts")
        if not isinstance(gates, dict) or not isinstance(counts, dict):
            raise ValueError("Gold batch rows require gates and counts")
        if gates.get("reference_passed") is not False:
            raise ValueError(
                "legacy Gold batch needs raw control rewards before it can prove passing gates"
            )
        return _Assessment(task, digest, package, evidence, reference_failed=True)
    if legacy:
        required = {
            "reference_reward",
            "nop_reward",
            "witness_rewards",
            "exit0_pth_reward",
            "train_total",
            "train_killed",
            "eval_total",
            "eval_killed",
        }
        train_total, train_killed = "train_total", "train_killed"
        eval_total, eval_killed = "eval_total", "eval_killed"
        flags = ("gate1_ref", "gate2_nop", "gate3_wit", "gate4_exit0")
    else:
        required = {
            "task_id",
            "kind",
            "upgraded_package",
            "train_mutants_total",
            "train_mutants_killed",
            "eval_mutants_total",
            "eval_mutants_killed",
            "gate1_reference_passed",
            "gate2_nop_passed",
            "gate3_witness_passed",
            "gate4_exit0_pth_passed",
        }
        train_total, train_killed = "train_mutants_total", "train_mutants_killed"
        eval_total, eval_killed = "eval_mutants_total", "eval_mutants_killed"
        flags = (
            "gate1_reference_passed",
            "gate2_nop_passed",
            "gate3_witness_passed",
            "gate4_exit0_pth_passed",
        )
    if not required <= row.keys():
        raise ValueError("not a TaskCertificationResult receipt or supported legacy Gold summary")
    raw_witnesses = row.get("witness_rewards")
    if isinstance(raw_witnesses, dict):
        witnesses = list(raw_witnesses.values()) or [row.get("witness_reward")]
    elif raw_witnesses is None:
        witnesses = [row.get("witness_reward")]
    elif isinstance(raw_witnesses, list):
        witnesses = raw_witnesses
    else:
        raise ValueError("witness_rewards must be an object or a legacy list")
    probes = [row.get("exit0_pth_reward"), *witnesses]
    extra_attacks = row.get("attack_rewards", {})
    if not isinstance(extra_attacks, dict):
        raise ValueError("attack_rewards must be an object")
    probes.extend(extra_attacks.values())
    for reward in [row.get("reference_reward"), row.get("nop_reward"), *probes]:
        _validate_reward(reward)
    train_pass, _ = _mutation_pair(row, train_total, train_killed)
    eval_pass, eval_survived = _mutation_pair(row, eval_total, eval_killed)
    eval_rewards = row.get("eval_rewards")
    if isinstance(eval_rewards, dict):
        eval_survived = any(
            value == 1 and not isinstance(value, bool) for value in eval_rewards.values()
        )
    elif not legacy:
        # Counts alone cannot distinguish survival from an unexecuted trial.
        eval_survived = False
    weakness = None
    if row.get("nop_reward") == 1:
        weakness = "NOP_ACCEPTED: The no-op control scored 1 on this grader."
    elif any(value == 1 for value in probes):
        weakness = "ATTACK_ACCEPTED: A task-side attack, tamper witness, or startup probe scored 1."
    elif eval_survived:
        weakness = "MUTANT_SURVIVED: A held-out mutant survived on this grader."
    # Missing, fractional, or infrastructure results cannot count as kills.
    rewards_complete = all(
        value in (0, 1, None) and not isinstance(value, bool)
        for value in [row.get("reference_reward"), row.get("nop_reward"), *probes]
    )
    for key, total_key in (("train_rewards", train_total), ("eval_rewards", eval_total)):
        rewards = row.get(key)
        if rewards is not None:
            if not isinstance(rewards, dict):
                raise ValueError(f"{key} must be an object")
            for reward in rewards.values():
                _validate_reward(reward)
            rewards_complete &= len(rewards) == row[total_key] and all(
                value == 0 and not isinstance(value, bool) for value in rewards.values()
            )
    rewards_complete &= gate7_consistent
    passed = (
        rewards_complete
        and row.get("reference_reward") == 1
        and row.get("nop_reward") == 0
        and bool(witnesses)
        and all(value == 0 for value in probes)
        and all(row.get(flag) is True for flag in flags)
        and train_pass
        and eval_pass
    )
    reference = row.get("reference_reward")
    return _Assessment(
        task,
        digest,
        package,
        evidence,
        passed,
        weakness,
        alternatives=alt,
        reference_failed=reference is not None and reference != 1,
        alternative_rejections=alternative_rejections,
    )


def _admission(row: dict[str, Any], evidence: dict[str, str]) -> _Assessment:
    record = AdmissionRecord.model_validate(row)
    for raw_step in row.get("steps", []):
        for reward in raw_step.get("rewards", []):
            _validate_reward(reward)
    task = _task_id(record.task)
    digest = _digest(record.package_digest)
    steps = {step.name: step for step in record.steps}
    if len(steps) != len(record.steps):
        raise ValueError("duplicate admission steps")
    weakness = None
    if "nop" in steps and 1 in steps["nop"].rewards:
        weakness = "NOP_ACCEPTED: The no-op control scored 1 on this grader."
    elif any(step.kind == "cheat" and 1 in step.rewards for step in record.steps):
        weakness = "ATTACK_ACCEPTED: A task-side cheat attack scored 1 on this grader."
    elif record.survivor_patches or any(step.survivor_patches for step in record.steps):
        weakness = "MUTANT_SURVIVED: An admission mutant survived on this grader."
    required = default_steps()
    passed = (
        record.verdict == "admitted" and not record.skipped_steps and not record.mutation_skipped
    )
    for definition in required:
        step = steps.get(definition.name)
        if step is None or step.status != "pass" or step.kind != definition.kind:
            passed = False
            continue
        if definition.kind in {"control", "cheat"}:
            expected = definition.expect if definition.kind == "control" else 0.0
            passed &= bool(step.rewards) and all(reward == expected for reward in step.rewards)
    reference = steps.get("oracle")
    return _Assessment(
        task,
        digest,
        record.task,
        evidence,
        passed,
        weakness,
        reference_failed=reference is not None
        and any(reward is not None and reward != 1 for reward in reference.rewards),
    )


def _rows(data: Any, kind: str) -> tuple[list[dict[str, Any]], bool, bool]:
    legacy = batch = False
    if kind == "admission":
        if not isinstance(data, dict) or data.get("schema") != "evallab.task_admission/v1":
            raise ValueError("admission input must be an evallab.task_admission/v1 record")
        return [data], False, False
    if isinstance(data, dict) and "tasks" in data:
        batch = data.get("schema_version") == "gold-v1/manifest/v1"
        legacy = "n_tasks" in data and "gate1_reference" in data
        data = data["tasks"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data or not all(isinstance(row, dict) for row in data):
        raise ValueError("Gold/replay input must contain task-side receipt objects")
    return data, legacy, batch


def _code_revision(root: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def build_task_status(
    *,
    admission: Sequence[Path | str] = (),
    gold_results: Sequence[Path | str] = (),
    alt_replays: Sequence[Path | str] = (),
    repo_root: Path | None = None,
    built_at: str | None = None,
    code_rev: str | None = None,
) -> dict[str, Any]:
    """Build interface A without reading rollout labels or executing any trials.

    Alternative replay inputs must themselves be complete TaskCertificationResult
    receipts with Gate 7 counts, not real-submission outcomes or adjudications.
    Counts are never summed across receipts (which might reuse solutions).
    """
    root = repo_root or Path(__file__).resolve().parents[2]
    registry = _get_corpus_registry(root)
    assessments: dict[str, list[_Assessment]] = {}
    inputs: list[dict[str, str]] = []
    seen: dict[Path, str] = {}
    for kind, paths in (
        ("admission", admission),
        ("gold_certification", gold_results),
        ("alt_solution_replay", alt_replays),
    ):
        for raw in paths:
            expanded = Path(raw).expanduser()
            path = check_dev_path_allowed(expanded if expanded.is_absolute() else root / expanded)
            files = (
                sorted(path.rglob("admission-*.json"))
                if path.is_dir() and kind == "admission"
                else [path]
            )
            if not files:
                raise ValueError(f"no admission records found: {path}")
            for file in files:
                file = check_dev_path_allowed(file)
                if file in seen:
                    if seen[file] != kind:
                        raise ValueError(
                            f"same evidence file declared under multiple input kinds: {file}"
                        )
                    continue
                seen[file] = kind
                if re.search(
                    r"(?:label|rollout|trajector|submission|adjudicat)", file.as_posix(), re.I
                ):
                    raise RefusedRun(f"circular task-status evidence filename/path: {file}")
                # Hash exactly the bytes parsed, once, to avoid a provenance/read race.
                raw_bytes = file.read_bytes()
                evidence = {
                    "path": str(file),
                    "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    "kind": kind,
                }
                try:
                    data = json.loads(raw_bytes)
                except json.JSONDecodeError:
                    data = [json.loads(line) for line in raw_bytes.splitlines() if line.strip()]
                _guard(data)
                rows, legacy, batch = _rows(data, "admission" if kind == "admission" else kind)
                inputs.append(evidence)
                for row in rows:
                    if kind == "admission":
                        assessment = _admission(row, evidence)
                    else:
                        if kind == "alt_solution_replay" and (
                            legacy
                            or batch
                            or "env" in row
                            and "accepted_test" in row
                            or "alternative_solutions_total" not in row
                            or "alternative_solutions_accepted" not in row
                        ):
                            raise ValueError(
                                "alternative replay needs a TaskCertificationResult with Gate 7 counts"
                            )
                        assessment = _gold(row, evidence, legacy=legacy, batch=batch)
                    identifiers = _identity_candidates(assessment, row)
                    if any(
                        registry.assigned_test_anywhere(task_id=identifier)
                        or registry.assigned_test_anywhere(item_id=identifier)
                        for identifier in identifiers
                    ):
                        raise RefusedRun(
                            f"task-status task is assigned TEST in corpus registry: {assessment.task_id}"
                        )
                    assessments.setdefault(assessment.task_id, []).append(assessment)
    tasks = []
    for task, items in sorted(assessments.items()):
        digests = {item.digest for item in items if item.digest is not None}
        if len(digests) > 1:
            raise ValueError(
                f"conflicting package digests for {task}; build separate manifests per grader"
            )
        digest = next(iter(digests), None)
        controls = sorted(items, key=lambda item: (item.evidence["kind"], item.evidence["path"]))
        if len(controls) > 1 and digests and any(item.digest is None for item in controls):
            raise ValueError(
                f"mixed bound and unbound receipts for {task}; build separate manifests per grader"
            )
        if len(controls) > 1 and any(item.digest is None for item in controls):
            raise ValueError(f"unbound package versions for {task}; select one receipt per grader")
        status, reason = (
            "uncertified",
            "NO_EVIDENCE: No qualifying task-side control evidence is available.",
        )
        weakness = next((item.weakness for item in controls if item.weakness), None)
        alternatives = [item.alternatives for item in controls if item.alternatives is not None]
        rejected = next(
            (
                (item.alternatives[0] - item.alternatives[1], item.alternatives[0])
                if item.alternatives[1] is not None
                else (item.alternative_rejections, item.alternatives[0])
                for item in controls
                if item.alternatives is not None
                and (
                    item.alternative_rejections > 0
                    or item.alternatives[1] is not None
                    and item.alternatives[1] < item.alternatives[0]
                )
            ),
            None,
        )
        if any(item.reference_failed for item in controls):
            reason = "REFERENCE_FAILED: The reference fix did not score 1 on this grader."
        elif weakness:
            status, reason = "weak", weakness
        elif rejected is not None:
            k, n = rejected
            reason = f"WRONG_REJECTION_{k}_OF_{n}: This grader rejected {k} of {n} independently correct alternative solutions."
        elif controls and all(item.gates_pass for item in controls):
            measured_totals = [
                total
                for total, accepted in alternatives
                if accepted is not None and accepted == total
            ]
            total = max(measured_totals, default=0)
            if total >= 2:
                status, reason = (
                    "certified",
                    "ALL_GATES_PASSED: Controls, attacks, mutation gates, and at least two correct alternatives passed.",
                )
            elif not measured_totals:
                status, reason = (
                    "provisional",
                    "ALT_UNMEASURED: Task-side gates passed, but alternative correct solutions were not measured.",
                )
            else:
                status, reason = (
                    "provisional",
                    f"ALT_TOO_FEW_{total}: Task-side gates passed, but only {total} correct alternatives were measured.",
                )
        elif controls:
            reason = next(item.incomplete for item in controls if not item.gates_pass)
        tasks.append(
            {
                "task_id": task,
                "status": status,
                "reason": reason,
                "package_digest": digest,
                "evidence": sorted(
                    [item.evidence for item in items],
                    key=lambda entry: (entry["kind"], entry["path"]),
                ),
            }
        )
    return {
        "schema": SCHEMA,
        "built_at": built_at or datetime.now(UTC).isoformat(),
        "code_rev": code_rev or _code_revision(root),
        "inputs": sorted(inputs, key=lambda entry: (entry["kind"], entry["path"])),
        "tasks": tasks,
    }


def run_build(args: argparse.Namespace, root: Path) -> int:
    """CLI handler: validate everything before creating the output."""
    expanded_out = args.out.expanduser()
    out = check_dev_path_allowed(
        expanded_out if expanded_out.is_absolute() else root / expanded_out
    )
    if out.exists():
        raise FileExistsError(f"refusing to overwrite task-status output: {out}")
    manifest = build_task_status(
        admission=args.admission,
        gold_results=args.gold_results,
        alt_replays=args.alt_replays,
        repo_root=root,
    )
    if not manifest["inputs"]:
        raise ValueError("provide at least one --admission, --gold-results, or --alt-replays input")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    counts = {
        status: sum(row["status"] == status for row in manifest["tasks"])
        for status in ("certified", "provisional", "weak", "uncertified")
    }
    print(
        json.dumps(
            {"out": str(out), "tasks": len(manifest["tasks"]), "counts": counts}, sort_keys=True
        )
    )
    return 0
