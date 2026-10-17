"""Inventory the 567 retained MiMo reference tasks against exact v6 bytes.

This is a control-qualification census, not a weak-test or semantic-validity
verdict. Historical package digests never qualify a rebuilt package. Native
execution UUIDs deduplicate receipt copies; setup failures remain unscored.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from evallab.mimo_census import job_has_trial_errors, trial_tests_executed
from evallab.mimo_clean import build_solution_sh
from evallab.registry import harbor_task_digest, task_directory_digest

FLEET_LABEL = "oracle:pass+nop:fail"
FLEET_SIZE = 567
COLUMNS = (
    "task_id", "language", "bucket", "reason", "reference_label",
    "reference_patch_path", "reference_patch_sha256", "reference_bytes_match",
    "run_digest", "v6_package_digest", "v6_harbor_digest", "package_path",
    "package_bytes_match", "reference_solution_match", "oracle_execution_ids",
    "nop_execution_ids", "oracle_rewards", "nop_rewards", "reported_issues", "native_evidence",
)


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def native_observations(metadata_paths: set[Path]) -> list[dict[str, Any]]:
    """Read native identity, rewards, errors and case execution, not receipt labels."""
    observations: dict[str, dict[str, Any]] = {}
    for metadata_path in sorted(metadata_paths):
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_bytes())
        job = metadata_path.parent
        legacy = metadata_path.name == "stability-manifest.json"
        job_result_path = job / "result.json"
        job_result = json.loads(job_result_path.read_bytes()) if legacy and job_result_path.is_file() else {}
        process_ok = (
            bool(job_result.get("finished_at"))
            and (job_result.get("stats") or {}).get("n_completed_trials", 0) > 0
            and not job_has_trial_errors(job)
        ) if legacy else metadata.get("exit_code") == 0 and metadata.get("timed_out") is False
        for result_path in sorted(job.glob("*/result.json")):
            if legacy:
                entry = metadata.get("trials", {}).get(result_path.parent.name, {})
                source_slug = Path(entry.get("source", "")).parent.name
                staging = {
                    "declared_task_name": source_slug.removeprefix("mimo-v2.6-rl__"),
                    "source_package_digest": entry.get("task_version_digest"),
                    "source_harbor_digest": entry.get("harbor_digest"),
                }
            else:
                staging = metadata.get("task_staging") or {}
            digest = staging.get("source_package_digest")
            task = str(staging.get("declared_task_name", "")).removeprefix("mimo-v2.6-rl/")
            if not digest or not task.startswith("format-code-task-"):
                continue
            native = json.loads(result_path.read_bytes())
            agent = (native.get("config", {}).get("agent") or {}).get("name")
            if agent not in ("oracle", "nop") or not native.get("id"):
                continue
            trial = result_path.parent
            stability_path = trial / "verifier/stability.json"
            if stability_path.is_file():
                runs = json.loads(stability_path.read_bytes()).get("runs", [])
                rewards = [run.get("reward") for run in runs]
                errors = [run.get("error") for run in runs]
                executed = [
                    trial_tests_executed(trial / "verifier/repeat" / str(index))
                    for index in range(len(runs))
                ]
            else:
                reward_map = (native.get("verifier_result") or {}).get("rewards") or {}
                rewards = [reward_map.get("reward")]
                errors = [None]
                executed = [trial_tests_executed(trial)]
            scored = (
                bool(rewards)
                and all(type(reward) in (int, float) and reward in (0, 1) for reward in rewards)
                and all(executed)
                and all(error is None for error in errors)
                and native.get("exception_info") is None
                and process_ok
                and bool(staging.get("source_harbor_digest"))
                and (legacy or staging.get("staged_harbor_digest") == staging["source_harbor_digest"])
            )
            observation = {
                "execution_id": native["id"], "task_id": task, "agent": agent,
                "source_package_digest": digest,
                "source_harbor_digest": staging.get("source_harbor_digest"),
                "staged_harbor_digest": staging.get("staged_harbor_digest"),
                "native_task_checksum": native.get("task_checksum"),
                "rewards": rewards, "test_execution": executed, "run_errors": errors,
                "exception_type": (native.get("exception_info") or {}).get("exception_type"),
                "exception_info_sha256": (
                    hashlib.sha256(compact(native["exception_info"]).encode()).hexdigest()
                    if native.get("exception_info") is not None else None
                ),
                "returncode": metadata.get("exit_code"), "scored": scored,
                "timed_out": metadata.get("timed_out"),
                "identity_kind": "stability_manifest" if legacy else "native_executor_staging",
                "job_result_sha256": file_sha256(job_result_path) if legacy and job_result_path.is_file() else None,
                "metadata_path": str(metadata_path), "metadata_sha256": file_sha256(metadata_path),
                "result_path": str(result_path), "result_sha256": file_sha256(result_path),
            }
            previous = observations.get(native["id"])
            if previous is not None:
                identity = ("task_id", "agent", "source_package_digest", "rewards", "scored")
                if any(previous[key] != observation[key] for key in identity):
                    raise ValueError(f"conflicting copies of native execution {native['id']}")
                continue
            observations[native["id"]] = observation
    return sorted(observations.values(), key=lambda item: item["execution_id"])


def census_row(
    reference: dict[str, str], manifest: dict[str, str] | None,
    *, repo: Path, package_root: Path, observations: list[dict[str, Any]],
    reported_issues: list[dict[str, Any]],
) -> dict[str, str]:
    task = reference["task_id"]
    build = manifest or {}
    patch = resolve_path(repo, reference.get("patch_path", ""))
    reference_ok = patch.is_file() and file_sha256(patch) == reference.get("patch_sha256", "").removeprefix("sha256:")
    package = resolve_path(package_root, build.get("package_path", ""))
    package_ok = bool(build.get("final_digest")) and package.is_dir() and task_directory_digest(package) == build["final_digest"]
    harbor_digest = harbor_task_digest(package) if package_ok else ""
    solution_ok = False
    if reference_ok and package_ok:
        solution = package / "solution/solve.sh"
        expected = build_solution_sh(
            task_id=task, label=reference["label"], fix_commit=reference["fix_commit"],
            patch_bytes=patch.read_bytes(),
        )
        solution_ok = solution.is_file() and solution.read_bytes() == expected
    exact = [
        observation for observation in observations
        if observation["task_id"] == task
        and observation["source_package_digest"] == build.get("final_digest")
        and observation["source_harbor_digest"] == harbor_digest
    ]
    controls = {agent: [item for item in exact if item["agent"] == agent] for agent in ("oracle", "nop")}
    good = {
        agent: [item for item in controls[agent] if item["scored"] and all(reward == expected for reward in item["rewards"])]
        for agent, expected in (("oracle", 1), ("nop", 0))
    }
    wrong = any(
        item["scored"] and any(reward != expected for reward in item["rewards"])
        for agent, expected in (("oracle", 1), ("nop", 0)) for item in controls[agent]
    )
    if not reference_ok:
        bucket, reason = "missing_ref", "indexed_reference_bytes_missing_or_hash_mismatch"
    elif build.get("status") != "built" or not package_ok:
        bucket, reason = "broken_ref_or_env", "v6_package_missing_or_digest_mismatch"
    elif not solution_ok:
        bucket, reason = "broken_ref_or_env", "embedded_reference_differs_from_index"
    elif wrong:
        bucket, reason = "broken_ref_or_env", "exact_input_control_disagrees_with_reference_1_nop_0"
    elif good["oracle"] and good["nop"]:
        bucket, reason = "v6_verified", "exact_input_reference_1_nop_0_tests_executed"
    elif any(
        issue["reference_patch_sha256"] == reference.get("patch_sha256", "")
        for issue in reported_issues
    ):
        bucket, reason = "broken_ref_or_env", "accepted_reported_reference_failure_not_superseded_by_exact_controls"
    elif any(
        item["exception_info_sha256"] is not None
        or item["returncode"] not in (0, None)
        or item["timed_out"] is True
        or not item["rewards"]
        or any(reward is None for reward in item["rewards"])
        or any(error is not None for error in item["run_errors"])
        for item in exact
    ):
        bucket, reason = "broken_ref_or_env", "exact_input_control_setup_or_execution_failed"
    elif exact:
        bucket, reason = "v6_unverified", "exact_input_control_test_execution_or_pair_unproven"
    else:
        bucket, reason = "v6_unverified", "exact_input_reference_nop_pair_not_retained"
    return {
        "task_id": task, "language": build.get("language", ""), "bucket": bucket,
        "reason": reason, "reference_label": reference["label"],
        "reference_patch_path": reference.get("patch_path", ""),
        "reference_patch_sha256": reference.get("patch_sha256", ""),
        "reference_bytes_match": str(reference_ok).lower(), "run_digest": build.get("run_digest", ""),
        "v6_package_digest": build.get("final_digest", ""), "v6_harbor_digest": harbor_digest,
        "package_path": build.get("package_path", ""), "package_bytes_match": str(package_ok).lower(),
        "reference_solution_match": str(solution_ok).lower(),
        "reported_issues": compact(reported_issues),
        "oracle_execution_ids": compact([item["execution_id"] for item in controls["oracle"]]),
        "nop_execution_ids": compact([item["execution_id"] for item in controls["nop"]]),
        "oracle_rewards": compact([item["rewards"] for item in controls["oracle"]]),
        "nop_rewards": compact([item["rewards"] for item in controls["nop"]]),
        "native_evidence": compact(exact),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference-index", type=Path)
    parser.add_argument("--reported-issues", type=Path, default=Path(__file__).with_name("reported-issues.json"))
    parser.add_argument("--native-controls", type=Path, action="append", default=[])
    parser.add_argument("--jobs-root", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference_path = args.reference_index or args.repo_root / "research/experiments/mimo-reference-fixes/index.csv"
    references = sorted((row for row in csv_rows(reference_path) if row["label"] == FLEET_LABEL), key=lambda row: row["task_id"])
    if len(references) != FLEET_SIZE or len({row["task_id"] for row in references}) != FLEET_SIZE:
        raise ValueError("canonical reference predicate must resolve exactly 567 unique tasks")
    builds = {row["task_id"]: row for row in csv_rows(args.manifest)}
    issues = json.loads(args.reported_issues.read_bytes())["issues"]
    for issue in issues:
        source = resolve_path(args.repo_root, issue["source"])
        if file_sha256(source) != issue["source_sha256"]:
            raise ValueError(f"reported reference-failure source changed: {source}")
    metadata_paths: set[Path] = set()
    for source in args.native_controls:
        document = json.loads(source.read_bytes())
        for observation in document["observations"]:
            binding = observation["native_metadata"]
            path = Path(binding["path"])
            if path.is_file() and file_sha256(path) != binding["sha256"]:
                raise ValueError(f"native metadata changed since retained receipt: {path}")
            metadata_paths.add(path)
    for root in args.jobs_root:
        metadata_paths.update(root.glob("*/lab-metadata.json"))
        metadata_paths.update(root.glob("*/stability-manifest.json"))
    observations = native_observations(metadata_paths)
    rows = [
        census_row(
            reference, builds.get(reference["task_id"]), repo=args.repo_root,
            package_root=args.package_root, observations=observations,
            reported_issues=[issue for issue in issues if issue["task_id"] == reference["task_id"]],
        )
        for reference in references
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(compact({
        "tasks": len(rows), "buckets": dict(sorted(Counter(row["bucket"] for row in rows).items())),
        "native_execution_ids": len(observations), "output": str(args.output),
        "sha256": file_sha256(args.output), "membership_index_sha256": file_sha256(reference_path),
        "manifest_sha256": file_sha256(args.manifest),
        "scope": "reference/nop controls only; not semantic validity or clean-set ladder acceptance",
    }))


if __name__ == "__main__":
    main()
