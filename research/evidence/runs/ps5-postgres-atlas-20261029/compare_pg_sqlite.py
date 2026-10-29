"""Build one synthetic Run Atlas store on SQLite (FTS and LIKE) and on PostgreSQL, run the same reads, compare.

Synthetic content only. Usage (from the repo root, with a PostgreSQL that accepts the URL):

    uv run --no-sync python research/evidence/runs/ps5-postgres-atlas-20261029/compare_pg_sqlite.py \
        --pg-url 'postgresql://postgres@/atlas?host=/private/tmp/ps5d/sock&port=55432' \
        --workdir /private/tmp/ps5d/work --out results.json

Every call runs on all three stores; the PostgreSQL result is compared with each SQLite result
(ordering is compared as returned; a mismatch that disappears after sorting is reported as
``order_only``). Exceptions are results too: both sides raising the same type and message is equal.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg import sql

from evallab.production import qa_data_card, qa_data_findings, qa_data_inventory, qa_data_task
from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    _facet_filters,
    import_run_facets,
)
from evallab.production.cloud_trace_export import CHEATBENCH_DATASET
from evallab.production.corpus_queue import CorpusItem, CorpusRegistry
from evallab.production.qa_data import Budget, QaContext
from evallab.production.trace_findings import import_trace_findings, upsert_trace_findings

TIER1, TIER2, TIER3 = "tier1_container_verified", "tier2_consensus_labeled", "tier3_provisional"
AUDITED = ("audited-task",)
BULK_TASKS, BULK_RUNS_PER_TASK = 40, 30
VOLATILE = {"created_at", "updated_at", "ingested_at"}


class SyntheticRegistry(CorpusRegistry):
    def __init__(self, root: Path) -> None:
        self.split_assignments_digest = "sha256:" + hashlib.sha256(b"synthetic authority").hexdigest()
        self._assignment_keys = frozenset({"authority", CHEATBENCH_DATASET})
        self._assignments = {
            ("authority", "source"): "dev",
            (CHEATBENCH_DATASET, "external-dev"): "dev",
            (CHEATBENCH_DATASET, "external-locked"): "dev",
        }
        self._assignment_names = {}
        self._assigned_test_ids = {"external-locked"}
        self._assigned_test_names = {"locked-task"}
        self._sealed_names = set()
        self._sealed_digests = set()
        self._sealed_ids = set()
        self._index_names = {}
        self._items = {}
        self._external = set()
        self.external_items = tuple(CorpusItem(
            dataset_id=CHEATBENCH_DATASET, item_id=item_id, split="dev", task_id=item_id,
            task_digest=None, source_path=root / "unopened.json", metadata={}, source_record_sha256="a" * 64,
        ) for item_id in ("external-dev", "external-locked"))

    def items(self, *, dataset_id: str | None = None, split: str | None = None) -> tuple[CorpusItem, ...]:
        return tuple(item for item in self.external_items
                     if dataset_id in (None, item.dataset_id) and split in (None, item.split))


def facet(run_id: str, **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run_id, "source": "x", "split": "DEV", "facet_version": "run-facets/v1",
        "facet_model": "synthetic/model", "input_tokens": 10, "output_tokens": 5,
        "goal": "Repair parser output", "approach": "Changed the parser implementation", "outcome": "pass",
        "tests": "read", "grader_or_harness_touched": False, "answer_material_accessed": False,
        "success_claim_without_evidence": False, "eval_awareness": "none", "shortcut": "none",
        "suspicion": 1, "evidence_steps": [0], **changes,
    }


def finding(run_id: str, detector: str, verdict: str, *, steps: list[int] | None = None,
            produced_at: str = "2026-10-28T10:00:00Z", **changes: Any) -> dict[str, Any]:
    return {
        "run_id": run_id, "detector": detector, "detector_version": "v1", "reader_model": None,
        "verdict": verdict, "score": 0.5, "kind": "cheat", "evidence_steps": steps or [],
        "summary": "a lead on the parser", "produced_at": produced_at, **changes,
    }


STEPS = [
    {"step_index": 0, "role": "user", "content_text": "repair the parser output"},
    {"step_index": 1, "role": "assistant", "content_text": "inspect the parser", "tool_name": "bash",
     "command_or_action": "pytest -q"},
    {"step_index": 2, "role": "tool", "content_text": "run finished", "observation_preview": "AssertionError in parser"},
    {"step_index": 3, "role": "assistant", "content_text": "edit expected output", "tool_name": "edit",
     "command_or_action": "sed expected"},
]


SNAKE_STEPS = [
    {"step_index": 0, "role": "user", "content_text": "repair the parser output"},
    {"step_index": 1, "role": "assistant", "tool_name": "terminal", "command_or_action": "cat grader_tamperable.log"},
]


def write_jsonl(path: Path, *lines: Any) -> Path:
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
    return path


def build(db: CloudTraceDatabase, registry: SyntheticRegistry, work: Path) -> dict[str, Any]:
    """Fill ``db``; return the receipts of the writes (compared across stores like any other result)."""
    receipts: dict[str, Any] = {}
    for task_id, dataset, split in (
        ("namespace/alpha", "dataset-a", "dev"), ("beta", "dataset-b", "dev"), ("empty", "dataset-a", "dev"),
        ("test-task", "hidden-dataset", "test"), ("locked-task", "hidden-dataset", "dev"),
        ("sealed-task", "hidden-dataset", "dev"), ("sealed-task2", "dataset-a", "dev"),
    ):
        db.upsert_task({"task_id": task_id, "dataset_id": dataset, "split": split,
                        "instruction_text": f"Repair the parser output of {task_id.split('/')[-1]}"})
    for task_id, tier, direction in (("namespace/alpha", TIER1, "wrong_pass"), ("beta", TIER2, "unfair_fail"),
                                     ("empty", TIER3, "wrong_pass")):
        db.upsert_task_issue({
            "issue_id": f"{task_id}/issue", "task_id": task_id, "defect_class": "missing_output_check",
            "direction": direction, "status": "confirmed" if tier != TIER3 else "provisional", "trust_tier": tier,
            "reproducer_script": "parser accepts the wrong output",
        })
    for trace_id, task_id, model, reward, tier, exploit in (
        ("run-a", "namespace/alpha", "model-a", 1, TIER1, True), ("run-b", "namespace/alpha", "model-b", 0, TIER1, True),
        ("run-c", "beta", "model-a", 0.5, TIER2, True), ("run-d", "sealed-task2", "model-a", 1, TIER1, True),
        ("run-e", "namespace/alpha", "model-c", 0, TIER1, True), ("run-f", "beta", "model-b", 1, TIER2, True),
        ("run-h", "namespace/alpha", "model-c", None, TIER3, False), ("run-nosteps", "beta", "model-a", 0, TIER2, True),
        ("run-snake", "beta", "model-a", 0, TIER2, True),
        ("test-run", "test-task", "hidden-model", 1, TIER1, True), ("locked-run", "locked-task", "hidden-model", 1, TIER1, True),
        ("sealed-run", "sealed-task", "hidden-model", 1, TIER1, True),
    ):
        db.upsert_trace({
            "trace_id": trace_id, "run_id": "batch", "task_id": task_id, "model_id": model, "reward": reward,
            "trust_tier": tier, "is_exploit": exploit, **({"flagged_passage_steps": [1]} if trace_id == "run-h" else {}),
        }, steps=[] if trace_id == "run-nosteps" else SNAKE_STEPS if trace_id == "run-snake" else STEPS)
    receipts["facets_direct"] = db.upsert_run_facets([
        facet("run-a", approach="Compared with sealed-task", suspicion=3, tests="modified", evidence_steps=[0, 1]),
        facet("run-b", tests="read"),
        facet("run-c", suspicion=3, answer_material_accessed=True, tests="deleted", evidence_steps=[2]),
        facet("run-d"), facet("run-e", tests="replaced", suspicion=2, eval_awareness="mentions_tests"),
        facet("run-f", tests="modified", suspicion=2, shortcut="rewrote expected output", grader_or_harness_touched=True),
        facet("run-nosteps", evidence_steps=[]), facet("test-run"), facet("sealed-run"),
    ], registry=registry)
    receipts["clusters"] = db.replace_run_clusters([
        {"cluster_id": "c1", "name": "all", "level": 1, "parent": None, "centroid_text": "edited tests",
         "run_ids": ["run-a", "run-b", "run-c", "run-d", "run-e", "run-f"], "size": 6},
        {"cluster_id": "c2", "name": "mid", "level": 2, "parent": "c1", "centroid_text": "edited tests",
         "run_ids": ["run-a", "run-c", "run-e", "run-f"], "size": 4},
        {"cluster_id": "c3", "name": "deep", "level": 3, "parent": "c2", "centroid_text": "edited tests",
         "run_ids": ["run-c", "run-f"], "size": 2},
        {"cluster_id": "c1b", "name": "other", "level": 1, "parent": None, "centroid_text": "reads",
         "run_ids": ["run-b"], "size": 1},
    ], registry=registry)
    # Bulk content: enough rows that the batched writes and array/json scopes are exercised well past one page.
    for number in range(BULK_TASKS):
        db.upsert_task({"task_id": f"bulk/t{number:02d}", "dataset_id": "dataset-a", "split": "dev",
                        "instruction_text": "bulk filler"})
        for run in range(BULK_RUNS_PER_TASK):
            db.upsert_trace({
                "trace_id": f"bulk/t{number:02d}/r{run:02d}", "run_id": "bulk", "task_id": f"bulk/t{number:02d}",
                "model_id": f"model-{run % 5}", "reward": (run % 3) / 2, "trust_tier": TIER2, "is_exploit": run % 7 == 0,
            }, steps=[{"step_index": index, "role": "assistant", "content_text": f"filler {index}"} for index in range(3)])
    bulk = [f"bulk/t{number:02d}/r{run:02d}" for number in range(BULK_TASKS) for run in range(BULK_RUNS_PER_TASK)]
    facet_lines: list[Any] = [facet(run_id, suspicion=1 + int(run_id[-2:]) % 3, evidence_steps=[0, 2],
                                    tests=["read", "modified", "deleted"][int(run_id[-2:]) % 3]) for run_id in bulk]
    facet_lines += [
        "{not json", facet("bulk/t00/r00", suspicion=3), facet("bulk/t00/r01", evidence_steps=[99]),
        facet("nope/none"), facet("test-run"), facet("sealed-run"), facet("run-h", evidence_steps=[3, 1, 0]),
        facet("run-e", evidence_steps=[2**62]),
    ]
    receipts["facets_import"] = {key: value for key, value in import_run_facets(
        db, write_jsonl(work / "facets.jsonl", *facet_lines), registry=registry).items() if key != "path"}
    receipts["facets_import_again"] = {key: value for key, value in import_run_facets(
        db, work / "facets.jsonl", registry=registry).items() if key != "path"}
    findings = [finding(run_id, "tripwire", "alarm" if int(run_id[-2:]) % 4 == 0 else "clear", steps=[0, 2])
                for run_id in bulk] + [finding(run_id, "run_health.loop", "abstain", steps=[1]) for run_id in bulk]
    findings += [
        finding("run-a", "tripwire", "alarm", steps=[1]), finding("run-b", "tripwire", "alarm", steps=[0, 3]),
        finding("run-c", "run_health.loop", "alarm"), finding("run-c", "tripwire", "alarm", steps=[2]),
        finding("run-nosteps", "tripwire", "clear"), finding("run-nosteps", "tripwire", "abstain"),
        finding("run-e", "tripwire", "alarm", steps=[99]), finding("run-e", "tripwire", "alarm", steps=[2**62]),
        finding("run-e", "tripwire", "alarm", steps=[2**40]), finding("nope/none", "tripwire", "alarm"),
        finding("sealed-run", "tripwire", "alarm"), finding("test-run", "tripwire", "alarm"),
        finding("run-h", "tripwire", "alarm", steps=[1], produced_at="2026-10-28T09:00:00Z"),
        finding("run-h", "tripwire", "clear", steps=[1], produced_at="2026-10-28T11:00:00Z"),
        finding("run-d", "tripwire", "alarm", steps=[0]),
    ]
    path = write_jsonl(work / "findings.jsonl", *findings, "{broken")
    receipts["findings_import"] = {key: value for key, value in import_trace_findings(db, path, registry=registry).items() if key != "path"}
    receipts["findings_import_again"] = {key: value for key, value in import_trace_findings(db, path, registry=registry).items() if key != "path"}
    receipts["findings_older"] = upsert_trace_findings(db, [
        finding("run-a", "tripwire", "clear", steps=[1], produced_at="2026-10-27T10:00:00Z"),
        finding("run-b", "tripwire", "clear", steps=[0], produced_at="2026-10-29T10:00:00Z"),
    ], registry=registry)
    for table, order in (("run_facets", "run_id"), ("trace_findings", "run_id, detector, detector_version, reader_model"),
                         ("run_clusters", "cluster_id")):
        receipts[f"table:{table}"] = db._rows(f"SELECT * FROM {table} ORDER BY {order}")
    return receipts


def normal(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type) and hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, (set, frozenset)):
        value = sorted(value)
    text = json.dumps(value, default=lambda item: item.to_dict() if hasattr(item, "to_dict") else str(item), sort_keys=True)
    return strip(json.loads(text))


def strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, list):
        return [strip(item) for item in value]
    return value


def first_diff(left: Any, right: Any, path: str = "") -> str:
    if type(left) is not type(right):
        return f"{path or '/'}: type {type(left).__name__} vs {type(right).__name__}"
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f"{path}/{key}: present on one side only"
            if left[key] != right[key]:
                return first_diff(left[key], right[key], f"{path}/{key}")
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path or '/'}: length {len(left)} vs {len(right)}"
        for index, (one, two) in enumerate(zip(left, right, strict=True)):
            if one != two:
                return first_diff(one, two, f"{path}[{index}]")
    return f"{path or '/'}: {str(left)[:80]!r} vs {str(right)[:80]!r}"


def canonical_sorted(value: Any) -> Any:
    if isinstance(value, list):
        return sorted((canonical_sorted(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        return {key: canonical_sorted(item) for key, item in value.items()}
    return value


def calls(ctx: QaContext, registry: SyntheticRegistry) -> dict[str, Callable[[], Any]]:
    database, retriever = ctx.database, ctx.retriever
    sealed = ["sealed-task"]

    def fresh() -> QaContext:
        return dataclasses.replace(ctx, budget=Budget(10**7))

    def retrieve(query: str, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("exclude_task_ids", AUDITED)
        return [example.to_dict() for example in retriever.retrieve_similar_examples(query, *args, **kwargs)]

    def served(task_ids: Any) -> Any:
        with database._transaction():
            return sorted(fresh().served_facet_run_ids(task_ids))

    def admitted(filters: Any = (), **kwargs: Any) -> Any:
        with database._transaction():
            rows, seals = database._admitted_facets(retriever, _facet_filters(dict(filters)), **kwargs)
        return {"rows": rows, "sealed": sorted(seals)}

    many = [f"fake/{number}" for number in range(5000)] + ["namespace/alpha", "beta", "sealed-task2", "it's \"quoted\""]
    out: dict[str, Callable[[], Any]] = {}
    for name, facets, extra in (
        ("search_runs:all", {}, {}), ("search_runs:suspicion", {"suspicion": [2, 3]}, {}),
        ("search_runs:tests", {"tests": "modified"}, {}), ("search_runs:tests-list-limit", {"tests": ["modified", "deleted"]}, {"limit": 2}),
        ("search_runs:bool", {"answer_material_accessed": True}, {}), ("search_runs:bool-false+outcome", {"answer_material_accessed": False, "outcome": ["pass", "fail"]}, {}),
        ("search_runs:eval", {"eval_awareness": "mentions_tests"}, {}),
        ("search_runs:allowed-beta", {}, {"allowed_task_ids": ["beta"]}),
        ("search_runs:allowed-empty", {}, {"allowed_task_ids": []}),
        ("search_runs:allowed-many", {"suspicion": 3}, {"allowed_task_ids": many}),
        ("search_runs:source+split", {"source": "dataset-a", "split": "DEV"}, {}),
        ("search_runs:facet_model", {"facet_model": "synthetic/model"}, {"limit": 5}),
        ("search_runs:no-match", {"facet_model": "synthetic/nonexistent"}, {}),
    ):
        out[name] = lambda facets=facets, extra=extra: database.search_runs(
            facets, registry=registry, sealed_task_ids=sealed, **extra)
    out["retriever.search_runs"] = lambda: retriever.search_runs({"suspicion": 3})
    out["_admitted_facets:all"] = lambda: admitted()
    out["_admitted_facets:filters+limit"] = lambda: admitted({"tests": ["modified", "deleted"], "suspicion": [2, 3]}, limit=2)
    out["_admitted_facets:allowed-set"] = lambda: admitted(allowed_task_ids={"namespace/alpha", "sealed-task2"})
    out["_admitted_facets:allowed-frozenset-many"] = lambda: admitted(allowed_task_ids=frozenset(many))
    for name, ids in (
        ("tuple", ("namespace/alpha", "beta")), ("set", {"namespace/alpha", "beta", "sealed-task2"}),
        ("frozenset", frozenset({"beta"})), ("single-withheld", ("sealed-task2",)), ("empty", ()), ("empty-set", set()),
        ("denied", ("test-task", "locked-task", "sealed-task")), ("many", tuple(many)), ("bulk", tuple(f"bulk/t{n:02d}" for n in range(BULK_TASKS))),
    ):
        out[f"served_facet_run_ids:{name}"] = lambda ids=ids: served(ids)
    for name, kwargs in (
        ("plain", {}), ("limit3", {"limit": 3}), ("dataset+limit", {"dataset": "dataset-a", "limit": 4}),
        ("dataset-b", {"dataset": "dataset-b"}), ("task", {"task_id": "namespace/alpha"}), ("task-bare", {"task_id": "beta"}),
        ("outcome", {"outcome": "fail", "limit": 7}), ("model+limit", {"model": "model-a", "limit": 3}),
        ("ids_only", {"ids_only": True, "limit": 9}), ("alarms-sort", {"sort": "alarms", "limit": 8}),
        ("alarmed_by", {"alarmed_by": "tripwire", "limit": 12}), ("withheld-task", {"task_id": "sealed-task2"}),
        ("denied-task", {"task_id": "sealed-task"}), ("limit0", {"limit": 0}),
    ):
        full = {"task_id": None, "dataset": None, "model": None, "outcome": None, "limit": 40, "ids_only": False, **kwargs}
        out[f"qa.traces:{name}"] = lambda full=full: qa_data_inventory.traces(fresh(), **full)
    out["qa.inventory"] = lambda: qa_data_inventory.inventory(fresh(), ids_out=None)
    out["qa.clusters:db-list"] = lambda: qa_data_inventory.clusters(fresh(), members=None, source="db")
    out["qa.clusters:db-members"] = lambda: qa_data_inventory.clusters(fresh(), members="c1", source="db")
    out["qa.clusters:db-level2"] = lambda: qa_data_inventory.clusters(fresh(), members=None, source="db", level=2)
    for task_id in ("namespace/alpha", "alpha", "beta", "sealed-task2", "empty", "sealed-task", "test-task", "bulk/t03"):
        out[f"qa.task:{task_id}"] = lambda task_id=task_id: qa_data_task.task(fresh(), task_id)
    out["qa.task:with_outcomes"] = lambda: qa_data_task.task(fresh(), "namespace/alpha", with_outcomes=True)
    out["qa.task_rows:alpha"] = lambda: qa_data_task._task_rows(fresh(), "alpha", with_outcomes=True)
    for run_id in ("run-a", "run-b", "run-c", "run-d", "run-e", "run-h", "run-nosteps", "bulk/t00/r00", "bulk/t39/r29",
                   "sealed-run", "test-run", "locked-run", "nope"):
        out[f"qa.build_card:{run_id}"] = lambda run_id=run_id: qa_data_card.build_card(fresh(), run_id)
        out[f"qa.card_stored:{run_id}"] = lambda run_id=run_id: qa_data_card._stored(fresh(), run_id)
    out["qa.build_card:no-ledgers"] = lambda: qa_data_card.build_card(fresh(), "run-c", ledgers=False)
    for label, value in (("nul", "run-a\x00x"), ("surrogate", "run-\ud800x"), ("long", "r" * 20000), ("quote", "x' OR '1'='1"),
                         ("empty", ""), ("unicode", "run-é-日本")):
        out[f"weird-id:build_card:{label}"] = lambda value=value: qa_data_card.build_card(fresh(), value)
        out[f"weird-id:task_of_run:{label}"] = lambda value=value: fresh().task_of_run(value)
        out[f"weird-id:require_task:{label}"] = lambda value=value: fresh().require_task(value)
        out[f"weird-id:traces-task:{label}"] = lambda value=value: qa_data_inventory.traces(
            fresh(), task_id=value, dataset=None, model=None, outcome=None, limit=5, ids_only=False)
    out["qa.findings:summary"] = lambda: qa_data_findings.summary(fresh())
    out["qa.findings:run-a"] = lambda: qa_data_findings.findings(fresh(), run_id="run-a", task_id=None, limit=5)
    out["qa.findings:run-c"] = lambda: qa_data_findings.findings(fresh(), run_id="run-c", task_id=None, limit=5)
    out["qa.findings:task-alpha"] = lambda: qa_data_findings.findings(fresh(), run_id=None, task_id="namespace/alpha", limit=5)
    out["qa.findings:bulk-run"] = lambda: qa_data_findings.findings(fresh(), run_id="bulk/t05/r05", task_id=None, limit=5)
    # Retrieval: lexical, facets, cluster, anchor and their mixtures.
    out["retrieve:lexical"] = lambda: retrieve("parser", top_k=5, exclude_task_ids=None)
    out["retrieve:lexical-multiword"] = lambda: retrieve("parser output", "task_quality", 3, exclude_task_ids=None)
    out["retrieve:lexical-issue-kind"] = lambda: retrieve("parser", issue_kind="wrong_pass", top_k=4, exclude_task_ids=None)
    out["retrieve:lexical-reward_seeker"] = lambda: retrieve("expected output", "reward_seeker", 4, include_provisional=True, exclude_task_ids=None)
    out["retrieve:lexical-widen"] = lambda: retrieve("parser zzzunknown", top_k=6, exclude_task_ids=None)
    out["retrieve:empty-misalignment-flagged"] = lambda: retrieve("", "misalignment", 10, include_provisional=True, facets={})
    out["retrieve:facets-empty-map"] = lambda: retrieve("parser", top_k=10, facets={})
    out["retrieve:facets-tests"] = lambda: retrieve("parser", top_k=10, facets={"tests": ["modified", "deleted", "replaced"]})
    out["retrieve:facets-bool-int"] = lambda: retrieve("", top_k=10, facets={"suspicion": [2, 3], "answer_material_accessed": False})
    out["retrieve:facets-outcome-source"] = lambda: retrieve("parser", top_k=10, facets={"outcome": "pass", "source": "dataset-a"})
    out["retrieve:cluster-c2"] = lambda: retrieve("", top_k=10, cluster_id="c2")
    out["retrieve:cluster-c3+query"] = lambda: retrieve("parser", top_k=10, cluster_id="c3")
    out["retrieve:cluster+facets"] = lambda: retrieve("", top_k=10, cluster_id="c1", facets={"tests": "modified"})
    out["retrieve:cluster-unknown"] = lambda: retrieve("", cluster_id="nope")
    out["retrieve:anchor"] = lambda: retrieve("parser", top_k=10, like_run_id="run-c")
    out["retrieve:anchor-empty-query"] = lambda: retrieve("", top_k=10, like_run_id="run-c", include_provisional=True)
    out["retrieve:anchor-top1"] = lambda: retrieve("", top_k=1, like_run_id="run-f")
    out["retrieve:cluster+anchor"] = lambda: retrieve("", top_k=10, cluster_id="c1", like_run_id="run-c")
    out["retrieve:cluster+anchor+facets"] = lambda: retrieve("parser", top_k=10, cluster_id="c2", like_run_id="run-f", facets={"suspicion": [2, 3]})
    out["retrieve:anchor-withheld-facet"] = lambda: retrieve("", like_run_id="run-a")
    out["retrieve:anchor-withheld-task-name"] = lambda: retrieve("", like_run_id="run-d")
    out["retrieve:anchor-denied"] = lambda: retrieve("", like_run_id="sealed-run")
    out["retrieve:sealed-query"] = lambda: retrieve("sealed-task parser", top_k=3, exclude_task_ids=None)
    out["retrieve:bulk-anchor"] = lambda: retrieve("", top_k=5, like_run_id="bulk/t01/r02", exclude_task_ids=("bulk/t01",))
    out["retrieve:bulk-facets"] = lambda: retrieve("", top_k=5, facets={"tests": "modified", "suspicion": 2}, exclude_task_ids=("bulk/t01",))
    out["failed_run_summary:beta"] = lambda: retriever.failed_run_summary("beta")
    out["failed_run_summary:sealed"] = lambda: retriever.failed_run_summary("sealed-task")
    # A term inside a snake_case/dotted identifier: FTS5 and LIKE find it, PostgreSQL's english parser keeps the identifier whole.
    out["search_traces:identifier-term"] = lambda: [row["trace_id"] for row in database.search_traces("tamperable")]
    out["search_traces:identifier-whole"] = lambda: [row["trace_id"] for row in database.search_traces("grader_tamperable.log")]
    out["retrieve:identifier-term"] = lambda: retrieve("tamperable", top_k=3, include_provisional=True, exclude_task_ids=None)
    out["row_counts"] = lambda: database.row_counts()
    return out


def run_store(label: str, target: Any, sqlite_fts: bool, work: Path) -> dict[str, Any]:
    work.mkdir(parents=True, exist_ok=True)
    registry = SyntheticRegistry(work)
    results: dict[str, Any] = {}
    with CloudTraceDatabase(target, sqlite_fts=sqlite_fts) as database:
        results["build"] = ("ok", normal(build(database, registry, work)))
    with CloudTraceDatabase(target, read_only=True) as database:
        retriever = CloudTraceRetriever(database, registry=registry, sealed_task_ids=["sealed-task"])
        ctx = QaContext(database=database, retriever=retriever, repo_root=work, state_dir=work / "state", budget=Budget(10**7))
        results["schema_scoped_read_only:row_counts"] = ("ok", normal(database.row_counts()))
        for name, call in calls(ctx, registry).items():
            try:
                results[name] = ("ok", normal(call()))
            except BaseException as error:  # noqa: BLE001 - an exception is a result to compare
                if isinstance(error, KeyboardInterrupt):
                    raise
                results[name] = ("error", type(error).__name__, str(error)[:200], traceback.format_exc().splitlines()[-3:-1])
    return results


def compare(pg: dict[str, Any], other: dict[str, Any], side: str) -> list[dict[str, Any]]:
    rows = []
    for name in pg:
        one, two = pg[name], other[name]
        if one[0] == "ok" and two[0] == "ok":
            if one[1] == two[1]:
                status, detail = "equal", ""
            elif canonical_sorted(one[1]) == canonical_sorted(two[1]):
                status, detail = "order_only", first_diff(one[1], two[1])
            else:
                status, detail = "differs", first_diff(one[1], two[1])
        elif one[0] == "error" and two[0] == "error":
            same = one[1:3] == two[1:3]
            status, detail = ("equal_exception" if same else "differs_exception"), f"{one[1]}: {one[2]!r} vs {two[1]}: {two[2]!r}"
        else:
            status = "exception_pg_only" if one[0] == "error" else "exception_sqlite_only"
            detail = f"{one[1] if one[0] == 'error' else two[1]}: {(one if one[0] == 'error' else two)[2]!r}"
        rows.append({"call": name, "against": side, "status": status, "detail": detail[:300]})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-url", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.workdir.exists() and any(args.workdir.iterdir()):
        parser.error("--workdir must be empty")
    # Isolate the synthetic store in a disposable database and a schema-scoped connection.
    name = "ps5_" + uuid4().hex[:12]
    with psycopg.connect(args.pg_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    parts = urlsplit(args.pg_url)
    pg_target = urlunsplit(parts._replace(path="/" + name))
    with psycopg.connect(pg_target, autocommit=True) as connection:
        connection.execute("CREATE SCHEMA atlas_comparison")
    pg_target += ("&" if urlsplit(pg_target).query else "?") + "options=" + quote("-csearch_path=atlas_comparison")
    try:
        pg = run_store("pg", pg_target, True, args.workdir / "pg")
    finally:
        with psycopg.connect(args.pg_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    fts = run_store("sqlite-fts", args.workdir / "fts" / "store.sqlite", True, args.workdir / "fts")
    like = run_store("sqlite-like", args.workdir / "like" / "store.sqlite", False, args.workdir / "like")
    rows = compare(pg, fts, "sqlite-fts") + compare(pg, like, "sqlite-like")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    summary = {
        "calls": len(pg), "comparisons": len(rows), "by_status": dict(sorted(counts.items())),
        "pg_exceptions": sorted(name for name, value in pg.items() if value[0] == "error"),
        "sqlite_fts_exceptions": sorted(name for name, value in fts.items() if value[0] == "error"),
        "sqlite_like_exceptions": sorted(name for name, value in like.items() if value[0] == "error"),
        "bulk": {"tasks": BULK_TASKS, "runs": BULK_TASKS * BULK_RUNS_PER_TASK},
    }
    notable = [row for row in rows if row["status"] not in {"equal"}]
    args.out.write_text(json.dumps({"summary": summary, "not_equal": notable, "all": rows}, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    for row in notable:
        print(row["status"], row["against"], row["call"], row["detail"][:160])
    return 0


if __name__ == "__main__":
    sys.exit(main())
