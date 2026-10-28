#!/usr/bin/env python3
"""Census existing seeker pairs using sources only; never execute a grader/model."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import tomllib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any

from evallab.candidate_runner import run_candidate
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.seeker_worker import (
    find_cross_key_dev_assignments,
    find_cross_key_non_dev_assignments,
    require_cross_key_dev_only,
)
from evallab.registry import compute_subpath_digest, task_directory_digest
from evallab.upstream_grader import GraderRefusal, _reservation, _tree_digest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TIMEOUT_SECONDS = 600
DATASETS = ("breaker-bench-v1", "mimo-defect-map-v1", "tb2-to-tb21-v1", "tb4-audit-v1")


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def bare(task: str) -> str:
    return task.strip().split("@", 1)[0].rsplit("/", 1)[-1]


def aliases(task: str) -> set[str]:
    name = bare(task)
    return {name, name.split("__")[-1]} if "__" in name else {name}


def dev_gate(task: str, assignments: Path) -> str | None:
    """Reuse the worker's ledger-positive, bare-name, cross-key contract."""
    if find_cross_key_non_dev_assignments(task, assignments_csv=assignments):
        return "a_cross_key_non_dev"
    if not find_cross_key_dev_assignments(task, assignments_csv=assignments):
        return "a_no_affirmative_dev"
    require_cross_key_dev_only(task, assignments_csv=assignments, require_affirmative_dev=True)
    return None


@dataclass(frozen=True)
class Pair:
    original: str | None
    repaired: str | None
    kind: str = "package"
    original_expected: str | None = None
    repaired_expected: str | None = None


class Sources:
    """Merge source aliases without counting repeated models/versions as tasks."""

    def __init__(self, repo: Path, lab: Path) -> None:
        self.repo, self.lab = repo, lab
        self.pairs: dict[tuple[str, str], dict[Pair, set[str]]] = defaultdict(dict)
        self.inputs: dict[str, str] = {}
        self.coverage: dict[str, Any] = {}
        self.variants = (
            lab / "derived/task-store/variants",
            lab / ".worktrees/envcheck-heldout-v7-20261017/runs/envcheck-v7-20261017/task-store/variants",
            lab / ".worktrees/envcheck-heldout-v7-20261017/derived/task-store/variants",
        )

    def load(self, relative: str) -> Any:
        path = self.repo / relative
        data = path.read_bytes()
        self.inputs[relative] = digest_bytes(data)
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in data.splitlines() if line.strip()]
        return json.loads(data)

    def add(self, dataset: str, task: str, pair: Pair, source: str) -> None:
        self.pairs[(dataset, bare(task))].setdefault(pair, set()).add(source)

    def variant(self, task: str, digest: str | None) -> str | None:
        if not digest:
            return None
        short = digest.removeprefix("sha256:")[:12]
        candidates = [root / f"mimo-v2.6-rl__{bare(task)}" / short for root in self.variants]
        return str(next((path for path in candidates if path.is_dir()), candidates[0]))

    def discover(self, cloud: Path) -> None:
        gym = self.load("research/experiments/seeker-gym-v0/gym.json")
        inventory = self.load("research/experiments/mimo-defect-map-v1/inventory.json")
        repairs = self.load("research/experiments/mimo-defect-map-v1/repairs.json")
        clean = self.load("research/experiments/mimo-executed-clean-v1/manifest.json")
        retained = self.load("research/experiments/ts3-hide-tests-two-turn-20261025/manifest.json")
        originals = {}
        for row in inventory["tasks"]:
            materialization = row["materialization"]
            originals[bare(row["task_id"])] = materialization.get("resolved_locator") or materialization.get("locator")
        self.coverage["inventory_originals"] = len(inventory["tasks"])
        self.coverage["executed_clean_members"] = len(clean["members"])
        self.coverage["gym_pair_rows"] = len(gym["pairs"])
        self.coverage["repair_edge_rows"] = len(repairs["repairs"])
        for row in gym["pairs"]:
            name = row["task"]
            kind = "port" if row["fixed_kind"] in {"port", "pm_port"} else "package"
            self.add(
                "tb4-audit-v1" if kind == "port" else "mimo-defect-map-v1", name,
                Pair(row.get("original_task"), row.get("fixed_task") or self.variant(name, row.get("fixed_package_digest")),
                     kind, repaired_expected=row.get("fixed_package_digest")), "seeker-gym-v0",
            )
        for row in repairs["repairs"]:
            name = row["task_id"]
            parent = row.get("frozen_parent_package_digest") or row.get("parent_package_digest")
            self.add("mimo-defect-map-v1", name,
                     Pair(self.variant(name, parent), self.variant(name, row["child_package_digest"]),
                          original_expected=parent, repaired_expected=row["child_package_digest"]),
                     "mimo-defect-map-v1/repairs")
            if bare(name) in originals:
                self.add("mimo-defect-map-v1", name,
                         Pair(originals[bare(name)], self.variant(name, row["child_package_digest"]),
                              repaired_expected=row["child_package_digest"]),
                         "mimo-defect-map-v1/inventory+repairs")
        for row in clean["members"]:
            # Unchanged-clean controls do not acquire an invented repair counterpart.
            if row["cohort"] == "repaired":
                name = row["task_id"]
                self.add("mimo-defect-map-v1", name,
                         Pair(originals.get(bare(name)), self.variant(name, row["package_digest"]),
                              repaired_expected=row["package_digest"]), "mimo-executed-clean-v1")
        registry = CorpusRegistry(self.repo)
        for item in registry.items(dataset_id="tb4-audit-v1"):
            if item.task_id is None or item.item_id != item.task_id:
                continue
            name = bare(item.task_id)
            package = item.metadata.get("package") or {}
            original = str(Path(package["root_hint"]).expanduser() / package.get("name", name)) if package.get("root_hint") else None
            self.add("tb4-audit-v1", name,
                     Pair(original, str(self.lab.parent / "eval-lab-data/tb4-20261020/karotte-port" / name), "port"),
                     "corpus-registry/tb4-audit-v1")
        self.inputs["research/answer-keys/tb4-audit-v1/tasks.jsonl"] = digest_bytes(
            (self.repo / "research/answer-keys/tb4-audit-v1/tasks.jsonl").read_bytes()
        )
        for dataset in ("breaker-bench-v1", "tb2-to-tb21-v1"):
            for row in self.load(f"research/answer-keys/{dataset}/pairs.jsonl"):
                name = row["task_id"]
                # These are source-declared pairs, not assertions that native packages exist.
                self.add(dataset, name, Pair(None, None), f"answer-keys/{dataset}/pairs")
        for name, row in retained["by_task"].items():
            self.add(row["dataset_id"], name, Pair(None, None), "retained-ts3/by_task")
        self.cloud(cloud)
        self.materialized_benchmarks()
        original_roots = sorted({Path(path).parent for path in originals.values() if path})
        self.coverage["inventory_package_roots"] = [str(path) for path in original_roots]
        for (dataset, name), pairs in list(self.pairs.items()):
            if dataset == "mimo-defect-map-v1" and name not in originals:
                for parent in original_roots:
                    if (parent / name).is_dir():
                        originals[name] = str(parent / name)
                        break
            if dataset == "mimo-defect-map-v1" and name in originals:
                for pair in list(pairs):
                    self.add(dataset, name,
                             Pair(originals[name], pair.repaired, pair.kind,
                                  repaired_expected=pair.repaired_expected),
                             "mimo-defect-map-v1/inventory-fallback")

    def cloud(self, path: Path) -> None:
        if not path.is_file():
            self.coverage["cloud_db"] = {"path": str(path), "status": "unavailable"}
            return
        # Read-only snapshot, including the live WAL; never copy/commit the private DB.
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT task_id,dataset_id,original_grader_digest,repaired_grader_digest,metadata_json "
                "FROM corpus_tasks WHERE original_grader_digest IS NOT NULL "
                "AND repaired_grader_digest IS NOT NULL ORDER BY dataset_id,task_id"
            ).fetchall()
        self.coverage["cloud_db"] = {
            "path": str(path), "status": "read_only_snapshot", "paired_rows": len(rows),
            "paired_rows_sha256": digest_json(rows),
            "by_dataset": dict(sorted(Counter(row[1] for row in rows).items())),
        }
        for task, dataset, _, _, metadata in rows:
            doc = json.loads(metadata)
            self.add(dataset, task, Pair(None, None), "cloud-db/paired-digests")
            documents = [doc, *[row.get("metadata_json", {}) for row in doc.get("cloud_trace_ingestion_sources", [])]]
            for document in documents:
                for variant in document.get("variants", []):
                    original, repaired = variant.get("original_package") or {}, variant.get("repaired_package") or {}
                    for original_path in sorted({original.get("read_root"), original.get("source_root")} - {None}):
                        for repaired_path in sorted({repaired.get("read_root"), repaired.get("source_root")} - {None}):
                            kind = "port" if "karotte-port" in Path(repaired_path).parts else "package"
                            self.add(dataset, task, Pair(original_path, repaired_path, kind), "cloud-db/variants")

    def materialized_benchmarks(self) -> None:
        results = self.lab.parent / "eval-lab-results"
        roots = [self.lab / "runs", results]
        # Bound discovery to campaign package roots; do not scan arbitrary trial bodies.
        package_roots = []
        for root in roots:
            if not root.is_dir():
                continue
            campaigns = sorted(root.glob("*")) if root.name == "runs" else sorted(root.glob("*/*"))
            for campaign in campaigns:
                if "breaker" in campaign.name or "tb2-to-tb21" in campaign.name:
                    package_roots.append(campaign / "packages")
        self.coverage["benchmark_package_roots"] = [str(path) for path in package_roots if path.is_dir()]
        for (dataset, task) in sorted(self.pairs):
            if dataset == "tb2-to-tb21-v1":
                for root in package_roots:
                    self.add(dataset, task, Pair(str(root / "tb2" / task), str(root / "tb21" / task)), "retained-tb2/packages")
            elif dataset == "breaker-bench-v1":
                for root in package_roots:
                    for old in sorted(root.glob(f"*__{task.replace('__', '-')}__020")):
                        self.add(dataset, task, Pair(str(old), str(old.with_name(old.name[:-3] + "022"))), "retained-breaker/packages")


@cache
def package_identity(path: str) -> tuple[str, str, str]:
    package = Path(path)
    exact = _tree_digest(package)  # Fail on symlinks/special files before following any.
    verifier = package / "tests"
    if not verifier.exists():
        verifier = package / "verifier"
    return task_directory_digest(package), compute_subpath_digest(verifier), exact


def static_plan(task: str, *, port: str | None = None) -> dict[str, Any]:
    return run_candidate(
        task, files={"solve.sh": "#!/bin/sh\nexit 0\n"},
        grader="port" if port else "upstream", port_task=port,
        backend="modal", allow_local_docker=False, timeout_seconds=TIMEOUT_SECONDS, plan=True,
    )


def plan_ok(plan: dict[str, Any]) -> bool:
    return (plan.get("schema") == "evallab.candidate.plan/v1" and plan.get("runnable") is True
            and plan.get("executed") is False and plan.get("reserved_usd") == 0.0
            and not plan.get("error") and not plan.get("refusals"))


def plan_failure(plan: dict[str, Any]) -> dict[str, str]:
    # Exceptions may contain unparsed source expressions. Persist categories and
    # hashes only, never those expressions, instructions, or answer-key labels.
    error = str(plan.get("error") or plan.get("refusals") or "").casefold()
    categories = (
        ("gpu", "gpu_unsupported"), ("setup", "offline_setup_adapter_unsupported"),
        ("model/provider", "external_provider_dependency"),
        ("model-backed", "unsupported_port_judge"), ("multi-step", "multi_step_unsupported"),
        ("identity", "task_identity_mismatch"), ("missing", "missing_source_input"),
        ("resource", "resource_declaration_refused"),
    )
    return {
        "plan_sha256": digest_json(plan),
        "refusal_category": next((category for text, category in categories if text in error),
                                 "source_contract_refused"),
    }


def tier_blocker(path: str, plan: dict[str, Any], *, port: bool = False) -> str | None:
    config = {} if port else tomllib.loads((Path(path) / "task.toml").read_text())
    environment = config.get("environment") or {}
    verifier = (config.get("verifier") or {}).get("environment") or {}
    environments = [environment, verifier, *plan["resources"]]
    if any(row.get("gpus", 0) >= 1 for row in environments):
        return "e_gpu_required"
    if any(row.get("memory_mb", 0) > 24576 for row in environments):
        return "e_memory_limit"
    # A hard Docker socket/privileged/device contract cannot run on this Modal lane.
    dockerfiles = [Path(path) / "Containerfile"] if port else [Path(path) / "environment/Dockerfile", Path(path) / "tests/Dockerfile"]
    if environment.get("type") == "docker" or environment.get("backend") == "docker":
        return "e_docker_only"
    for dockerfile in dockerfiles:
        if dockerfile.is_file() and "/var/run/docker.sock" in dockerfile.read_text(errors="replace"):
            return "e_docker_only"
    return None


def evaluate_pair(pair: Pair) -> tuple[int, str, dict[str, Any]]:
    details = {"original_task": pair.original, "repaired_task": pair.repaired, "repaired_grader_kind": pair.kind}
    if not pair.original or not Path(pair.original).is_dir():
        return 1, "b_original_missing", details
    try:
        original, original_grader, original_exact = package_identity(pair.original)
    except (OSError, ValueError) as exc:
        return 1, "b_original_unreadable", {**details, "error_sha256": digest_bytes(str(exc).encode())}
    details.update(original_package_digest=original, original_grader_digest=original_grader, original_exact_tree_digest=original_exact)
    if not pair.repaired or not Path(pair.repaired).is_dir():
        return 2, "c_repaired_missing", {**details, "expected_repaired_package_digest": pair.repaired_expected}
    if pair.kind == "port":
        required = ("Containerfile", "setup_data.py", "pyproject.toml", "uv.lock", ".manifest.json")
        missing = [str(Path(pair.repaired) / name) for name in required
                   if not (Path(pair.repaired) / name).is_file()]
        if missing:
            return 2, "c_repaired_missing", {**details, "missing_adapter_inputs": missing}
    try:
        if pair.kind == "port":
            repaired_exact = _tree_digest(Path(pair.repaired))
            repaired = task_directory_digest(Path(pair.repaired))
            repaired_grader = compute_subpath_digest(Path(pair.repaired))
        else:
            repaired, repaired_grader, repaired_exact = package_identity(pair.repaired)
    except (OSError, ValueError, GraderRefusal) as exc:
        return 2, "c_repaired_unreadable", {**details, "error_sha256": digest_bytes(str(exc).encode())}
    details.update(repaired_package_digest=repaired, repaired_grader_digest=repaired_grader, repaired_exact_tree_digest=repaired_exact)
    if pair.original_expected and original != pair.original_expected:
        return 2, "c_original_digest_mismatch", details
    if pair.repaired_expected and repaired != pair.repaired_expected:
        return 2, "c_repaired_digest_mismatch", details
    if original_grader == repaired_grader:
        return 2, "c_grader_unchanged", details
    plans = {"original": static_plan(pair.original)}
    if not plan_ok(plans["original"]):
        return 3, "d_original_plan_refused", {**details, **plan_failure(plans["original"])}
    if plans["original"]["task"]["exact_tree_digest"] != original_exact:
        return 3, "d_original_plan_refused", {**details, "refusal_category": "source_drift"}
    plans["repaired"] = static_plan(pair.original, port=pair.repaired) if pair.kind == "port" else static_plan(pair.repaired)
    if not plan_ok(plans["repaired"]):
        return 3, "d_repaired_plan_refused", {**details, **plan_failure(plans["repaired"])}
    if pair.kind == "port":
        details["repaired_adapter_digest"] = plans["repaired"]["grader_provenance"]["grader_digest"]
        planned_repaired_exact = plans["repaired"]["grader_provenance"]["port_identity"]["exact_tree_digest"]
    else:
        planned_repaired_exact = plans["repaired"]["task"]["exact_tree_digest"]
    if planned_repaired_exact != repaired_exact:
        return 3, "d_repaired_plan_refused", {**details, "refusal_category": "source_drift"}
    for side, path in (("original", pair.original), ("repaired", pair.repaired)):
        blocker = tier_blocker(path, plans[side], port=side == "repaired" and pair.kind == "port")
        if blocker:
            return 4, blocker, {**details, "blocked_side": side}
    reservations = {}
    for side, plan in plans.items():
        # Port plans defer materialization; use the native formula on the exact
        # adapter resource declarations, not a fabricated native plan result.
        reservations[side] = plan["modal_reservation"] or _reservation(plan["resources"], TIMEOUT_SECONDS)
    return 5, "qualified", {
        **details,
        "static_plan_digests": {side: digest_json(plan) for side, plan in plans.items()},
        "original_reservation_usd": reservations["original"]["reserved_usd"],
        "repaired_reservation_usd": reservations["repaired"]["reserved_usd"],
        "reservation_estimates": reservations,
        "repaired_reservation_basis": "adapter_resources_native_formula" if pair.kind == "port" else "native_plan",
    }


def build(repo: Path, lab: Path, cloud: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
    package_identity.cache_clear()
    assignments = repo / "research/answer-keys/splits/assignments.csv"
    assignments_digest = digest_bytes(assignments.read_bytes())
    with assignments.open(newline="") as stream:
        assignment_rows = list(csv.DictReader(stream))
    sources = Sources(repo, lab)
    sources.discover(cloud)
    rows, qualified = [], []
    for (dataset, task), pairs in sorted(sources.pairs.items()):
        record: dict[str, Any] = {"dataset_id": dataset, "task_id": task,
                                  "sources": sorted({s for values in pairs.values() for s in values})}
        reason = dev_gate(task, assignments)
        if reason:
            record.update(reason_code=reason, non_dev_assignments=[list(row) for row in find_cross_key_non_dev_assignments(task, assignments_csv=assignments)])
        else:
            outcomes = [evaluate_pair(pair) for pair in sorted(pairs, key=lambda pair: json.dumps(asdict(pair), sort_keys=True))]
            best = max(outcomes, key=lambda outcome: outcome[0])
            record.update(reason_code=best[1], **best[2])
            # Every alternate is source metadata only: no task bodies or answers.
            record["pair_attempts"] = [{"reason_code": result[1], **result[2]} for result in outcomes if result[1] != "qualified"]
            if best[1] == "qualified":
                dev_rows = [{key: row.get(key, "") for key in ("key_id", "item_id", "split", "task", "unit")} for row in assignment_rows
                            if str(row.get("split", "")).casefold() in {"dev", "train"}
                            and any(aliases(str(row.get(column) or "")) & aliases(task)
                                    for column in ("item_id", "task", "unit"))]
                qualified.append({key: value for key, value in
                                  {**record, "assignment_rows": dev_rows}.items()
                                  if key != "pair_attempts"})
        rows.append(record)
    by_dataset = {}
    for dataset in sorted({*DATASETS, *(row["dataset_id"] for row in rows)}):
        selected = [row for row in rows if row["dataset_id"] == dataset]
        counts = Counter(row["reason_code"] for row in selected)
        by_dataset[dataset] = {"candidates": len(selected), "qualified": counts.get("qualified", 0),
                               "excluded_by_first_gate": dict(sorted((key, value) for key, value in counts.items() if key != "qualified"))}
    if digest_bytes(assignments.read_bytes()) != assignments_digest:
        raise RuntimeError("assignments.csv changed during the census; no output published")
    provenance = {
        "assignments_csv_sha256": assignments_digest,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "source_files": sources.inputs,
        "builder_sha256": digest_bytes(Path(__file__).read_bytes()),
        "planning": {"backend": "modal", "plan": True, "timeout_seconds": TIMEOUT_SECONDS,
                     "model_calls": 0, "grader_executions": 0, "reserved_usd": 0.0},
        "digest_algorithms": {
            "package": "evallab.registry.task_directory_digest",
            "grader": "evallab.registry.compute_subpath_digest over tests/ (or verifier/); entire port source for port rows",
            "exact_tree": "evallab.upstream_grader._tree_digest (includes modes and empty directories)",
            "static_plan": "sha256 of canonical compact sorted-key JSON returned by run_candidate(plan=True)",
        },
    }
    exclusions = Counter(row["reason_code"] for row in rows if row["reason_code"] != "qualified")
    assert len(rows) == len(sources.pairs) == len(qualified) + sum(exclusions.values())
    cohort = {"schema": "evallab.sb1_clean_seeker_set/v1", **provenance, "tasks": qualified}
    census = {"schema": "evallab.sb1_clean_seeker_census/v1", **provenance,
              "candidate_count": len(rows), "qualified_count": len(qualified),
              "qualified_unique_bare_names": len({row["task_id"] for row in qualified}),
              "target": 100, "target_met": len({row["task_id"] for row in qualified}) >= 100,
              "by_dataset": by_dataset, "excluded_by_first_gate": dict(sorted(exclusions.items())),
              "binding_gate": exclusions.most_common(1)[0][0] if exclusions else None,
              "ledger_positive_count": len(rows) - sum(value for key, value in exclusions.items() if key.startswith("a_")),
              "binding_runnable_gate": max(((value, key) for key, value in exclusions.items() if not key.startswith("a_")), default=(0, None))[1],
              "source_coverage": sources.coverage, "candidates": rows}
    return cohort, census


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab-root", type=Path, default=Path.home() / "Developer/eval-lab")
    parser.add_argument("--cloud-db", type=Path, default=Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite")
    args = parser.parse_args()
    cohort, census = build(REPO, args.lab_root, args.cloud_db)
    for name, value in (("cohort.json", cohort), ("census.json", census)):
        destination = HERE / name
        content = json.dumps(value, indent=2, sort_keys=True) + "\n"
        if not destination.is_file() or destination.read_text() != content:
            destination.write_text(content)
    for dataset, counts in census["by_dataset"].items():
        print(f"{dataset}: {counts['qualified']} qualified / {counts['candidates']} candidates")
    print(f"total: {census['qualified_count']} qualified; target >=100 met: {census['target_met']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
