#!/usr/bin/env python3
"""HAR-179: same-failing-test evidence, never an automatic task verdict.

Read the frozen task_history.csv paths and HAR-140/HAR-146 locked nop
publications. Missing verifier reports or instructions remain unknown. The
original history count is retained separately from unique physical trials.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from evallab import probe03
from evallab.registry import task_directory_digest
from evallab.storage.paths import shared_checkout_root

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RESULTS = Path.home() / "Developer/eval-lab-results"
TASK_RE = re.compile(r"format-code-task-\d{6}")
NOP_JOB_RE = re.compile(r"(?:HAR-(?:140|146)-|(?:^|-)har(?:140|146)-)")
SNAPSHOT_TASKS = Path("derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks")
COLUMNS = (
    "task_id",
    "runs",
    "unique_runs",
    "models",
    "model_names",
    "unknown_model_runs",
    "ctrf_runs",
    "verifier_runs",
    "model_evidence_state",
    "always_failing_tests",
    "observed_common_failing_tests",
    "no_agent_runs",
    "no_agent_ctrf_runs",
    "no_agent_verifier_runs",
    "verifier_sources",
    "test_evidence",
    "candidate_broken_test",
    "candidate_tests",
    "model_trial_paths",
    "no_agent_trial_paths",
    "notes",
)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned_instruction(
    task_id: str,
    digest: str | None,
    snapshot: Path,
    repo_root: Path,
    cache: dict,
) -> tuple[str | None, str | None]:
    """Recover an instruction only through an unchanged, digest-bound lineage."""
    key = (task_id, digest)
    if key in cache:
        return cache[key]
    package = snapshot / task_id
    result = (None, None)
    try:
        original = task_directory_digest(package)
        current, seen = digest, set()
        while current and current != original and current not in seen:
            seen.add(current)
            short = current.removeprefix("sha256:")[:12]
            record = load_json(
                repo_root / "library/task-variants" / f"mimo-v2.6-rl__{task_id}" / f"{short}.json"
            )
            files = record.get("files")
            if (
                record.get("variant_digest") != current
                or record.get("task_name") != f"mimo-v2.6-rl/{task_id}"
                or not isinstance(files, list)
                or any(
                    not isinstance(item, dict) or item.get("path") == "instruction.md"
                    for item in files
                )
                or "instruction" in record.get("components_changed", [])
            ):
                break
            current = (record.get("parent") or {}).get("digest")
        if current == original:
            path = package / "instruction.md"
            result = (path.read_text(encoding="utf-8"), str(path))
    except (OSError, ValueError):
        pass
    cache[key] = result
    return result


def instruction(
    trial: Path,
    config: dict,
    result: dict,
    task_id: str,
    snapshot: Path,
    repo_root: Path,
    cache: dict,
) -> tuple[str | None, str | None]:
    """Use recorded paths or verified ancestors, never the current ledger variant."""
    lab = load_json(trial.parent / "lab-metadata.json")
    provenance = load_json(trial.parent / "provenance.json")
    paths = []
    for source in (config, result.get("config") or {}):
        value = (source.get("task") or {}).get("path")
        if isinstance(value, str) and Path(value).is_absolute():
            paths.append(Path(value) / "instruction.md")
    roots = []
    for source in (provenance, lab):
        value = (source.get("repository") or {}).get("worktree")
        if isinstance(value, str) and value:
            roots.append(Path(value))
    declared = [(lab.get("experiment") or {}).get("task_path")]
    declared += [
        item.get("task_path") for item in provenance.get("tasks", []) if isinstance(item, dict)
    ]
    for value in declared:
        if isinstance(value, str) and value:
            task = Path(value)
            paths.extend(
                [task / "instruction.md"]
                if task.is_absolute()
                else [root / task / "instruction.md" for root in roots]
            )
    for path in dict.fromkeys(paths):
        try:
            return path.read_text(encoding="utf-8"), str(path)
        except (OSError, UnicodeError):
            continue
    text, _size = probe03._instruction_text(trial)
    if text is not None:
        return text, f"probe03._instruction_text:{trial}"
    digest = (lab.get("task_staging") or {}).get("source_package_digest")
    digest = digest or (lab.get("experiment") or {}).get("package_digest")
    if not digest:
        digest = next(
            (
                item.get("package_digest")
                for item in provenance.get("tasks", [])
                if isinstance(item, dict) and item.get("task_id") == task_id
            ),
            None,
        )
    return pinned_instruction(task_id, digest, snapshot, repo_root, cache)


def test_key(test: dict) -> str:
    name = test["name"]
    file = test.get("filePath")
    return f"{file}::{name}" if isinstance(file, str) and file and "::" not in name else name


def load_trial(
    path: Path,
    relative: str,
    task_id: str,
    snapshot: Path,
    repo_root: Path,
    cache: dict,
    *,
    include_instruction: bool = True,
) -> dict:
    config, result = load_json(path / "config.json"), load_json(path / "result.json")
    agent = config.get("agent") or (result.get("config") or {}).get("agent") or {}
    model = agent.get("model_name")
    model = model if isinstance(model, str) and model else None
    identity = result.get("id")
    identity = identity if isinstance(identity, str) and identity else str(path.resolve())
    text, instruction_path = (
        instruction(path, config, result, task_id, snapshot, repo_root, cache)
        if include_instruction
        else (None, None)
    )
    passage = probe03._verifier_passage(path, failing_test_limit=None, include_test_records=True)
    tests = passage["test_records"]
    source = passage["test_records_source"]
    valid = passage["test_records_complete"]
    notes = []
    task_names = set(
        TASK_RE.findall(
            json.dumps({"task_name": result.get("task_name"), "task": config.get("task")})
        )
    )
    if task_names and task_id not in task_names:
        valid = False
        notes.append(f"task_identity_mismatch:{relative}")
    if not valid:
        notes.append(f"incomplete_verifier_evidence:{relative}")
    by_name = defaultdict(list)
    if valid:
        for test in tests:
            by_name[test_key(test)].append(test)
        # Setup/collection errors and skips are not assertion failures.
        failed = {
            name: entries
            for name, entries in by_name.items()
            if all(test["status"] == "failed" for test in entries)
        }
    else:
        failed = {}
    assertions = {
        name: [
            evidence
            for test in entries
            for evidence in probe03.verifier_asserted_literals(test, text)
        ]
        for name, entries in failed.items()
    }
    signature = json.dumps(tests, sort_keys=True)
    return {
        "identity": identity,
        "paths": [relative],
        "model": model,
        "valid": valid,
        "failed": failed,
        "reported": set(by_name),
        "assertions": assertions,
        "verifier_source": source,
        "instruction_path": instruction_path,
        "instruction_sha256": hashlib.sha256(text.encode()).hexdigest()
        if text is not None
        else None,
        "instruction_text": text,
        "signature": signature,
        "notes": notes,
    }


def unique_trials(trials: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for trial in trials:
        groups[trial["identity"]].append(trial)
    unique = []
    for group in groups.values():
        best = max(group, key=lambda item: (item["valid"], item["instruction_text"] is not None))
        merged = {
            **best,
            "paths": [path for item in group for path in item["paths"]],
            "notes": [note for item in group for note in item["notes"]],
        }
        models = {item["model"] for item in group if item["model"] is not None}
        instructions = {item["instruction_sha256"] for item in group if item["instruction_sha256"]}
        reports = {item["signature"] for item in group if item["valid"]}
        if len(models) > 1 or len(instructions) > 1 or len(reports) > 1:
            merged["valid"] = False
            merged["failed"] = {}
            merged["assertions"] = {}
            merged["notes"].append(f"conflicting_publications:{best['identity']}")
        else:
            merged["model"] = next(iter(models), None)
            known = next((item for item in group if item["instruction_text"] is not None), None)
            if known is not None and merged["instruction_text"] is None:
                for key in ("instruction_text", "instruction_path", "instruction_sha256"):
                    merged[key] = known[key]
                merged["assertions"] = {
                    name: [
                        evidence
                        for test in entries
                        for evidence in probe03.verifier_asserted_literals(
                            test, merged["instruction_text"]
                        )
                    ]
                    for name, entries in merged["failed"].items()
                }
        unique.append(merged)
    return unique


def discover_nops(
    results: Path,
    task_ids: set[str],
    snapshot: Path,
    repo_root: Path,
    cache: dict,
) -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for config_path in sorted(results.glob("*/*/*/config.json")):
        trial = config_path.parent
        if not NOP_JOB_RE.search(trial.parent.name):
            continue
        config, result = load_json(config_path), load_json(trial / "result.json")
        agent = config.get("agent") or (result.get("config") or {}).get("agent") or {}
        lock = load_json(trial / "egress-lock.json")
        if (
            agent.get("name") != "nop"
            or lock.get("applied") is not True
            or lock.get("network_block_all") is not True
            or not (trial / "verifier").is_dir()
        ):
            continue
        names = set(
            TASK_RE.findall(
                json.dumps({"task_name": result.get("task_name"), "task": config.get("task")})
            )
        )
        if len(names) == 1 and (task_id := next(iter(names))) in task_ids:
            grouped[task_id].append(
                load_trial(
                    trial,
                    str(trial.relative_to(results)),
                    task_id,
                    snapshot,
                    repo_root,
                    cache,
                    include_instruction=False,
                )
            )
    return {task: unique_trials(trials) for task, trials in grouped.items()}


def common_failures(trials: list[dict]) -> set[str]:
    return set.intersection(*(set(trial["failed"]) for trial in trials)) if trials else set()


def task_row(history: dict, trials: list[dict], nops: list[dict]) -> dict:
    observed = [trial for trial in trials if trial["valid"]]
    complete = bool(trials) and len(observed) == len(trials)
    common = common_failures(observed)
    always = common if complete else set()
    models = sorted({trial["model"] for trial in trials if trial["model"] is not None})
    test_evidence, candidates = [], []
    for name in sorted(common):
        assertions = []
        absent_sets = []
        for trial in observed:
            absent = set()
            for evidence in trial["assertions"].get(name, []):
                assertions.append(
                    {
                        **evidence,
                        "trial_paths": trial["paths"],
                        "instruction_path": trial["instruction_path"],
                        "instruction_sha256": trial["instruction_sha256"],
                    }
                )
                if evidence["in_instruction"] is False:
                    absent.add(evidence["literal"])
            absent_sets.append(absent)
        shared_absent = set.intersection(*absent_sets) if absent_sets else set()
        candidate = name in always and bool(shared_absent)
        if candidate:
            candidates.append(name)
        nop_statuses = [
            "unknown"
            if not nop["valid"] or name not in nop["reported"]
            else "yes"
            if name in nop["failed"]
            else "no"
            for nop in nops
        ]
        if not nop_statuses or "unknown" in nop_statuses:
            nop_fails = "unknown"
        elif len(set(nop_statuses)) > 1:
            nop_fails = "mixed"
        else:
            nop_fails = nop_statuses[0]
        test_evidence.append(
            {
                "test": name,
                "failed_model_runs": len(observed),
                "every_model_run": name in always,
                "no_agent_fails": nop_fails,
                "no_agent_failed_runs": nop_statuses.count("yes"),
                "no_agent_unknown_runs": nop_statuses.count("unknown"),
                "assertions": assertions,
                "shared_absent_literals": sorted(
                    shared_absent, key=lambda value: (type(value).__name__, str(value))
                ),
                "candidate": candidate,
            }
        )
    state = (
        "no_model_runs"
        if not trials
        else "complete"
        if complete
        else "partial"
        if observed
        else "missing"
    )
    return {
        "task_id": history["task_id"],
        "runs": int(history["runs"]),
        "unique_runs": len(trials),
        "models": len(models),
        "model_names": models,
        "unknown_model_runs": sum(trial["model"] is None for trial in trials),
        "ctrf_runs": sum(trial["verifier_source"] == "ctrf" for trial in observed),
        "verifier_runs": len(observed),
        "model_evidence_state": state,
        "always_failing_tests": sorted(always),
        "observed_common_failing_tests": sorted(common),
        "no_agent_runs": len(nops),
        "no_agent_ctrf_runs": sum(
            nop["valid"] and nop["verifier_source"] == "ctrf" for nop in nops
        ),
        "no_agent_verifier_runs": sum(nop["valid"] for nop in nops),
        "verifier_sources": sorted({trial["verifier_source"] for trial in [*trials, *nops]}),
        "test_evidence": test_evidence,
        "candidate_broken_test": bool(candidates),
        "candidate_tests": candidates,
        "model_trial_paths": sorted(path for trial in trials for path in trial["paths"]),
        "no_agent_trial_paths": sorted(path for nop in nops for path in nop["paths"]),
        "notes": sorted({note for trial in [*trials, *nops] for note in trial["notes"]}),
    }


def analyze(
    history_path: Path,
    nop_manifest: Path,
    results: Path,
    repo_root: Path,
    snapshot_tasks: Path | None = None,
) -> dict:
    with history_path.open(newline="", encoding="utf-8") as handle:
        history = list(csv.DictReader(handle))
    with nop_manifest.open(newline="", encoding="utf-8") as handle:
        nop_tasks = {row["task_id"] for row in csv.DictReader(handle)}
    snapshot = snapshot_tasks if snapshot_tasks is not None else repo_root / SNAPSHOT_TASKS
    cache = {}
    nops = discover_nops(results, nop_tasks, snapshot, repo_root, cache)
    rows = []
    for item in history:
        paths = item["trials"].split()
        if len(paths) != int(item["runs"]):
            raise ValueError(f"History run/path count differs for {item['task_id']}")
        trials = unique_trials(
            [
                load_trial(results / path, path, item["task_id"], snapshot, repo_root, cache)
                for path in paths
            ]
        )
        rows.append(task_row(item, trials, nops.get(item["task_id"], [])))
    rows.sort(key=lambda row: row["task_id"])
    return {
        "rows": rows,
        "summary": {
            "history_sha256": sha256(history_path),
            "locked_nop_manifest_sha256": sha256(nop_manifest),
            "tasks": len(rows),
            "tasks_with_model_runs": sum(row["runs"] > 0 for row in rows),
            "history_run_entries": sum(row["runs"] for row in rows),
            "unique_model_runs": sum(row["unique_runs"] for row in rows),
            "model_ctrf_runs": sum(row["ctrf_runs"] for row in rows),
            "model_verifier_runs": sum(row["verifier_runs"] for row in rows),
            "locked_no_agent_runs": sum(row["no_agent_runs"] for row in rows),
            "locked_no_agent_ctrf_runs": sum(row["no_agent_ctrf_runs"] for row in rows),
            "locked_no_agent_verifier_runs": sum(row["no_agent_verifier_runs"] for row in rows),
            "locked_manifest_tasks": len(nop_tasks),
            "locked_tasks_without_retained_trial": sorted(nop_tasks - set(nops)),
            "candidate_tasks": [
                {
                    "task_id": row["task_id"],
                    "tests": row["candidate_tests"],
                    "trial_paths": row["model_trial_paths"],
                }
                for row in rows
                if row["candidate_broken_test"]
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=HERE / "task_history.csv")
    parser.add_argument(
        "--locked-nops",
        type=Path,
        default=ROOT / "research/experiments/har122-egress-lock/har146-locked-nop.csv",
    )
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument("--snapshot-tasks", type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "failing_tests.csv")
    args = parser.parse_args()
    snapshot = args.snapshot_tasks or shared_checkout_root(ROOT) / SNAPSHOT_TASKS
    result = analyze(args.history, args.locked_nops, args.results, ROOT, snapshot)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in result["rows"]:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in row.items()
                }
            )
    print(
        json.dumps(
            {**result["summary"], "output": str(args.output), "output_sha256": sha256(args.output)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
