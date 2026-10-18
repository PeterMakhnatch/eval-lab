"""Hash-bound, all-attempt heldout-v7 campaign; native execution is never discovery."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import runpy
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from evallab.qa import counterfactual as cf
from evallab.registry import harbor_task_digest
from evallab.task_variants import task_directory_digest

ARMS = ("base", "A", "B", "C")
ATTEMPTS = {"base": [1], "A": [1], "B": [1, 2], "C": [1]}
MODEL = "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
FROZEN_SHA = "d6b0336b4d475f9e9631f295cbde554630e5087c630f3093cc56b13a6e73f9de"
MEMBERSHIP_SHA = "ce90659538cac139177d1e2af5c643a51f48d5f40af2f2a7efcc03ddbad285da"
NAMESPACE = "mimo-v2.6-rl/"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def bare_sha(value: str) -> str:
    value = value.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("expected a full SHA-256 identity")
    return value


def file_ref(path: Path, expected: str | None = None) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=True)
    data = path.read_bytes()
    digest = sha(data)
    if expected is not None and digest != "sha256:" + bare_sha(expected):
        raise ValueError(f"source digest changed: {path}")
    return {"path": str(path), "sha256": digest, "size_bytes": len(data)}


def bound_bytes(binding: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    path = Path(binding["path"]).expanduser().resolve(strict=True)
    data = path.read_bytes()
    digest = sha(data)
    if digest != "sha256:" + bare_sha(binding["sha256"]):
        raise ValueError(f"source digest changed: {path}")
    return data, {"path": str(path), "sha256": digest, "size_bytes": len(data)}


def json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def canonical_task(value: str) -> str:
    leaf = value.removeprefix(NAMESPACE)
    if re.fullmatch(r"format-code-task-[0-9]{6}", leaf) is None:
        raise ValueError(f"not a canonical MiMo parent: {value!r}")
    return NAMESPACE + leaf


def canonical_outcome(row: dict[str, Any]) -> tuple[bool | None, str | None]:
    reward, passed = row.get("reward"), row.get("passed")
    error = row.get("error")
    if error is not None or row.get("measured") is False:
        if reward is not None or passed is not None:
            raise ValueError("canonical error/unmeasured row carries a measured endpoint")
        return None, str(error or "canonical_outcome_unmeasured")
    if reward is None and passed is None:
        return None, "canonical_outcome_unmeasured"
    if (type(passed) is not bool or type(reward) not in (int, float)
            or not math.isfinite(reward) or reward not in (0, 1) or reward != int(passed)):
        raise ValueError("inconsistent/nonbinary canonical endpoint")
    return passed, None


def _native_id(value: Any) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("source lacks a canonical native UUID")
    return value


def _artifacts(root: Path, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: file_ref(root / name) if (root / name).is_file() else None for name in names}


def _trials(job: Path) -> list[Path]:
    if not job.is_dir():
        return []
    found = []
    for result_path in sorted(job.glob("*/result.json")):
        result = json_object(result_path)
        if isinstance(result.get("config"), dict) and result.get("task_name"):
            found.append(result_path.parent.resolve())
    return found


def _package_identity(path: Path) -> tuple[str, str]:
    path = path.resolve(strict=True)
    config = tomllib.loads((path / "task.toml").read_text())
    task = canonical_task(config["task"]["name"])
    if canonical_task(config["metadata"]["source_id"]) != task:
        raise ValueError(f"task/source identity mismatch: {path}")
    return task, task_directory_digest(path)


def _native_row(
    *, trial: Path, job: Path, arm: str, task: str, attempt: int,
    population: str, original: bool | None, error: str | None,
    source: dict[str, Any], package: dict[str, Any], expected_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trial, job = trial.resolve(strict=True), job.resolve(strict=True)
    if trial.parent != job:
        raise ValueError("trial is not a direct child of its bound native job")
    result = json_object(trial / "result.json")
    uid = [_native_id(result["config"]["job_id"]), _native_id(result["id"])]
    if (job / "result.json").is_file() and json_object(job / "result.json").get("id") != uid[0]:
        raise ValueError(f"native parent/config job UUID disagreement: {trial}")
    if expected_result:
        file_ref(trial / "result.json", expected_result["sha256"])
    requested_model = MODEL + (f":rds1-{arm.lower()}" if arm != "base" else "")
    model = result["config"].get("agent", {}).get("model_name")
    if model != requested_model:
        raise ValueError(f"source model does not match arm {arm}: {trial}")
    if canonical_task(result["task_name"]) != task:
        raise ValueError(f"native source task identity disagrees with its canonical cell: {trial}")
    metadata_path = job / "lab-metadata.json"
    metadata = json_object(metadata_path) if metadata_path.is_file() else {}
    staging, identity = metadata.get("task_staging") or {}, metadata.get("model_identity") or {}
    if staging and (staging.get("declared_task_name") != task
                    or staging.get("source_package_digest") != package["sha256"]):
        raise ValueError(f"source staging/task/package disagreement: {trial}")
    if not staging:
        task_lock = json_object(trial / "lock.json")["task"]
        if (task_lock.get("type") != "local"
                or task_lock.get("digest") != harbor_task_digest(Path(package["path"]))):
            raise ValueError(f"source native locked package disagrees with its canonical cell: {trial}")
    if identity.get("requested") not in (None, requested_model):
        raise ValueError(f"source requested-model binding changed: {trial}")
    kept = trial / "verifier" / "agent.kept.diff"
    diff = file_ref(kept) if kept.is_file() else None
    return {
        "source_uid": uid, "source_trial_dir": str(trial), "native_job_dir": str(job),
        "arm": arm, "task_id": task, "attempt_index": attempt, "population": population,
        "v6_outcome": original, "v6_error": error, "diff": diff,
        "source_digest": cf.source_digest(trial), "canonical_source_ref": source,
        "source_package": package, "requested_model": requested_model,
        "native_raw_reward": (result.get("verifier_result") or {}).get("rewards"),
        "native_exception_type": (result.get("exception_info") or {}).get("exception_type"),
        "model_identity": identity, "task_staging": staging,
        "native_artifacts": {
            "job": _artifacts(job, ("result.json", "config.json", "lock.json", "experiment-spec.json",
                                    "lab-metadata.json", "repository-provenance/repository.json")),
            "trial": _artifacts(trial, ("result.json", "config.json", "lock.json", "egress-lock.json",
                                        "setup-fingerprint.json")),
        },
        "aliases": [],
    }


def inventory(config_path: Path) -> dict[str, Any]:
    config_ref = file_ref(config_path)
    config = json_object(config_path)
    if config.get("schema") != "heldout-v7.source-inputs/v1" or set(config["source_files"]) != set(ARMS):
        raise ValueError("source inputs must name all four arms exactly once")
    frozen_bytes, frozen_ref = bound_bytes(config["frozen"])
    if bare_sha(frozen_ref["sha256"]) != FROZEN_SHA:
        raise ValueError("not the authorized original frozen33 artifact")
    leaves = json.loads(frozen_bytes)["heldout"]
    if (len(leaves) != 33 or len(set(leaves)) != 33
            or hashlib.sha256(("\n".join(sorted(leaves)) + "\n").encode()).hexdigest() != MEMBERSHIP_SHA):
        raise ValueError("original33 membership changed")
    tasks = sorted(canonical_task(task) for task in leaves)
    pair_bytes, pair_ref = bound_bytes(config["package_pairs"])
    pairs = {canonical_task(row["task_id"]): row for row in csv.DictReader(io.StringIO(pair_bytes.decode()))}
    if not set(tasks) <= pairs.keys():
        raise ValueError("package mapping lacks frozen task identities")
    sources: dict[str, Any] = {"configuration": config_ref, "frozen": frozen_ref, "package_pairs": pair_ref}
    rows: list[dict[str, Any]] = []
    non_native: list[dict[str, Any]] = []
    scheduled: dict[str, set[str]] = {arm: set() for arm in ARMS}
    seen_cells: set[tuple[str, str, int]] = set()
    seen_uids: set[tuple[str, str]] = set()
    packages: dict[str, tuple[str, str]] = {}
    for arm in ARMS:
        data, source_ref = bound_bytes(config["source_files"][arm])
        sources[arm] = source_ref
        for line_no, line in enumerate(data.decode("utf-8").splitlines(), 1):
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("canonical JSONL row is not an object")
            package_path = Path(raw["task"]).resolve(strict=True)
            key = str(package_path)
            if key not in packages:
                packages[key] = _package_identity(package_path)
            task, digest = packages[key]
            if task not in tasks:
                raise ValueError("canonical source row is outside the frozen33 parents")
            if raw.get("task_id") is not None and canonical_task(raw["task_id"]) != task:
                raise ValueError("canonical row task identity disagrees with its full package")
            if raw.get("arm") not in (None, arm):
                raise ValueError("canonical row arm identity disagrees with its source file")
            name = raw["name"]
            attempt = raw.get("attempt_index")
            if arm in ("B", "C"):
                match = re.fullmatch(r"rollout-mimo-v2-6-distill-qwen-9b-rds1-[bc]-[0-9]{3}-[0-9a-f]{12}(?:-a([1-9][0-9]*))?", name)
                if match is None:
                    raise ValueError(f"unexpected official batch job name: {name}")
                attempt = int(match[1]) if match[1] else 1
            if type(attempt) is not int or attempt not in ATTEMPTS[arm]:
                raise ValueError("unauthorized raw source attempt")
            source = {**source_ref, "line": line_no, "row_sha256": sha(line.encode("utf-8"))}
            job = Path(raw["job_dir"]).resolve() if raw.get("job_dir") else None
            original, error = canonical_outcome(raw)
            package = {"path": str(package_path), "sha256": digest}
            if digest != pairs[task]["v6_package_digest"]:
                if not (arm == "B" and error and error.startswith("skipped_by_budget")
                        and original is None and (job is None or not _trials(job))):
                    raise ValueError("non-frozen source package is not an explicit unstarted diagnostic")
                non_native.append({"arm": arm, "task_id": task, "attempt_index": attempt,
                                   "population": "diagnostic", "reason": error,
                                   "canonical_source_ref": source, "source_package": package})
                continue
            cell = (arm, task, attempt)
            if cell in seen_cells:
                raise ValueError(f"duplicate official source cell: {cell}")
            seen_cells.add(cell)
            scheduled[arm].add(task)
            if arm in ("B", "C"):
                expected_name = (f"rollout-mimo-v2-6-distill-qwen-9b-rds1-{arm.lower()}-"
                                 f"{tasks.index(task):03d}-{bare_sha(digest)[:12]}"
                                 + (f"-a{attempt}" if arm == "B" else ""))
                if job is None or name != expected_name or job.name != name:
                    raise ValueError("batch name/index/package/attempt binding changed")
            trials = _trials(job) if job is not None else []
            recorded_trial = raw.get("native_trial_path")
            if recorded_trial is not None and trials != [Path(recorded_trial).resolve(strict=True)]:
                raise ValueError("canonical native-trial pointer disagrees with retained native job")
            if not trials:
                if original is not None:
                    raise ValueError("canonical measured source has no native trial")
                non_native.append({"arm": arm, "task_id": task, "attempt_index": attempt,
                                   "population": "official", "reason": error or "no_native_trial",
                                   "canonical_source_ref": source,
                                   "native_job_dir": str(job) if job is not None else None})
                continue
            if job is None or len(trials) != 1:
                raise ValueError("official source job does not have exactly one native trial")
            row = _native_row(trial=trials[0], job=job, arm=arm, task=task, attempt=attempt,
                              population="official", original=original, error=error, source=source,
                              package=package, expected_result=raw.get("native_result_source"))
            uid = tuple(row["source_uid"])
            if uid in seen_uids:
                raise ValueError("duplicate native source identity in canonical inputs")
            seen_uids.add(uid)
            rows.append(row)
    for binding in config.get("quarantined_jobs", []):
        job = Path(binding["path"]).resolve(strict=True)
        locked_trials = json_object(job / "lock.json")["trials"]
        if len(locked_trials) != 1:
            raise ValueError("quarantined source lacks a unique locked native task")
        locked_harbor = locked_trials[0]["task"]["digest"]
        for trial in _trials(job):
            task = canonical_task(json_object(trial / "result.json")["task_name"])
            if task not in tasks:
                raise ValueError("quarantined native task is outside the frozen33")
            digest = pairs[task]["v6_package_digest"]
            matches = sorted(path for path, identity in packages.items() if identity == (task, digest))
            if not matches:
                raise ValueError("quarantined source has no retained full frozen package")
            task_path = Path(matches[0])
            if harbor_task_digest(task_path) != locked_harbor:
                raise ValueError("quarantined native locked Harbor bytes do not match the frozen package")
            source = {"path": str(job), "disposition": binding["reason"],
                      "native_locked_harbor_digest": locked_harbor,
                      "package_binding": "native_canonical_task_and_locked_harbor_to_full_frozen_package",
                      "limitation": "Absent native full-package metadata is not reconstructed or claimed."}
            row = _native_row(trial=trial, job=job, arm="B", task=task, attempt=1,
                              population="quarantined", original=None, error=binding["reason"],
                              source=source, package={"path": str(task_path), "sha256": digest})
            uid = tuple(row["source_uid"])
            if uid in seen_uids:
                raise ValueError("quarantined root aliases an already inventoried native attempt")
            seen_uids.add(uid)
            rows.append(row)
    if config.get("native_alias_index"):
        alias_data, alias_ref = bound_bytes(config["native_alias_index"])
        sources["native_alias_index"] = alias_ref
        by_job: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_job.setdefault(row["source_uid"][0], []).append(row)
        for line_no, line in enumerate(alias_data.decode("utf-8").splitlines(), 1):
            alias = json.loads(line)
            for row in by_job.get(alias.get("native_job_id"), []):
                row["aliases"].append({"path": alias["path"], "recorded_result": alias.get("result"),
                                       "index_source": {**alias_ref, "line": line_no},
                                       "status": "historical_index_binding_not_rehashed"})
    summary = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm and row["population"] == "official"]
        summary[arm] = {
            "recorded_schedule_cells": sum(cell[0] == arm for cell in seen_cells),
            "native_attempts": len(selected), "kept_diffs": sum(row["diff"] is not None for row in selected),
            "empty_kept_diffs": sum(row["diff"] is not None and row["diff"]["size_bytes"] == 0 for row in selected),
            "v6_measured": sum(row["v6_outcome"] is not None for row in selected),
            "v6_passes": sum(row["v6_outcome"] is True for row in selected),
            "canonical_unmeasured_with_diff": sum(row["v6_outcome"] is None and row["diff"] is not None for row in selected),
            "native_missing_diff": sum(row["diff"] is None for row in selected),
        }
    return {
        "schema": "heldout-v7.inventory/v1", "created": datetime.now(UTC).isoformat(),
        "ledger_tag": "2026-10-17-v7", "frozen_task_ids": tasks, "authorized_attempts": ATTEMPTS,
        "sources": sources, "rows": rows, "non_native_cells": non_native,
        "unscheduled_tasks_by_arm": {arm: sorted(set(tasks) - scheduled[arm]) for arm in ARMS},
        "summary": summary,
        "quarantined_summary": {"native_attempts": sum(row["population"] == "quarantined" for row in rows),
                                "kept_diffs": sum(row["population"] == "quarantined" and row["diff"] is not None for row in rows)},
        "native_cloud_operations_started": 0, "scoring_allowed": False,
    }


def create_json(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    with path.open("xb") as output:
        output.write(data)
    return file_ref(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("inventory", help="Bind all canonical/native source attempts without cloud work")
    prepare.add_argument("--inputs", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    run = commands.add_parser("run", help="Replay all retained attempts on an independently accepted exact v7 release")
    run.add_argument("--inventory", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--accepted-sha", required=True, help="Exact manifest SHA independently accepted by the Verifier")
    run.add_argument("--verifier-receipt", type=Path, required=True)
    run.add_argument("--rate-evidence", type=Path, required=True)
    run.add_argument("--usd-per-execution", type=float, required=True, help="Explicit native reservation bound from the retained calibration evidence")
    run.add_argument("--work", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True, help="New report/labels output directory")
    run.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            value = inventory(args.inputs)
            output = create_json(args.out, value)
            result = {"output": output, "summary": value["summary"], "quarantined": value["quarantined_summary"],
                      "scoring_allowed": False}
        else:
            campaign = runpy.run_path(str(Path(__file__).with_name("campaign.py")))
            result = campaign["execute"](
                inventory_path=args.inventory, manifest_path=args.manifest, accepted_sha=args.accepted_sha,
                verifier_receipt=args.verifier_receipt, rate_evidence=args.rate_evidence,
                usd_per_execution=args.usd_per_execution, work=args.work, output=args.out,
                workers=args.workers, repo_root=Path(__file__).resolve().parents[3],
            )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"heldout-v7: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
