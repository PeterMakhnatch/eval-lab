#!/usr/bin/env python3
"""Inventory immutable MiMo executions and join EXISTING repaired-grade receipts ($0).

No command in this file launches a verifier, sandbox, or model. Source result.json,
not a batch's error/reward projection, owns native identity and original reward.
Aliases of one trial UUID are one execution; conflicting alias bytes remain flagged.

Recipes (run from the repository root; output directories must be new)::

  uv run --no-sync python research/experiments/test-repair-performance-20261017/partition.py inventory \
    --variants ~/Developer/eval-lab-results/2026-10-16/envcheck-repair/repaired-v6-variants-manifest.json \
    --out /tmp/envcheck-inventory
  uv run --no-sync python research/experiments/test-repair-performance-20261017/partition.py join \
    --inventory /tmp/envcheck-inventory/inventory.jsonl \
    --receipt ~/Developer/eval-lab/derived/qa-trace/training-arms/2026-10-16/inputs/regrade-final-v5.json \
    --out /tmp/envcheck-partition

Inventory scans the results home and retained checkout/worktree runs by default.
--source-root replaces that scan (repeatable); --annotations adds retained review
rows, never scores. --include-nonpasses is for M3 only; default inventory is R_orig=1.
Every manifest task appears in summary.coverage, including tasks with zero sources.
PARTIAL variants remain admitted for mechanical repaired-grader partitions when an
exact valid receipt exists. Their original status, killed/total counts and declared
survivors remain in variant.manifest_row; they are NOT promoted to fully qualified.
Pins expose measurement_eligible separately from qualified. Eligibility binds an
accepted repaired/partial status, current package bytes and retained reference/nop
controls (legacy v6-controls/trials, or an explicit qualification_receipt). Missing
custody, candidate/infra status, or infrastructure-observation zero cannot grant
measurement eligibility. These partitions are not independent semantic correctness.
For a moved/expanded manifest, legacy rows can set control_receipts_dir to the
retained v6-controls/trials directory. Native rows use qualification_receipt with
the original parent/task/package lineage joined explicitly; no task ID is guessed.

JSONL join unit = (execution_id, repaired_digest), never archive path or task alone.
Shared columns: execution_id (native trial UUID), task (original canonical parent),
model, attempt (observed aN suffix, otherwise null), original_reward, repaired_reward
(nullable), repaired_digest (FULL sha256), source_trial, receipt (file#rows/index),
status, partition, heldout. status is measured/missing_receipt/invalid_receipt/
conflicting_receipts. partition=true_pass/false_pass requires observed R_orig=1 and
R_rep=1/0; otherwise unmeasured. A measured original failure can have partition
unmeasured and a real repaired_reward. Missing/infra never becomes reward zero.

provenance.receipts preserves each untouched canonical row, file/row hashes and
validation problems. Row hashing is SHA256 of UTF-8 json.dumps(sort_keys=True,
separators=(',', ':'), ensure_ascii=False); all digest strings use sha256: prefix.
Source exceptions, native exit state, copied-fix/teacher/fidelity annotations and
all alias file hashes survive. QA summaries and native regrade-receipt/v1 are
adapters onto the existing replay evidence, NOT a second replay/storage engine.
Training exports and correction generation are in correction_data.py; they enforce
all33 exclusions independently of this inventory's heldout boolean.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from uuid import UUID

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
DEFAULT_HELDOUT = (
    Path.home() / "Developer/eval-lab/.worktrees/mimo-shared-image-restoration/"
    "research/experiments/mimo-training-20261016/heldout.json"
)
FROZEN_HELDOUT_SHA256 = "sha256:d6b0336b4d475f9e9631f295cbde554630e5087c630f3093cc56b13a6e73f9de"
TASK = re.compile(r"mimo-v2\.6-rl/format-code-task-[0-9]{6}")


def load_script(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO / relative)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load existing script: {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ARMS = load_script("envcheck_training_arms", "research/experiments/qa-trace/training-arms/build_arms.py")


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.removeprefix("sha256:").lower()
    return "sha256:" + value if re.fullmatch("[0-9a-f]{64}", value) else None


def file_ref(path: Path) -> dict[str, Any]:
    return {"path": str(path.resolve()), "sha256": sha256(path.read_bytes())}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(canonical_bytes(row).decode() + "\n" for row in rows))


def parent_task(value: str) -> str:
    task = ARMS.canonical_task(value)
    if not TASK.fullmatch(task):
        raise ValueError(f"explicit original MiMo parent ID required, not a variant alias: {value!r}")
    return task


def frozen_heldout(path: Path) -> tuple[set[str], dict[str, Any]]:
    ref = file_ref(path)
    if ref["sha256"] != FROZEN_HELDOUT_SHA256:
        raise ValueError("heldout manifest is not the exact frozen all33 input; do not use measured26")
    tasks = ARMS.heldout_tasks(path)
    if tasks is None or len(tasks) != 33:
        raise ValueError("expected all 33 frozen original heldout parent IDs")
    return tasks, ref


def variants(path: Path) -> list[dict[str, Any]]:
    doc = read_json(path)
    rows = doc.get("variants") if isinstance(doc, dict) else doc
    if not isinstance(rows, list) or not rows:
        raise ValueError("variant manifest needs a nonempty variants list")
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        task = parent_task(row.get("parent_task") or row.get("original_task") or row["task"])
        pin = digest(row.get("variant_digest") or row.get("repaired_digest"))
        package = row.get("variant_package_path") or row.get("package_path")
        if pin is None or not package:
            raise ValueError(f"manifest row lacks exact package digest/path: {task}")
        accepted = row.get("status") in {"repaired", "partial", "qualified"} or (
            row.get("status") is None and row.get("qualified") is True
        )
        item = {
            "task": task,
            "repaired_digest": pin,
            "package_path": str((path.parent / package).resolve()),
            "qualified": accepted and row.get("status") != "partial" and row.get("qualified") is not False,
            "manifest_row": row,
            "manifest": file_ref(path),
        }
        item["measurement_custody"] = measurement_custody(item, path, accepted=accepted)
        item["measurement_eligible"] = not item["measurement_custody"]["problems"]
        key = task, pin
        if key in out and out[key] != item:
            raise ValueError(f"conflicting manifest rows: {key}")
        out[key] = item
    return list(out.values())


def measurement_custody(pin: dict[str, Any], manifest: Path, *, accepted: bool) -> dict[str, Any]:
    """Bind mechanical eligibility to retained controls without re-running them."""
    from evallab.task_variants import task_directory_digest

    row = pin["manifest_row"]
    evidence: dict[str, Any] = {"manifest": pin["manifest"], "controls": [], "problems": []}
    if not accepted:
        evidence["problems"].append("manifest_status_not_accepted")
    try:
        package = Path(pin["package_path"])
        if not package.is_dir():
            raise FileNotFoundError(package)
        actual = task_directory_digest(package)
        evidence["package_digest"] = actual
        if actual != pin["repaired_digest"]:
            evidence["problems"].append("repaired_package_digest_mismatch")
    except (OSError, ValueError) as exc:
        evidence["problems"].append(f"repaired_package_unavailable:{type(exc).__name__}:{exc}")
    qualification = row.get("qualification_receipt")
    if qualification:
        try:
            path = (manifest.parent / qualification).resolve()
            control = read_json(path)
            if not isinstance(control, dict) or not isinstance(control.get("checks"), list) or any(not isinstance(check, dict) for check in control["checks"]):
                raise ValueError("qualification needs a checks object list")
            evidence["qualification_receipt"] = file_ref(path)
            if digest(control.get("package_digest")) != pin["repaired_digest"] or parent_task(control["task"]) != pin["task"]:
                evidence["problems"].append("qualification_package_or_parent_mismatch")
            matrix = file_ref(Path(control["matrix"]))
            evidence["matrix"] = matrix
            if matrix["sha256"] != digest(control.get("matrix_sha256")):
                evidence["problems"].append("qualification_matrix_changed")
            for role, expected in (("reference", 1.0), ("nop", 0.0)):
                checks = [check for check in control.get("checks", []) if check.get("role") == role]
                if len(checks) != 1:
                    evidence["problems"].append(f"{role}_control_missing_or_ambiguous")
                    continue
                check = checks[0]
                evidence["controls"].append({"role": role, "row": check, "row_sha256": sha256(canonical_bytes(check))})
                repeats = check.get("repeats") or []
                if check.get("status") != "ok" or check.get("error") or number(check.get("expected_reward")) != expected or digest(check.get("matrix_sha256")) != matrix["sha256"] or [number(v) for v in repeats] != [expected] * 3:
                    evidence["problems"].append(f"{role}_control_unmeasured_or_failed")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            evidence["problems"].append(f"qualification_custody_unavailable:{type(exc).__name__}:{exc}")
    else:
        # Existing accepted31 archive layout; the older repair receipt can be v5,
        # so it is NOT substituted for the exact ported-v6 control receipts.
        number_id = pin["task"].rsplit("-", 1)[-1]
        control_dir = (manifest.parent / row.get("control_receipts_dir", "v6-controls/trials")).resolve()
        for role, filename, expected in (("reference", "oracle", 1.0), ("nop", "nop", 0.0)):
            path = control_dir / f"{number_id}-{filename}.json"
            try:
                control = read_json(path)
                if not isinstance(control, dict):
                    raise ValueError("control receipt must be an object")
                evidence["controls"].append({**file_ref(path), "role": role, "row": control,
                                            "row_sha256": sha256(canonical_bytes(control))})
                repeats = control.get("repeats") or []
                if digest(control.get("ported_digest")) != pin["repaired_digest"] or control.get("task") != number_id:
                    evidence["problems"].append(f"{role}_control_package_or_parent_mismatch")
                if control.get("role") != filename:
                    evidence["problems"].append(f"{role}_control_role_mismatch")
                if control.get("status") != "ok" or control.get("error") or number(control.get("reward")) != expected or [number(v) for v in repeats] != [expected] * 3:
                    evidence["problems"].append(f"{role}_control_unmeasured_or_failed")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                evidence["problems"].append(f"{role}_control_custody_unavailable:{type(exc).__name__}:{exc}")
    return evidence


def mechanical_grader_admitted(pin: dict[str, Any]) -> bool:
    """Require native control custody independently of semantic repair status."""
    return pin.get("measurement_eligible") is True


def score(result: dict[str, Any]) -> float | None:
    value = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    return number(value)


def number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def default_roots() -> list[Path]:
    from evallab.results_home import results_root
    from evallab.storage.paths import shared_checkout_root

    shared = shared_checkout_root(REPO)
    worktrees = shared / ".worktrees"
    roots = [results_root(), shared / "runs", REPO / "runs"]
    if worktrees.is_dir():
        roots += [path / "runs" for path in worktrees.iterdir() if path.is_dir()]
    return sorted({p.resolve() for p in roots if p.is_dir()})


def native_messages(path: Path) -> tuple[list[dict[str, Any]], list[Any] | None]:
    doc = read_json(path)
    main = (doc.get("trajs") or {}).get("main")
    if not isinstance(main, dict) or not isinstance(main.get("messages"), list):
        raise ValueError(f"missing actual native main conversation: {path}")
    return main["messages"], main.get("tools")


def source_record(trial: Path) -> dict[str, Any]:
    """Read a real native trial; never promote a copied patch to an assistant turn."""
    from evallab.qa.counterfactual import source_digest

    trial = trial.resolve()
    result = read_json(trial / "result.json")
    execution_id = str(UUID(result["id"]))
    task = parent_task(result["task_name"])
    config = result.get("config") or {}
    agent = config.get("agent") or {}
    model = agent.get("model_name")
    if not model:
        info = (result.get("agent_info") or {}).get("model_info") or {}
        model = f"{info['provider']}/{info['name']}" if info.get("provider") else info.get("name")
    if not model or agent.get("name") in {"oracle", "nop"} or config.get("source_trial"):
        raise ValueError("not an original model execution")
    files = {}
    for key, relative in {
        "result": "result.json", "config": "config.json", "lock": "lock.json",
        "patch": "verifier/agent.diff", "trajectory": "agent/trajectory.json",
        "native": "agent/mimoagent/native-trajectory.json", "fingerprint": "setup-fingerprint.json",
        "artifact_manifest": "artifacts/manifest.json",
    }.items():
        path = trial / relative
        if path.is_file():
            files[key] = file_ref(path)
    fidelity = "missing_trajectory"
    fidelity_error = None
    try:
        if "native" in files:
            native_messages(Path(files["native"]["path"]))
            fidelity = "native_messages"
        elif "trajectory" in files:
            _, _, fidelity = ARMS.to_messages(read_json(Path(files["trajectory"]["path"])))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        fidelity = "unreadable_trajectory"
        fidelity_error = f"{type(exc).__name__}: {exc}"
    lock = read_json(trial / "lock.json") if "lock" in files else {}
    metadata = (result.get("agent_result") or {}).get("metadata") or {}
    names = [str(result.get("trial_name", "")), Path(config.get("trials_dir") or "").name]
    attempt_evidence = {}
    for name in names:
        suffix = re.search(r"(?:^|[-_])a([0-9]+)(?:__|$)", name)
        if suffix:
            attempt_evidence[name] = int(suffix[1])
    observed_attempts = set(attempt_evidence.values())
    flags = {
        "exception_info": result.get("exception_info"),
        "native_exit_status": metadata.get("native_exit_status"),
        "teacher": metadata.get("teacher") is True or "codex" in str(agent.get("name", "")).lower(),
        "copied_fix": metadata.get("copied_fix") is True,
        "agent_metadata": metadata,
        "fidelity": fidelity,
        "fidelity_error": fidelity_error,
        "attempt_evidence": attempt_evidence,
        "attempt_conflict": len(observed_attempts) > 1,
        "source_patch_missing": "patch" not in files,
        "extra_instruction_paths": config.get("extra_instruction_paths"),
        "extra_instructions": config.get("extra_instructions"),
    }
    alias = {
        "trial": str(trial), "trial_uuid": execution_id, "job_uuid": config.get("job_id"),
        "source_digest": digest(source_digest(trial)), "files": files,
        "task_checksum": result.get("task_checksum"),
        "executed_task_digest": (lock.get("task") or {}).get("digest"),
        "flags": flags,
    }
    return {
        "execution_id": execution_id, "task": task, "model": model,
        "attempt": next(iter(observed_attempts)) if len(observed_attempts) == 1 else None,
        "original_reward": score(result),
        "source_trial": str(trial), "agent": (result.get("agent_info") or {}).get("name") or agent.get("name"),
        "started_at": result.get("started_at"), "flags": flags, "alias": alias,
    }


def inventory(
    pins: list[dict[str, Any]], roots: list[Path], annotations: list[Path], *, include_nonpasses: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks = {v["task"] for v in pins}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues: list[dict[str, Any]] = []
    seen = set()
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"source root does not exist: {root}")
        for path in sorted(root.rglob("result.json")):
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            try:
                raw = read_json(path)
                name = raw.get("task_name", "")
                canonical = name if isinstance(name, str) and "/" in name else f"mimo-v2.6-rl/{name}"
                if not TASK.fullmatch(canonical) or canonical not in tasks:
                    continue
                # Job aggregates have no trial UUID/task_name; controls/regrades are not sources.
                cfg = raw.get("config") or {}
                if (cfg.get("agent") or {}).get("name") in {"oracle", "nop"} or cfg.get("source_trial"):
                    continue
                entry = source_record(path.parent)
                grouped[entry["execution_id"]].append(entry)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    notes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in annotations:
        if path.suffix == ".json":
            document = read_json(path)
            entries = document.get("observations", document.get("rows")) if isinstance(document, dict) else document
            if not isinstance(entries, list):
                raise ValueError(f"annotation JSON needs observations/rows: {path}")
        else:
            entries = read_jsonl(path)
        for row in entries:
            if row.get("execution_id"):
                notes[row["execution_id"]].append({"source": file_ref(path), "row": row})
    output = []
    for key, copies in sorted(grouped.items()):
        if not include_nonpasses and not any(c["original_reward"] == 1.0 for c in copies):
            continue
        # Prefer a retained alias with native messages and its own pre-hidden-test patch.
        copies.sort(key=lambda c: (-int(c["original_reward"] == 1.0), -int("native" in c["alias"]["files"]), -int("patch" in c["alias"]["files"]), c["source_trial"]))
        source = copies[0]
        conflicts = []
        for field in ("task", "model", "original_reward", "attempt"):
            if len({canonical_bytes(c[field]) for c in copies}) > 1:
                conflicts.append(f"alias_{field}_conflict")
        for field in ("exception_info", "native_exit_status"):
            if len({canonical_bytes(c["flags"][field]) for c in copies}) > 1:
                conflicts.append(f"alias_{field}_conflict")
        for field in ("patch", "trajectory", "native"):
            values = {c["alias"]["files"][field]["sha256"] for c in copies if field in c["alias"]["files"]}
            if len(values) > 1:
                conflicts.append(f"alias_{field}_conflict")
        review_text = " ".join(
            str(note["row"].get(field) or "")
            for note in notes.get(key, [])
            for field in ("review_why", "reason", "summary")
        )
        flags = source["flags"] | {
            "alias_conflicts": conflicts,
            "teacher": any(c["flags"]["teacher"] for c in copies),
            "copied_fix": any(c["flags"]["copied_fix"] for c in copies) or any(
                note["row"].get("copied_fix") is True for note in notes.get(key, [])
            ) or bool(re.search(r"leak copy|copied[- ]fix|reference[- ]copy|copy of reference", review_text, re.I)),
            "annotations": notes.get(key, []),
        }
        output.append({
            **{k: v for k, v in source.items() if k not in {"alias", "flags"}},
            "flags": flags,
            "provenance": {"trial_uuid": key, "job_uuid": source["alias"]["job_uuid"],
                           "source_result_sha256": source["alias"]["files"]["result"]["sha256"],
                           "aliases": [c["alias"] for c in copies]},
        })
    return output, issues


def receipt_trials(row: dict[str, Any]) -> list[Path]:
    """Resolve retained native outputs, including normal process-job archive moves."""
    direct = row.get("regrade_dir") or row.get("regrade_trial_dir")
    trials = [Path(direct)] if direct and (Path(direct) / "result.json").is_file() else []
    job = Path(row["job_dir"]) if row.get("job_dir") else Path(direct).parent if direct else None
    if job and job.is_dir():
        trials.extend(path.parent for path in job.glob("*/result.json"))
    if job and not trials:
        from evallab.results_home import results_root

        trials.extend(path.parent for path in results_root().glob(f"*/*-{job.name}/*/result.json"))
    return sorted({path.resolve() for path in trials})


def receipt_rows(paths: list[Path]) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for supplied in paths:
        files = sorted(supplied.rglob("summary.json")) + sorted(supplied.rglob("regrade-receipt.json")) if supplied.is_dir() else [supplied]
        for path in files:
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            ref = file_ref(path)
            doc = read_json(path)
            if isinstance(doc, dict) and isinstance(doc.get("rows"), list):
                rows = doc["rows"]
            elif isinstance(doc, dict) and doc.get("schema_version") == "regrade-receipt/v1" or isinstance(doc, dict) and doc.get("engine") == "qa-trace-counterfactual/2":
                rows = [doc]
            else:
                raise ValueError(f"not a canonical QA/native repaired regrade receipt: {path}")
            for index, row in enumerate(rows):
                native_results = []
                for trial in receipt_trials(row):
                    result = read_json(trial / "result.json")
                    native_results.append({
                        **file_ref(trial / "result.json"), "trial_uuid": result.get("id"),
                        "job_uuid": (result.get("config") or {}).get("job_id"),
                        "reward": score(result), "exception_info": result.get("exception_info"),
                    })
                attachments = {}
                matrix = Path(row["matrix_receipt"]) if row.get("matrix_receipt") else None
                if matrix and matrix.is_file():
                    attachments["matrix_receipt"] = file_ref(matrix)
                output.append({**ref, "selector": f"{path.resolve()}#rows/{index}",
                               "row_sha256": sha256(canonical_bytes(row)), "row": row,
                               "native_results": native_results, "attachments": attachments})
    return output


def receipt_execution(row: dict[str, Any]) -> str | None:
    if row.get("execution_id"):
        return row["execution_id"]
    source = row.get("source") or {}
    trial = source.get("trial_dir") or row.get("trial")
    if trial and (Path(trial) / "result.json").is_file():
        return read_json(Path(trial) / "result.json").get("id")
    return None


def validate_receipt(source: dict[str, Any], pin: dict[str, Any], row: dict[str, Any]) -> list[str]:
    from evallab.regrade import verifier_identity
    from evallab.task_variants import task_directory_digest

    problems = []
    if not mechanical_grader_admitted(pin):
        problems.append("manifest_variant_not_admitted")
    aliases = source["provenance"]["aliases"]
    if row.get("schema_version") == "regrade-receipt/v1":
        ident = row.get("source") or {}
        matches = [a for a in aliases if a["files"]["result"]["sha256"] == digest(ident.get("result_digest"))]
        if not matches:
            problems.append("native_source_result_hash_mismatch")
        for key, field in (
            ("trajectory", "trajectory_digest"), ("patch", "agent_patch_digest"),
            ("artifact_manifest", "artifact_manifest_digest"),
        ):
            if ident.get(field) and not any(a["files"].get(key, {}).get("sha256") == digest(ident[field]) for a in matches):
                problems.append(f"native_source_{key}_hash_mismatch")
        package = Path(pin["package_path"])
        if not package.is_dir() or task_directory_digest(package) != pin["repaired_digest"]:
            problems.append("native_repaired_package_missing_or_changed")
        elif (row.get("verifier") or {}).get("digest") != verifier_identity(package).digest:
            problems.append("native_verifier_digest_mismatch")
        recorded = ((row.get("recorded") or {}).get("rewards") or {}).get("reward")
        if row.get("verdict") == "refused" or row.get("refusals"):
            problems.append("native_regrade_refused")
        if (row.get("execution") or {}).get("exit_code") != 0:
            problems.append("native_regrade_execution_not_successful")
    else:
        if row.get("engine") != "qa-trace-counterfactual/2":
            problems.append("unknown_replay_engine")
        if digest(row.get("grader_digest")) != pin["repaired_digest"]:
            problems.append("repaired_digest_mismatch")
        if row.get("task") != source["task"]:
            problems.append("source_task_mismatch")
        if digest(row.get("source_digest")) not in {a["source_digest"] for a in aliases}:
            problems.append("source_bytes_mismatch")
        controls = row.get("controls") or {}
        if row.get("valid_grader") is not True or set(controls) != {"nop", "reference"} or number(controls.get("nop")) != 0.0 or number(controls.get("reference")) != 1.0:
            problems.append("unvalidated_grader")
        if row.get("execution_error") or row.get("refusals") or row.get("outcome") == "refused":
            problems.append("replay_refused_or_infrastructure_error")
        recorded = row.get("recorded")
    if number(recorded) != source["original_reward"]:
        problems.append("original_reward_mismatch")
    if repaired_score(row) not in {0.0, 1.0}:
        problems.append("missing_or_nonbinary_repaired_reward")
    if source.get("flags", {}).get("alias_conflicts"):
        problems.append("source_alias_conflict")
    return problems


def repaired_score(row: dict[str, Any]) -> float | None:
    value = row.get("regraded")
    if isinstance(value, dict):
        value = (value.get("rewards") or {}).get("reward")
    return number(value)


def join_one(source: dict[str, Any], pin: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    inspected = []
    for evidence in receipts:
        row = evidence["row"]
        is_native = row.get("schema_version") == "regrade-receipt/v1"
        if not is_native and digest(row.get("grader_digest")) != pin["repaired_digest"]:
            continue
        # Native receipts pin verifier content, not package bytes; the adapter validates both.
        if is_native and str(Path((row.get("verifier") or {}).get("task_dir", "")).resolve()) != pin["package_path"]:
            continue
        problems = validate_receipt(source, pin, row)
        # Retained native observations outrank a lossy batch/summary projection.
        # Keep the observation in provenance, but never certify its infra zero.
        for replay in evidence.get("native_results", []):
            if replay.get("exception_info"):
                problems.append("native_replay_exception")
            if number(replay.get("reward")) != repaired_score(row):
                problems.append("native_replay_reward_missing_or_mismatched")
        inspected.append({**evidence, "valid": not problems, "problems": problems})
    valid = [r for r in inspected if r["valid"]]
    values = {repaired_score(r["row"]) for r in valid}
    selected = valid[0] if len(values) == 1 else None
    reward = repaired_score(selected["row"]) if selected else None
    status = "measured" if selected else "conflicting_receipts" if len(values) > 1 else "invalid_receipt" if inspected else "missing_receipt"
    partition = "unmeasured"
    if source["original_reward"] == 1.0 and reward in {0.0, 1.0}:
        partition = "true_pass" if reward == 1.0 else "false_pass"
    return {
        **source, "repaired_reward": reward, "repaired_digest": pin["repaired_digest"],
        "receipt": selected["selector"] if selected else None, "status": status, "partition": partition,
        "partition_scope": "mechanical_repaired_grader",
        "variant": pin,
        "provenance": {**source["provenance"], "receipts": inspected,
                       "selected_receipt": selected, "original_flags": source.get("flags", {})},
    }


def join_rows(sources: list[dict[str, Any]], pins: list[dict[str, Any]], receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_execution: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt in receipts:
        key = receipt_execution(receipt["row"])
        if key:
            by_execution[key].append(receipt)
    return [join_one(s, p, by_execution.get(s["execution_id"], []))
            for s in sources for p in pins if s["task"] == p["task"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    inv = commands.add_parser("inventory", help="read all actual source trials; no execution")
    inv.add_argument("--variants", type=Path, required=True)
    inv.add_argument("--source-root", type=Path, action="append")
    inv.add_argument("--annotations", type=Path, action="append", default=[])
    inv.add_argument("--include-nonpasses", action="store_true")
    inv.add_argument("--heldout-manifest", type=Path, default=DEFAULT_HELDOUT)
    inv.add_argument("--out", type=Path, required=True)
    join = commands.add_parser("join", help="join canonical QA/native receipts, never regrade")
    join.add_argument("--inventory", type=Path, required=True)
    join.add_argument("--receipt", type=Path, action="append", default=[])
    join.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inventory":
        pins = variants(args.variants)
        heldout, heldout_ref = frozen_heldout(args.heldout_manifest)
        annotation = REPO / "research/experiments/qa-trace/regrade/ordinary-passes.jsonl"
        annotations = [annotation, *args.annotations] if annotation.is_file() else args.annotations
        from evallab.storage.paths import shared_checkout_root

        audit = shared_checkout_root(REPO) / "derived/qa-trace/training-arms/2026-10-16/inputs/full-prompt-comparator-audit-v4.json"
        if audit.is_file():
            annotations.append(audit)
        roots = args.source_root or default_roots()
        rows, issues = inventory(pins, roots, annotations, include_nonpasses=args.include_nonpasses)
        for row in rows:
            row["heldout"] = row["task"] in heldout
        args.out.mkdir(parents=True, exist_ok=False)
        write_jsonl(args.out / "inventory.jsonl", rows)
        write_jsonl(args.out / "passes.jsonl", [
            {**r, "item_id": f"envcheck:{r['execution_id']}", "trial_path": r["source_trial"],
             "observed_reward": r["original_reward"], "observed_exception": r["flags"]["exception_info"]}
            for r in rows if r["original_reward"] == 1.0
        ])
        write_jsonl(args.out / "source-issues.jsonl", issues)
        write_json(args.out / "manifest.json", {"variants": pins, "heldout": heldout_ref,
                   "inventory": file_ref(args.out / "inventory.jsonl"),
                   "passes": file_ref(args.out / "passes.jsonl")})
        summary = {"executions": len(rows), "source_roots": [str(p.resolve()) for p in roots],
                   "issues": len(issues), "heldout_executions": sum(r["heldout"] for r in rows),
                   "coverage": {task: sum(r["task"] == task for r in rows) for task in sorted({p["task"] for p in pins})}}
    else:
        manifest = read_json(args.inventory.parent / "manifest.json")
        if file_ref(args.inventory) not in (manifest["inventory"], manifest.get("passes")):
            raise ValueError("inventory or passing-view bytes/path no longer match their source manifest")
        rows = join_rows(read_jsonl(args.inventory), manifest["variants"], receipt_rows(args.receipt))
        args.out.mkdir(parents=True, exist_ok=False)
        write_jsonl(args.out / "partition.jsonl", rows)
        for name in ("true_pass", "false_pass", "unmeasured"):
            write_jsonl(args.out / f"{name}.jsonl", [r for r in rows if r["partition"] == name])
        summary = {"rows": len(rows), "partitions": dict(Counter(r["partition"] for r in rows)),
                   "statuses": dict(Counter(r["status"] for r in rows)), "inventory": file_ref(args.inventory),
                   "heldout": manifest["heldout"], "receipt_inputs": [str(p.resolve()) for p in args.receipt],
                   "missing_prerequisites": sorted({r["status"] for r in rows if r["status"] != "measured"})}
    write_json(args.out / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
