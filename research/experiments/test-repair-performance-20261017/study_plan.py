"""Freeze an offline M3 schedule from the accepted B/C common cohort.

No model, sandbox, queue, or verifier is invoked. The accepted report's 26 task IDs,
not observed repair outcomes, determine membership. All 33 frozen parent IDs remain
training exclusions. Missing qualified repairs are unresolved plan cells, never
replaced with the original grader. Re-running with a new repair manifest creates a
new digest-bound plan, not an in-place change to a measured experiment.

    python research/experiments/test-repair-performance-20261017/study_plan.py \
        --out /tmp/envcheck-m3-plan.json

Optional --repair-manifest JSON consumes the retained variants manifest, or a list
of qualify.py qualification objects (qualified=true, checks, package_digest) with
v6_parent_digest_of_variant and package_path added from retained VariantRecord
lineage. Multiple qualified repairs require --grader TASK=sha256:FULL_DIGEST.
--pin-graders is the accepted pin CSV, not a model input. All file references use
full SHA-256. Source package digests and Harbor task checksums are distinct fields.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home() / "Developer"
BRIDGE = HOME / "research-context/bridge/overnight-2026-10-16"
FROZEN = (
    HOME / "eval-lab/.worktrees/mimo-shared-image-restoration"
    / "research/experiments/mimo-training-20261016"
)
QA = HOME / "eval-lab/.worktrees/qa-modal"
MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B:rds1-b"
SCHEMA = "envcheck.m3-plan/v1"
TASK_PATTERN = re.compile(r"format-code-task-\d{6}")
DIGEST_PATTERN = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
ATTEMPT_PATTERN = re.compile(r"-a([1-9]\d*)$")
BOOTSTRAP = {"seed": 20261017, "replicates": 10000, "confidence": 0.95}


def digest(value: str) -> str:
    match = DIGEST_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"expected a full SHA-256, got {value!r}")
    return "sha256:" + match.group(1)


def object_digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def file_ref(path: Path, expected: str | None = None) -> dict[str, Any]:
    path = path.expanduser().resolve()
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    actual = "sha256:" + hasher.hexdigest()
    if expected is not None and actual != digest(expected):
        raise ValueError(f"source hash changed: {path}: {actual} != {digest(expected)}")
    return {"path": str(path), "sha256": actual, "bytes": path.stat().st_size}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"JSONL requires object rows: {path}")
    return rows


def canonical_task(value: str) -> str:
    name = value.removeprefix("mimo-v2.6-rl/")
    if TASK_PATTERN.fullmatch(name) is None:
        raise ValueError(f"not a canonical MiMo parent task: {value!r}")
    return "mimo-v2.6-rl/" + name


def path_task(value: str) -> str:
    matches = TASK_PATTERN.findall(value)
    if len(matches) != 1:
        raise ValueError(f"ambiguous source task path: {value!r}")
    return canonical_task(matches[0])


def binary_reward(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)) and value in (0, 1):
        return int(value)
    raise ValueError(f"reward must be observed 0/1/null, got {value!r}")


def comparable_settings(result: dict[str, Any], fingerprint: dict[str, Any]) -> dict[str, Any]:
    """Retain execution settings, omitting task/name/observational bookkeeping only."""
    config = result.get("config") or {}
    agent = config.get("agent") or {}
    return {
        "model": agent.get("model_name"),
        "agent": agent.get("name"),
        "native_revision": (result.get("agent_info") or {}).get("version"),
        "fingerprint": {
            key: fingerprint.get(key)
            for key in ("budgets", "environment", "harness", "sampling", "server", "reference_profile")
        },
        "lock": {key: (fingerprint.get("lock") or {}).get(key) for key in ("mode", "effective")},
        "timeouts": {key: config.get(key) for key in (
            "timeout_multiplier", "agent_timeout_multiplier", "verifier_timeout_multiplier",
            "agent_setup_timeout_multiplier", "environment_build_timeout_multiplier",
        )},
        "agent_flags": {key: (agent.get("kwargs") or {}).get(key)
                        for key in ("antihack", "explicit_rules")},
        "environment": config.get("environment"),
        "verifier": config.get("verifier"),
    }


def native_source(trial: Path) -> dict[str, Any]:
    """Read native receipts; a scored budget stop is not a clean completion."""
    result_path = trial / "result.json"
    result = load_json(result_path)
    fingerprint_path = trial / "setup-fingerprint.json"
    fingerprint = load_json(fingerprint_path) if fingerprint_path.is_file() else {}
    metadata = ((result.get("agent_result") or {}).get("metadata") or {})
    exception = result.get("exception_info")
    exit_status = metadata.get("native_exit_status")
    budget_stop = (
        (exception or {}).get("exception_type") == "TrialBudgetExhaustedError"
        or metadata.get("stop_reason") == "trial_budget_exhausted"
    )
    clean = not exception and exit_status == "Idle" and bool(result.get("finished_at"))
    settings = comparable_settings(result, fingerprint)
    refs = {"result": file_ref(result_path)}
    for key, path in (
        ("fingerprint", fingerprint_path),
        ("lock", trial / "lock.json"),
        ("native_trajectory", trial / "agent/mimoagent/native-trajectory.json"),
        ("patch", trial / "verifier/agent.diff"),
        ("experiment_spec", trial.parent / "experiment-spec.json"),
        ("job_result", trial.parent / "result.json"),
    ):
        if path.is_file():
            refs[key] = file_ref(path)
    return {
        "source_trial": str(trial.resolve()),
        "execution_id": result.get("id"),
        "job_uuid": (result.get("config") or {}).get("job_id"),
        "task": canonical_task(result["task_name"]),
        "model": ((result.get("config") or {}).get("agent") or {}).get("model_name"),
        "task_digest": (fingerprint.get("task") or {}).get("digest"),
        "task_checksum": result.get("task_checksum"),
        "native_reward": binary_reward(((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")),
        "exception_info": exception,
        "native_exit_status": exit_status,
        "stop_reason": metadata.get("stop_reason"),
        "infra_error": metadata.get("infra_error"),
        "completion": "clean" if clean else "budget_stop" if budget_stop else "infra_or_incomplete",
        "settings": settings,
        "settings_digest": object_digest(settings),
        "sources": refs,
    }


def resolve_trial(row: dict[str, Any], results_home: Path) -> Path | None:
    job = Path(row["job_dir"])
    candidates = sorted(job.glob("*/result.json")) if job.is_dir() else []
    if not candidates:
        candidates = sorted(results_home.glob(f"*/unknown-{row['name']}/*/result.json"))
    if len(candidates) > 1:
        raise ValueError(f"multiple native trials for one source cell: {row['name']}")
    return candidates[0].parent if candidates else None


def source_cells(
    rows: list[dict[str, Any]], pairs: dict[str, dict[str, str]], results_home: Path,
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    cells: dict[tuple[str, int], dict[str, Any]] = {}
    diagnostics = []
    for line, row in enumerate(rows, 1):
        task = path_task(row["task"])
        match = ATTEMPT_PATTERN.search(row["name"])
        if match is None:
            raise ValueError(f"B source row has no explicit attempt: {row['name']}")
        attempt = int(match.group(1))
        package = pairs[task]["v6_package_digest"]
        if Path(row["task"]).name != digest(package).removeprefix("sha256:")[:12]:
            diagnostics.append({"line": line, "reason": "non-frozen-package diagnostic", "row": row})
            continue
        if attempt not in (1, 2) or (task, attempt) in cells:
            raise ValueError(f"unauthorized or duplicate B cell: {task} a{attempt}")
        trial = resolve_trial(row, results_home)
        native = native_source(trial) if trial else None
        issues = []
        if native is None:
            issues.append("missing_native_trial")
        else:
            if native["task"] != task or native["model"] != MODEL:
                issues.append("native_task_or_model_mismatch")
            if native["task_digest"] != digest(package):
                issues.append("native_source_package_mismatch")
            if not native["execution_id"] or not native["job_uuid"] or not native["task_checksum"]:
                issues.append("missing_native_identity")
            if "fingerprint" not in native["sources"] or "native_trajectory" not in native["sources"]:
                issues.append("missing_native_settings_or_trajectory")
            if native["completion"] == "infra_or_incomplete":
                issues.append("source_infra_or_incomplete")
            if row["reward"] is not None and binary_reward(row["reward"]) != native["native_reward"]:
                issues.append("batch_native_reward_mismatch")
        if row.get("error") or row.get("reward") is None:
            issues.append("batch_unmeasured")
        cells[task, attempt] = {
            "task": task, "attempt": attempt, "model": MODEL, "source_name": row["name"],
            "source_line": line, "source_row_sha256": object_digest(row),
            "source_package_digest": digest(package), "batch_error": row.get("error"),
            "original_reward": binary_reward(row.get("reward")), "native": native,
            "reuse_issues": issues,
        }
    for (task, attempt), cell in cells.items():
        first = cells.get((task, 1))
        if attempt == 2 and cell["native"] and first and first["native"]:
            for field in ("settings_digest", "task_checksum", "task_digest"):
                if cell["native"][field] != first["native"][field]:
                    cell["reuse_issues"].append(f"attempt_pair_{field}_mismatch")
        cell["reusable"] = not cell["reuse_issues"]
    return cells, diagnostics


def qualified_repairs(paths: list[Path]) -> dict[str, list[dict[str, Any]]]:
    """Retain accepted full/partial graders for mechanical policy measurement."""
    repairs: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        document = load_json(path)
        rows = document if isinstance(document, list) else document.get("variants", document.get("tasks", [document]))
        for row in rows:
            status = row.get("status")
            total = row.get("v6_shortcut_replays_total", 0)
            killed = row.get("v6_shortcut_replays_killed", 0)
            declared = row.get("declared_survivors", 0)
            counts_valid = all(type(value) is int and value >= 0 for value in (total, killed, declared))
            full_scope = counts_valid and status == "repaired" and declared == 0 and total > 0 and killed == total
            partial_scope = counts_valid and status == "partial" and killed <= total and total - killed <= declared
            legacy = (
                (full_scope or partial_scope) and row.get("package_digest_verified") is True
                and row.get("v6_oracle_x3") == [1, 1, 1] and row.get("v6_nop_x3") == [0, 0, 0]
            )
            checks = row.get("checks") or []
            wrong = [check for check in checks[2:] if check.get("role") == "known-wrong"]
            native = (
                row.get("qualified") is True and len(checks) >= 3 and bool(wrong)
                and all(check.get("status") == "ok" and not check.get("error") for check in checks)
                and checks[0].get("role") == "reference" and checks[0].get("repeats") == [1, 1, 1]
                and checks[1].get("role") == "nop" and checks[1].get("repeats") == [0, 0, 0]
                and all(
                    check.get("rewards") == [0] if check.get("role") == "known-wrong"
                    else check.get("role") == "prompt-valid-alternate" and check.get("repeats") == [1, 1, 1]
                    for check in checks[2:]
                )
            )
            if not legacy and not native:
                continue
            task = canonical_task(row["task"])
            package_digest = digest(row.get("variant_digest") or row["package_digest"])
            repairs.setdefault(task, []).append({
                "repaired_digest": package_digest,
                "package_path": row.get("variant_package_path") or row.get("package_path"),
                "parent_digest": row.get("v6_parent_digest_of_variant"),
                "measurement_eligible": True,
                "repair_status": status or "native-control-qualified",
                "declared_survivors": declared if legacy else row.get("declared_survivors"),
                "qualification_source": file_ref(path), "qualification_row_sha256": object_digest(row),
                "qualification_row": row,
            })
    return repairs



def accepted_pin_observations(
    receipt: dict[str, Any], pin_rows: list[dict[str, Any]], cells: dict[tuple[str, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reuse only the raw observations bound by the accepted report receipt."""
    raw = []
    for binding in receipt["paired_pin_results"]["raw_item_bindings"]:
        path = Path(binding["path"])
        ref = file_ref(path, binding["sha256"])
        row = load_json(path)
        if (row.get("labels") or {}).get("arm") == "B" and row.get("regraded") is not None:
            raw.append((row, ref))
    observations = []
    for pin in pin_rows:
        if pin.get("arm") != "B":
            continue
        matching = [cell for cell in cells.values() if cell["source_name"] == pin["trial"]]
        if len(matching) != 1:
            raise ValueError(f"pin observation has no unique B source: {pin['trial']}")
        cell = matching[0]
        for row, ref in raw:
            if (row.get("labels") or {}).get("trial") != pin["trial"] or row.get("grader") != pin["grader"]:
                continue
            if row.get("valid_grader") is not True or row.get("controls") != {"reference": 1.0, "nop": 0.0}:
                raise ValueError(f"accepted pin controls are not valid: {ref['path']}")
            value = binary_reward(row["regraded"])
            if value != binary_reward(pin["repaired_pass"]):
                raise ValueError(f"accepted pin snapshot disagrees with raw receipt: {ref['path']}")
            source = cell["native"]
            if not source or Path(row["trial"]).name != Path(source["source_trial"]).name:
                raise ValueError("accepted pin native source identity differs")
            observations.append({
                "execution_id": source["execution_id"], "task": cell["task"], "model": MODEL,
                "attempt": cell["attempt"], "original_reward": cell["original_reward"],
                "repaired_reward": value, "repaired_digest": digest(row["grader_digest"]),
                "source_trial": source["source_trial"], "receipt": ref["path"], "status": "measured",
                "heldout": True, "partition": "true_pass" if value else "false_pass",
                "provenance": {"trial_uuid": source["execution_id"], "job_uuid": source["job_uuid"],
                               "receipts": [{**ref, "row": row, "row_sha256": object_digest(row),
                                             "valid": True, "problems": []}]},
            })
    if len(observations) != sum(row.get("arm") == "B" for row in pin_rows):
        raise ValueError("accepted B pin observations are incomplete or duplicated")
    return observations


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    receipt = load_json(args.accepted_receipt)
    if receipt.get("publication", {}).get("state") != "accepted" or receipt["publication"].get("citable") is not True:
        raise ValueError("the cohort authority must be the accepted, citable corrected report")
    report = load_json(args.accepted_report)
    inputs = {"accepted_report": file_ref(args.accepted_report, receipt["report"]["sha256"]),
              "accepted_receipt": file_ref(args.accepted_receipt)}
    for key, path in (("heldout", args.heldout), ("canonical_B", args.candidates),
                      ("canonical_C", args.cohort_control), ("package_pairs", args.package_pairs),
                      ("pin_regrades", args.pin_regrades), ("paired_grader_manifest", args.pin_graders)):
        inputs[key] = file_ref(path, receipt["inputs"][key]["sha256"])
    frozen = load_json(args.heldout)
    for key in ("split", "validity_release"):
        lineage = frozen[key]
        inputs[f"frozen_{key}"] = file_ref(Path(lineage["path"]), lineage["sha256"])
    if frozen.get("scoring_allowed") is not True or frozen.get("freeze_ready") is not True:
        raise ValueError("frozen heldout scoring has not been released")
    heldout = sorted(canonical_task(task) for task in frozen["heldout"])
    if len(set(heldout)) != 33 or len(heldout) != 33:
        raise ValueError("full frozen33 exclusion lineage is required")
    cohort = sorted(canonical_task(task) for task in receipt["primary"]["common_task_ids"])
    excluded = sorted(canonical_task(task) for task in report["paired"]["B_vs_C"]["missing_or_unpaired_task_ids"])
    if len(set(cohort)) != 26 or set(cohort) != set(heldout) - set(excluded):
        raise ValueError("accepted report and common26 receipt membership disagree")
    with args.package_pairs.open() as stream:
        pairs = {canonical_task(row["task_id"]): row for row in csv.DictReader(stream)}
    cells, diagnostics = source_cells(jsonl(args.candidates), pairs, args.results_home)
    control_first = set()
    for row in jsonl(args.cohort_control):
        task = path_task(row["task"])
        if Path(row["task"]).name != digest(pairs[task]["v6_package_digest"])[7:19]:
            continue
        match = ATTEMPT_PATTERN.search(row["name"])
        if (match is None or int(match.group(1)) == 1) and row.get("reward") is not None and not row.get("error"):
            control_first.add(task)
    reconstructed = {task for (task, attempt), cell in cells.items()
                     if attempt == 1 and cell["original_reward"] is not None and not cell["batch_error"]} & control_first
    if reconstructed != set(cohort):
        raise ValueError("canonical B/C first-attempt membership no longer matches the accepted26")
    repairs = qualified_repairs(args.repair_manifest)
    choices = {}
    for value in args.grader:
        task, separator, value_digest = value.partition("=")
        if not separator or canonical_task(task) in choices:
            raise ValueError("--grader requires unique TASK=sha256:FULL_DIGEST values")
        choices[canonical_task(task)] = digest(value_digest)
    with args.pin_graders.open() as stream:
        pins = {canonical_task(row["task_id"]): row for row in csv.DictReader(stream)}
    tasks = []
    for task in cohort:
        candidates = [cells[task, attempt] for attempt in (1, 2)]
        available = repairs.get(task, [])
        expected = choices.get(task) or (pins.get(task) or {}).get("final_digest")
        if expected:
            available = [row for row in available if row["repaired_digest"] == digest(expected)]
        unique = {row["repaired_digest"]: row for row in available}
        if len(unique) > 1:
            raise ValueError(f"multiple qualified repairs for {task}; pin one with --grader")
        repair = next(iter(unique.values()), None)
        if repair and (not repair["parent_digest"]
                       or digest(repair["parent_digest"]) != digest(pairs[task]["v6_package_digest"])):
            raise ValueError(f"qualified repair lacks exact frozen-v6 parent lineage: {task}")
        tasks.append({
            "task": task, "heldout": True, "training_allowed": False,
            "source_task_digest": digest(pairs[task]["v6_package_digest"]),
            "repaired_digest": repair["repaired_digest"] if repair else None,
            "repair": repair, "candidates": candidates,
            "prerequisites": ([] if repair else ["qualified digest-bound repaired grader for this frozen parent"]),
        })
    observations = accepted_pin_observations(receipt, jsonl(args.pin_regrades), cells)
    training = receipt["inputs"]["training_receipt_B"]
    training_path = Path(training["path"])
    inputs["training_receipt_B"] = file_ref(training_path, training["sha256"])
    training_doc = load_json(training_path)
    inputs["repair_manifests"] = [file_ref(path) for path in args.repair_manifest]
    inputs["plan_builder_source"] = file_ref(Path(__file__))
    return {
        "schema": SCHEMA, "model": MODEL, "n": 2, "attempt_order": [1, 2],
        "model_checkpoint": {"training_receipt": inputs["training_receipt_B"],
                             "base_revision": training_doc["model_revision"],
                             "adapter": training_doc["remote"]["adapter"],
                             "adapter_model_sha256": digest(training_doc["remote"]["adapter_model_sha256"]),
                             "attestation_limit": "retained training receipt and declared serving settings; not fresh remote attestation"},
        "inputs": inputs, "statistics": BOOTSTRAP,
        "population": {"planned_task_ids": cohort, "planned_tasks": 26,
                       "frozen_heldout_parent_ids": heldout, "training_exclusion_parent_ids": heldout,
                       "excluded_from_m3": [{"task": task, "reason": "outside previously accepted26 common cohort"}
                                            for task in excluded],
                       "selection_rule": "accepted corrected B-a1/C-a1 common26; never subset by repair outcomes",
                       "exposure_note": "previously evaluated heldout cohort, not a fresh unseen holdout; all33 excluded from training"},
        "policies": {
            "first_attempt": "candidate a1 only, evaluated on the exact pinned repaired digest",
            "best_of_2": "require both candidate grades; earliest repaired pass wins, ties earliest; if neither passes return a1",
            "reset_context_feedback": "retain a1 if repaired-pass; otherwise one executed same-model reset-context correction from a1's patch and safe repaired failing stdout, then regrade its actual output",
            "correction_generation_budget": "at most 2 generations per task: a1 plus one conditional correction; B a2 is an independent attempt, never a correction",
            "model_inputs": ["original task prompt", "source candidate patch", "safe repaired-test failing stdout"],
            "forbidden_inputs": ["repaired test source", "reference/solution source", "other trajectory history"],
        },
        "interpretation": {
            "primary_endpoint": "repaired-verifier-selected reward; selection/evaluation use the same grader and are dependent",
            "independent_semantic_validation": "unmeasured unless separately supplied independent evidence is bound to each selected execution",
            "missingness": "missing/infra grades are null, never scored zero; complete-case paired denominators and exclusions are explicit",
            "budget_stops": "observed verifier outcomes may include source budget stops; report separately and provide clean-completion sensitivity",
            "causal_limit": "offline policy comparison on retained attempts, not a randomized causal model-improvement experiment",
            "partial_repairs": "accepted partial graders retain status/declared scope; their mechanical reward is not a full semantic-correctness certificate",
            "har168": "diagnostic only: different tasks/runtime/context/parser; excluded from this plan",
        },
        "tasks": tasks, "retained_pin_regrades": observations, "excluded_source_rows": diagnostics,
        "runtime_prerequisites": [
            "parent authorization and readiness for exact same B adapter/model and pinned settings",
            "complete native candidate identity and missing repaired regrades for every planned attempt",
            "safe failing stdout with no echoed hidden-test/reference source and digest-bound source patch",
            "native extra_instruction_path + extra_instruction_sha256 reset-context request, real output execution and repaired replay receipt",
            "separate independent semantic/evaluation evidence to claim anything beyond repaired-verifier reward",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-report", type=Path, default=BRIDGE / "classifiers-b2-c1-report.json")
    parser.add_argument("--accepted-receipt", type=Path, default=BRIDGE / "classifiers-b2-c1-report-receipt.json")
    parser.add_argument("--heldout", type=Path, default=FROZEN / "heldout.json")
    parser.add_argument("--package-pairs", type=Path, default=FROZEN / "package-pairs.csv")
    parser.add_argument("--candidates", type=Path, default=QA / "runs/rollout-batch-rds1-b/results.jsonl")
    parser.add_argument("--cohort-control", type=Path, default=QA / "runs/rollout-batch-rds1-c/results.jsonl")
    parser.add_argument("--pin-regrades", type=Path, default=BRIDGE / "classifiers-b2-c1-pin-regrades.jsonl")
    parser.add_argument("--pin-graders", type=Path,
                        default=QA / "research/experiments/repaired-data-sft-v1/heldout-pin-repaired-graders.csv")
    parser.add_argument("--repair-manifest", type=Path, action="append")
    parser.add_argument("--grader", action="append", default=[], metavar="TASK=SHA256")
    parser.add_argument("--results-home", type=Path, default=HOME / "eval-lab-results")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.repair_manifest is None:
        args.repair_manifest = [HOME / "eval-lab-results/2026-10-16/envcheck-repair/repaired-v6-variants-manifest.json"]
    try:
        plan = build_plan(args)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x") as stream:
            stream.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    except (KeyError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"plan": file_ref(args.out), "planned_tasks": 26, "n": 2,
                      "qualified_repaired_tasks": sum(task["repaired_digest"] is not None for task in plan["tasks"]),
                      "model_calls": 0, "sandbox_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
