#!/usr/bin/env python3
"""Prepare and execute bounded surveys using the existing ref-subset method.

No mutation, minimization or grading implementation lives here. Native shared
Harbor tasks use evallab.ref_subset.probe_task unchanged, including the host-wide
two-slot Docker gate and original task resource/timeout declarations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from evallab.ref_subset import probe_task
from evallab.registry import compute_task_digests

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
RUNS = REPO / "runs" / "weak-tests-generalization-20261019"
SEED = "weak-tests-generalization-20261019:hef-size-strata-v1"
TB_TARGETS = {
    "cancel-async-tasks": ["/app/run.py"],
    "multi-source-data-merger": ["/app/merged_users.parquet", "/app/conflicts.json"],
}
TRACES_TASKS = (
    "cobol-modernization", "custom-memory-heap-crash", "extract-elf", "fix-code-vulnerability",
    "fix-ocaml-gc", "headless-terminal", "make-mips-interpreter", "model-extraction-relu-logits",
    "modernize-scientific-stack", "openssl-selfsigned-cert", "path-tracing", "path-tracing-reverse",
    "polyglot-c-py", "polyglot-rust-c", "raman-fitting", "sam-cell-seg", "sanitize-git-repo",
    "sqlite-db-truncate", "video-processing", "regex-chess",
)


def dump(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def materialize(frame: list[dict], selected: set[str]) -> None:
    """Recreate selected packages from frozen digests, including tasks outside the paired key."""
    materializer_path = REPO / "research/experiments/tb2-to-tb21-v1/materialize.py"
    spec = importlib.util.spec_from_file_location("tb21_materialize", materializer_path)
    assert spec is not None and spec.loader is not None
    materializer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = materializer
    spec.loader.exec_module(materializer)
    items = [
        {
            "item_id": "native-survey:" + row["task_id"],
            "subject": {
                "task_id": "tb21/" + row["task_id"],
                "task_digest": row["package_digest"],
            },
        }
        for row in frame if row["task_id"] in selected
    ]
    if len(items) != len(selected):
        raise ValueError("the frozen TB2.1 frame must cover every selected task")
    report = materializer.materialize_packages(
        items, RUNS, pins={"tb21": materializer.PINS["tb21"]}, repo_root=REPO,
    )
    if report["mismatches"] or report["unmaterialized"]:
        raise ValueError("the selected original TB2.1 packages did not materialize exactly")


def prepare(prior: Path) -> None:
    """Freeze selection before observing any new grades; reuse pinned materialization."""
    destination = HERE / "cohort.json"
    if destination.exists():
        raise ValueError("selection already frozen; do not overwrite it")
    old_cohort = prior / "research/answer-keys/tb21-weak-tests-v1/cohort.json"
    frozen = json.loads(old_cohort.read_text())
    materialize(frozen["tasks"], set(TB_TARGETS))
    tb_frame = []
    for row in frozen["tasks"]:
        tb_frame.append({
            key: row[key]
            for key in ("task_id", "package_digest", "reference_sha256", "memory_mb", "gpus", "compose")
        } | {
            "selection": "auditor" if row["task_id"] in TB_TARGETS else
                         "traces_campaign" if row["task_id"] in TRACES_TASKS else "not_selected",
        })
    hef_root = REPO / "library/benchmarks/humanevalfix/humanevalfix"
    hef_frame = []
    for package in sorted(hef_root.iterdir()):
        if not (package / "task.toml").is_file():
            continue
        files = list((package / "environment/workspace").glob("Python__*.py"))
        if len(files) != 1 or not (package / "solution/solve.sh").is_file():
            raise ValueError(f"unexpected HumanEvalFix package shape: {package}")
        config = tomllib.loads((package / "task.toml").read_text())
        hef_frame.append({
            "task_id": package.name,
            "package": str(package.relative_to(REPO)),
            "package_digest": compute_task_digests(package).package,
            "base_source_bytes": files[0].stat().st_size,
            "targets": ["/workspace/" + files[0].name],
            "timeout_sec": config["agent"]["timeout_sec"],
        })
    if len(hef_frame) != 164:
        raise ValueError("the pinned HumanEvalFix frame must contain 164 reference-bearing tasks")
    by_size = sorted(hef_frame, key=lambda row: (row["base_source_bytes"], row["task_id"]))
    for index, row in enumerate(by_size):
        row["stratum"] = "source_size_q" + str(index // 41 + 1)
        row["selection_rank"] = hashlib.sha256((SEED + ":" + row["task_id"]).encode()).hexdigest()
        row["selected"] = False
    for quartile in range(1, 5):
        group = [row for row in by_size if row["stratum"] == f"source_size_q{quartile}"]
        for row in sorted(group, key=lambda row: row["selection_rank"])[:3]:
            row["selected"] = True
    plan = {
        "schema": "evallab.weak_tests_generalization.cohort/v1",
        "prepared_at": datetime.now(UTC).isoformat(),
        "method": "evallab.ref_subset.probe_task / Probe / mutation_pass / verifier_mutation.select_mutants",
        "method_code_sha256": hashlib.sha256((REPO / "src/evallab/ref_subset.py").read_bytes()).hexdigest(),
        "limits": {"subset_grades": 24, "line_mutants": 8, "owned_lanes": 1, "shared_docker_slots": 2},
        "paid_experiment_spend_usd": 0,
        "tb21": {
            "source": frozen["source"],
            "frame_n": 89,
            "frame": tb_frame,
            "prior_cohort_sha256": hashlib.sha256(old_cohort.read_bytes()).hexdigest(),
            "prior_worktree": str(prior),
            "prior_live_campaign": TRACES_TASKS,
            "own_sampling": "Purposive two-stratum pilot: one executable Python submission and one structured-data submission, disjoint from the 20 live Traces tasks. Not a random population estimate.",
            "selected": [
                {"task_id": name, "package": str((RUNS / "packages/tb21" / name).relative_to(REPO)), "targets": targets}
                for name, targets in TB_TARGETS.items()
            ],
        },
        "humanevalfix": {
            "source": {"registry": "humanevalfix@1.0", "repo": "laude-institute/harbor-datasets", "commit": "ab02ff13250fae8d91b93a6e4c11ce0bdcb78215"},
            "frame_n": 164,
            "selection_seed": SEED,
            "sampling": "Four equal strata of 41 tasks by buggy source byte length (task name breaks ties); take the three smallest seeded SHA-256 ranks in each stratum. All tasks have equal 3/41 inclusion probability under the recorded hash-rank design.",
            "selection_reason": "Largest already-materialized homogeneous lightweight Python code-repair registry benchmark: 164 reference-bearing CPU tasks with one editable Python file, common Python 3.12 image, 1 CPU/2 GiB and 600-second task deadlines. SWE-bench pools need large per-task images; math/MCQ benchmarks do not exercise reference-code mutation. This is not an exhaustive claim that no other public registry benchmark has more eligible tasks.",
            "frame": sorted(hef_frame, key=lambda row: row["task_id"]),
        },
    }
    dump(destination, plan)
    print(json.dumps({"tb21_selected": list(TB_TARGETS), "hef_selected": [r["task_id"] for r in hef_frame if r["selected"]]}), flush=True)


def run() -> None:
    plan = json.loads((HERE / "cohort.json").read_text())
    os.environ["EVALLAB_DERIVED_ROOT"] = str(REPO / "derived/parquet")
    os.environ["EVALLAB_DOCKER_LOCK_SLOTS"] = "2"
    results = []
    jobs = [("tb21", row) for row in plan["tb21"]["selected"]]
    jobs += [("humanevalfix", row) for row in plan["humanevalfix"]["frame"] if row["selected"]]
    for benchmark, row in jobs:
        package = REPO / row["package"]
        out = RUNS / benchmark / row["task_id"]
        if (out / "report.json").is_file():
            raise ValueError(f"refusing to overwrite completed trial evidence: {out}")
        report = probe_task(
            package, None, out, max_grades=24, mutants=8, targets=row["targets"],
            mutation_seed="tb21-weak-tests-v1" if benchmark == "tb21" else SEED,
            repo_root=REPO,
        )
        result = {"benchmark": benchmark, "task_id": row["task_id"], "status": report["status"], "verdict": report["verdict"]}
        results.append(result)
        dump(RUNS / "completed.json", results)
        print(json.dumps(result), flush=True)
    dump(RUNS / "completion.json", {"finished_at": datetime.now(UTC).isoformat(), "results": results})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "materialize", "run"))
    parser.add_argument("--prior-worktree", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.prior_worktree is None:
            parser.error("prepare requires --prior-worktree")
        prepare(args.prior_worktree)
    elif args.command == "materialize":
        plan = json.loads((HERE / "cohort.json").read_text())
        materialize(plan["tb21"]["frame"], {row["task_id"] for row in plan["tb21"]["selected"]})
    else:
        run()


if __name__ == "__main__":
    main()
