#!/usr/bin/env python3
"""LB-2: ingest the three Goodfire DeepSWE judge runs under the DEV/held split firewall.

Authority: Peter Makhnatch's direct instruction recorded in CONTRACT.md section 4
(internal research, agent building, detector benchmarking and trace retrieval on
``Goodfire/reward-hack-data``; tasks overlapping any test/held-out split stay ``held``).

Pipeline
1. ``decode``: the byte-pinned LB-1 Goodfire normalizer decodes the three runs from the
   already-fetched, hash-verified source files into a private staging shard + staging CAS.
   Nothing touches production in this phase.
2. ``plan``: the production admission gate (``admitted_cloud_task_ids`` over
   ``CorpusRegistry``, the exact function retrieval and actor projection use) partitions
   the native tasks into DEV and held. Only counts are reported; no sealed identity is
   printed or written to any receipt.
3. ``apply`` (only with ``--apply``): consistent backup, then idempotent per-task/per-trace
   writes into the production database and CAS, then an independent re-read of every new
   trace (native nodes reconstructed from the raw CAS line), SQLite/FTS integrity checks,
   a before/after fingerprint of every pre-existing row, and a before/after admission diff.

Labels stay what the publisher released: provisional three-vote LLM-judge source labels.
``is_exploit`` mirrors the existing Goodfire DEV convention (DEV rows only, set exactly when
the publisher's ``verdict_enacted`` is ``yes``); it is not a replay confirmation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

HERE = Path(__file__).resolve().parent
NORMALIZER = HERE.parents[1] / "corpus-expansion-lb1-20261025/tools/goodfire_normalize_v3.py"
NORMALIZER_SHA256 = "79538cd27eed1f924608f4477ea31b04e0135d7501716ccaed64aa6652e67616"
REVISION = "bd8fb473568b8ef1d12fc2325a5c8af939c15ad9"
DATASET_ID = "goodfire-reward-hack-data"
RUNS = ("deepswe-glm-5.2-fp8", "deepswe-kimi-k3", "deepswe-qwen3.8-2.4t-a95b-fp8")
EXPECTED_NATIVE_ROWS = {"deepswe-glm-5.2-fp8": 562, "deepswe-kimi-k3": 520,
                        "deepswe-qwen3.8-2.4t-a95b-fp8": 452}
OWNER_AUTHORIZATION = {
    "authorized_at": "2026-10-25T17:55:00Z",
    "authorized_by": "Peter Makhnatch",
    "redistribution": "not_permitted",
    "scope": "internal_research_agent_retrieval_and_trace_analysis",
}
TASK_FIELDS = ("task_id", "dataset_id", "split", "instruction_text", "original_grader_digest",
               "repaired_grader_digest", "cas_uri", "metadata_json")
TRACE_FIELDS = ("trace_id", "run_id", "task_id", "model_id", "outcome", "reward", "is_exploit",
                "is_task_defect_fail", "step_count", "raw_cas_uri", "verified_receipt_json")
STEP_FIELDS = ("trace_id", "step_index", "role", "tool_name", "command_or_action", "content_text",
               "observation_preview", "observation_cas_uri", "flags_json")
FTS_TABLES = ("corpus_tasks_fts", "task_issues_fts", "agent_traces_fts", "trace_steps_fts")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_normalizer():
    if sha256_file(NORMALIZER) != NORMALIZER_SHA256:
        raise SystemExit("archived Goodfire normalizer bytes differ from the pinned SHA-256")
    spec = importlib.util.spec_from_file_location("lb1_goodfire_normalizer", NORMALIZER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["lb1_goodfire_normalizer"] = module
    spec.loader.exec_module(module)
    if (module.REVISION, module.DATASET_ID) != (REVISION, DATASET_ID):
        raise SystemExit("normalizer pins a different dataset revision")
    return module


def log(message: str, **fields: Any) -> None:
    print(json.dumps({"t": datetime.now(UTC).isoformat(timespec="seconds"), "msg": message, **fields},
                     sort_keys=True), file=sys.stderr, flush=True)


LOCK_DEADLINE_SECONDS = 3600


def retry_locked(function, *args, **kwargs):
    """Retry only SQLite lock contention.

    The production file is a rollback-journal database that other agents read for long
    stretches, so a writer's commit can wait behind a reader. Every operation retried here
    is an idempotent upsert or a read, and ``CloudTraceDatabase`` rolls back a failed
    transaction before re-raising, so a retry never observes a half-written trace.
    """
    started, delay = time.monotonic(), 0.5
    while True:
        try:
            return function(*args, **kwargs)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or time.monotonic() - started > LOCK_DEADLINE_SECONDS:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 15.0)


class Patient:
    """Route every database method call through ``retry_locked``."""

    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        attribute = getattr(self._target, name)
        if not callable(attribute):
            return attribute
        return lambda *args, **kwargs: retry_locked(attribute, *args, **kwargs)


class StageWriter:
    """Shard writer with the invariants the LB-1 importer asserted on every row."""

    def __init__(self, database):
        self.db = database
        self.traces: set[str] = set()
        self.steps = 0

    def upsert_task(self, task):
        assert task["split"] == "held" and task["dataset_id"] == DATASET_ID
        self.db.upsert_task(task)

    def upsert_trace(self, trace, *, steps):
        assert trace["trace_id"] not in self.traces
        assert trace["model_id"].startswith("source-reported:")
        assert trace["verified_receipt_json"]["status"] == "provisional"
        assert trace["is_exploit"] is False and trace["is_task_defect_fail"] is False
        assert trace["step_count"] == len(steps)
        self.db.upsert_trace(trace, steps=steps)
        self.traces.add(trace["trace_id"])
        self.steps += len(steps)


def decode(args, normalizer) -> dict[str, Any]:
    """Phase 1: byte-verified decode into a private staging shard and CAS."""
    from evallab.production.cloud_trace_db import CloudTraceDatabase, S3CompatibleCAS

    stage: Path = args.stage_dir
    receipt_path = stage / "decode-receipt.json"
    if receipt_path.exists():
        log("decode already complete; reusing immutable stage", receipt=str(receipt_path))
        return json.loads(receipt_path.read_text())
    if stage.exists() and any(stage.iterdir()):
        raise SystemExit(f"{stage} exists without a decode receipt; refusing to reuse a partial stage")
    stage.mkdir(parents=True, exist_ok=True)
    fetch: dict[str, dict] = {}
    for path in args.fetch_receipt:
        for entry in json.loads(path.read_text())["files"]:
            fetch[entry["path"]] = entry
    raw_root = args.raw_root / REVISION
    cas = S3CompatibleCAS(local_directory=stage / "cas")
    handoff_dir = stage / "handoff"
    handoff_dir.mkdir()
    tasks: dict[str, dict] = {}
    results = []
    with CloudTraceDatabase(stage / "shard.sqlite", sqlite_fts=False) as shard:
        writer = StageWriter(shard)
        for run in RUNS:
            started = time.monotonic()
            result = normalizer.decode_run(run, raw_root, cas, fetch, stage, writer, tasks, handoff_dir)
            if result["native_rows"] != EXPECTED_NATIVE_ROWS[run]:
                raise SystemExit(f"{run}: {result['native_rows']} rows, expected {EXPECTED_NATIVE_ROWS[run]}")
            result["seconds"] = round(time.monotonic() - started, 1)
            results.append(result)
            log("decoded run", run=run, rows=result["native_rows"], steps=result["steps"],
                seconds=result["seconds"])
        for task in tasks.values():  # final per-task run lists
            writer.upsert_task(task)
    manifests = {}
    for run in RUNS:
        manifest = json.loads((raw_root / run / "manifest.json").read_text())
        markers = sorted(name for name in ("_PARTIAL", "_SUCCESS") if (raw_root / run / name).exists())
        manifests[run] = {
            "publisher_manifest_sha256": sha256_file(raw_root / run / "manifest.json"),
            "publisher_complete_flag": manifest.get("complete"),
            "publisher_rows": manifest.get("rows"),
            "publisher_expected_episodes": manifest.get("expected_episodes"),
            "publisher_errored_rows": manifest.get("errored_rows"),
            "publisher_marker_files": markers,
        }
    summary = {
        "schema": "lb2.goodfire.deepswe_decode_receipt/1",
        "repo": "Goodfire/reward-hack-data", "revision": REVISION,
        "normalizer_sha256": NORMALIZER_SHA256, "ingest_script_sha256": sha256_file(Path(__file__)),
        "fetch_receipts": {str(path): sha256_file(path) for path in args.fetch_receipt},
        "source_files_sha256": {
            f"{run}/{name}": fetch[f"{run}/{name}"]["sha256"]
            for run in RUNS for name in ("traces.jsonl", "labels/judge_rollouts.jsonl", "labels/judge_passages.jsonl")
        },
        "runs": [{k: v for k, v in result.items() if k != "files_sha256"} for result in results],
        "publisher_manifest_facts": manifests,
        "native_rows": sum(r["native_rows"] for r in results), "steps": sum(r["steps"] for r in results),
        "distinct_native_tasks": len(tasks), "shard_sha256": sha256_file(stage / "shard.sqlite"),
        "json_decoded_by_this_phase": True, "network_requests": 0,
    }
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=1, sort_keys=True)
    return summary


def admission_gate(repo_root: Path, task_ids: list[str]):
    """Production gate: which of these (otherwise DEV-eligible) tasks may be DEV."""
    from evallab.production.cloud_trace_db import admitted_cloud_task_ids
    from evallab.production.corpus_queue import CorpusRegistry

    registry = CorpusRegistry(repo_root)
    if not registry.has_assignment_authority():
        raise SystemExit("no validated split ledger loaded; refusing to assign any DEV split")
    identities = [{"task_id": task_id, "dataset_id": DATASET_ID, "split": "DEV",
                   "original_grader_digest": None, "repaired_grader_digest": None,
                   "item_id": None, "item_type": None} for task_id in task_ids]
    return admitted_cloud_task_ids(identities, registry=registry), registry.split_assignments_digest


def admission_block(split: str, digest: str) -> dict[str, Any]:
    clearance = (
        f"admitted_cloud_task_ids(CorpusRegistry, split ledger {digest}) at LB-2 ingestion; "
        f"{'admitted' if split == 'DEV' else 'denied (overlaps a test/held-out assignment or seal, or lacks authority)'}"
    )
    block = {
        "actor_exclusion": "not_enforced_by_CloudTraceDatabase; keep staged until retrieval filters held/rights rows",
        "canonical_alias_clearance": clearance,
        "effective_split": split, "source_split": None,
        "training_admission": "not_established_by_retained_evidence",
    }
    if split == "DEV":
        block["owner_authorization"] = copy.deepcopy(OWNER_AUTHORIZATION)
    return block


def rights_block(split: str) -> dict[str, Any]:
    block = {"declared_license": None, "redistribution": "not_permitted_pending_decision",
             "underlying_benchmark_and_model_terms": "not_audited"}
    if split == "DEV":
        block["owner_authorization"] = copy.deepcopy(OWNER_AUTHORIZATION)
        block["rights_status"] = "ALLOW_internal_research_owner_authorized"
    else:
        block["rights_status"] = "HOLD_unresolved_no_license_declared"
    return block


def split_task(task: dict[str, Any], dev_ids: frozenset[str], digest: str) -> dict[str, Any]:
    row = copy.deepcopy(task)
    split = "DEV" if row["task_id"] in dev_ids else "held"
    row["split"] = split
    row["metadata_json"]["admission"] = admission_block(split, digest)
    row["metadata_json"]["rights"] = rights_block(split)
    return row


def split_trace(trace: dict[str, Any], split: str, digest: str) -> dict[str, Any]:
    row = {field: copy.deepcopy(trace[field]) for field in TRACE_FIELDS}
    receipt = row["verified_receipt_json"]
    receipt["canonical_admission"] = split
    receipt["admission"] = admission_block(split, digest)
    receipt["rights"] = rights_block(split)
    verdict = (receipt.get("source_label") or {}).get("verdict_enacted")
    row["is_exploit"] = split == "DEV" and verdict == "yes"
    return row


def stage_rows(stage: Path):
    """Read tasks/traces back out of the shard through the public API (no SQL on content)."""
    from evallab.production.cloud_trace_db import CloudTraceDatabase

    connection = sqlite3.connect(f"file:{stage / 'shard.sqlite'}?mode=ro", uri=True)
    task_ids = [r[0] for r in connection.execute("SELECT task_id FROM corpus_tasks ORDER BY task_id")]
    trace_ids = [r[0] for r in connection.execute("SELECT trace_id FROM agent_traces ORDER BY trace_id")]
    connection.close()
    return task_ids, trace_ids, CloudTraceDatabase(stage / "shard.sqlite", sqlite_fts=False)


def plan(args) -> dict[str, Any]:
    task_ids, trace_ids, shard = stage_rows(args.stage_dir)
    with shard:
        tasks = {task_id: shard.get_task(task_id) for task_id in task_ids}
        admitted, digest = admission_gate(args.repo_root, task_ids)
        counts = {"DEV": {"tasks": 0, "traces": 0, "steps": 0, "enacted_yes": 0, "is_exploit": 0},
                  "held": {"tasks": 0, "traces": 0, "steps": 0, "enacted_yes": 0, "is_exploit": 0}}
        per_run: dict[str, dict[str, int]] = {run: {"DEV": 0, "held": 0} for run in RUNS}
        for task in tasks.values():
            counts["DEV" if task["task_id"] in admitted else "held"]["tasks"] += 1
        for trace_id in trace_ids:
            trace = shard.get_trace(trace_id)
            split = "DEV" if trace["task_id"] in admitted else "held"
            row = split_trace(trace, split, digest)
            bucket = counts[split]
            bucket["traces"] += 1
            bucket["steps"] += len(trace["steps"])
            bucket["enacted_yes"] += row["verified_receipt_json"]["source_label"].get("verdict_enacted") == "yes"
            bucket["is_exploit"] += row["is_exploit"]
            per_run[row["run_id"].rsplit("/", 1)[1]][split] += 1
    return {"assignment_ledger_sha256": digest, "totals": counts, "traces_by_run_and_split": per_run,
            "admitted_task_ids": sorted(admitted)}


def fingerprint(path: Path, exclude_traces: set[str], exclude_tasks: set[str]) -> dict[str, Any]:
    """Row-content fingerprint of everything except the rows this run adds."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    result: dict[str, Any] = {}
    queries = {
        "corpus_tasks": ("SELECT task_id, dataset_id, split, instruction_text, original_grader_digest, "
                         "repaired_grader_digest, cas_uri, metadata_json FROM corpus_tasks ORDER BY task_id", 0, exclude_tasks),
        "task_issues": ("SELECT * FROM task_issues ORDER BY issue_id", None, set()),
        "agent_traces": ("SELECT * FROM agent_traces ORDER BY trace_id", 0, exclude_traces),
        "trace_steps": ("SELECT trace_id, step_index, role, tool_name, command_or_action, content_text, "
                        "observation_preview, observation_cas_uri, flags_json FROM trace_steps "
                        "ORDER BY trace_id, step_index", 0, exclude_traces),
    }
    for table, (query, key_index, excluded) in queries.items():
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(query):
            if key_index is not None and row[key_index] in excluded:
                continue
            digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    connection.close()
    return result


def backup(source: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        # A backup is taken before the first production write; a resumed run keeps it.
        check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        check.close()
        if ok != "ok":
            raise SystemExit(f"existing backup {destination} failed its integrity check")
        return {"path": str(destination), "bytes": destination.stat().st_size, "integrity_check": ok,
                "reused_existing_pre_write_backup": True}
    destination.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    writer = sqlite3.connect(destination)
    reader.backup(writer)
    writer.close()
    reader.close()
    check = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    check.close()
    return {"path": str(destination), "bytes": destination.stat().st_size, "integrity_check": ok}


def integrity(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(path, timeout=60)
    out = {"integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
           "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall())}
    for table in FTS_TABLES:
        retry_locked(connection.execute, f"INSERT INTO {table}({table}) VALUES('integrity-check')")
        out[table] = "ok"
    connection.close()
    return out


def apply(args, normalizer, planned: dict[str, Any], decode_receipt: dict[str, Any]) -> dict[str, Any]:
    from evallab.production.cloud_trace_db import (
        CloudTraceDatabase, S3CompatibleCAS, admitted_cloud_task_ids,
    )
    from evallab.production.corpus_queue import CorpusRegistry

    digest = planned["assignment_ledger_sha256"]
    admitted = frozenset(planned["admitted_task_ids"])
    task_ids, trace_ids, shard = stage_rows(args.stage_dir)
    prod_cas = S3CompatibleCAS(local_directory=args.cas_dir)
    stage_cas = S3CompatibleCAS(local_directory=args.stage_dir / "cas")
    new_tasks, new_traces = set(task_ids), set(trace_ids)
    before_fp = fingerprint(args.database, new_traces, new_tasks)
    backup_info = backup(args.database, args.stage_dir / "backup" / "cloud-trace-db.pre-lb2.sqlite")
    log("backup complete", **backup_info)
    with retry_locked(CloudTraceDatabase, args.database) as database, shard:
        prod = Patient(database)
        counts_before = prod.row_counts()
        written_tasks = written_traces = skipped_tasks = skipped_traces = 0
        for task_id in task_ids:
            row = split_task(shard.get_task(task_id), admitted, digest)
            row = {field: row[field] for field in TASK_FIELDS}
            existing = prod.get_task(task_id)
            if existing is not None:
                if existing != row:
                    raise SystemExit("refusing to replace a pre-existing, different task row")
                skipped_tasks += 1
                continue
            prod.upsert_task(row)
            if prod.get_task(task_id) != row:
                raise SystemExit("task read-back differs from the row written")
            written_tasks += 1
        # Objects are immutable and content-addressed; copy every staged object once.
        copied = 0
        for path in sorted((args.stage_dir / "cas" / "sha256").iterdir()):
            uri = f"cas://sha256/{path.name}"
            data = stage_cas.get_bytes(uri)
            if prod_cas.put_bytes(data) != uri:
                raise SystemExit("CAS copy changed an object identity")
            copied += 1
        log("cas copied", objects=copied)
        for number, trace_id in enumerate(trace_ids, 1):
            source = shard.get_trace(trace_id)
            split = "DEV" if source["task_id"] in admitted else "held"
            row = split_trace(source, split, digest)
            steps = [{field: step[field] for field in STEP_FIELDS} for step in source["steps"]]
            existing = prod.get_trace(trace_id)
            if existing is not None:
                if {k: existing[k] for k in TRACE_FIELDS} != row or existing["steps"] != source["steps"]:
                    raise SystemExit("refusing to replace a pre-existing, different trace row")
                skipped_traces += 1
                continue
            prod.upsert_trace(row, steps=steps)
            written_traces += 1
            if number % 100 == 0:
                log("traces written", done=number, total=len(trace_ids))
        counts_after = prod.row_counts()

        verified_nodes = 0
        by_split = {"DEV": {"tasks": 0, "traces": 0, "steps": 0, "is_exploit": 0},
                    "held": {"tasks": 0, "traces": 0, "steps": 0, "is_exploit": 0}}
        for task_id in task_ids:
            by_split["DEV" if task_id in admitted else "held"]["tasks"] += 1
            stored = prod.get_task(task_id)
            assert stored["split"] == ("DEV" if task_id in admitted else "held")
            prod_cas.get_bytes(stored["cas_uri"])
        for trace_id in trace_ids:
            source = shard.get_trace(trace_id)
            split = "DEV" if source["task_id"] in admitted else "held"
            expected = split_trace(source, split, digest)
            back = prod.get_trace(trace_id)
            assert {k: back[k] for k in TRACE_FIELDS} == expected, "trace row differs after write"
            assert back["steps"] == source["steps"] and back["issues"] == []
            receipt = back["verified_receipt_json"]
            raw = prod_cas.get_bytes(back["raw_cas_uri"])
            if hashlib.sha256(raw).hexdigest() != receipt["native_line"]["sha256_including_terminator"]:
                raise SystemExit("raw CAS line hash differs from the receipt")
            nodes = json.loads(raw)["traces"][0]["nodes"]
            assert len(nodes) == len(back["steps"])
            for step, node in zip(back["steps"], nodes, strict=True):
                assert normalizer.reconstruct(step) == node
                verified_nodes += 1
            prod_cas.get_bytes(receipt["label_row_cas_uri"])
            if receipt["passages"]["cas_uri"]:
                prod_cas.get_bytes(receipt["passages"]["cas_uri"])
            bucket = by_split[split]
            bucket["traces"] += 1
            bucket["steps"] += len(back["steps"])
            bucket["is_exploit"] += back["is_exploit"]
        registry = CorpusRegistry(args.repo_root)
        all_identities = prod._rows(
            "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest, "
            "json_extract(metadata_json, '$.item_id') AS item_id, "
            "CASE WHEN json_valid(metadata_json) THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type "
            "FROM corpus_tasks ORDER BY task_id"
        )
        admitted_after = admitted_cloud_task_ids(all_identities, registry=registry)
    after_fp = fingerprint(args.database, new_traces, new_tasks)
    unchanged = before_fp == after_fp
    new_dev_ids = {t for t in task_ids if t in admitted}
    delta = {table: counts_after[table] - counts_before[table] for table in counts_before}
    return {
        "backup": backup_info, "row_counts_before": counts_before, "row_counts_after": counts_after,
        "row_count_delta": delta, "tasks_written": written_tasks, "tasks_already_present": skipped_tasks,
        "traces_written": written_traces, "traces_already_present": skipped_traces,
        "cas_objects_copied_or_confirmed": copied, "by_split": by_split,
        "native_nodes_reconstructed_from_raw_cas": verified_nodes,
        "preexisting_rows_fingerprint_before": before_fp, "preexisting_rows_fingerprint_after": after_fp,
        "preexisting_rows_unchanged": unchanged,
        "new_dev_tasks_admitted_by_gate": len(new_dev_ids & admitted_after),
        "new_held_tasks_admitted_by_gate": len((new_tasks - new_dev_ids) & admitted_after),
        "database_integrity": integrity(args.database),
    }


def sensitivity_probe(repo_root: Path) -> dict[str, Any]:
    """Prove the gate matches DeepSWE-form identities by bare name.

    Every sealed text identity is wrapped in the exact id shape this run writes
    (``dataset/env/namespace/<name>@task-bundle-sha256-<hex>``). A gate that cannot see
    sealed names through that shape would admit all of them. Only counts are reported.
    """
    from evallab.production.cloud_trace_db import admitted_cloud_task_ids
    from evallab.production.corpus_queue import CorpusRegistry

    registry = CorpusRegistry(repo_root)
    sealed = sorted(registry.sealed_identifiers())
    probes = [f"{DATASET_ID}/deep_swe/datacurve/{name}@task-bundle-sha256-0123456789abcdef"
              for name in sealed if name]
    identities = [{"task_id": task_id, "dataset_id": DATASET_ID, "split": "DEV",
                   "original_grader_digest": None, "repaired_grader_digest": None,
                   "item_id": None, "item_type": None} for task_id in probes]
    leaked = admitted_cloud_task_ids(identities, registry=registry)
    return {"sealed_text_identities_probed": len(probes), "probe_ids_admitted_by_gate": len(leaked)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True, help="directory containing <revision>/<run>/...")
    parser.add_argument("--fetch-receipt", type=Path, action="append", required=True)
    parser.add_argument("--repo-root", type=Path, required=True, help="eval-lab checkout holding the split ledger")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cas-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write production; omit for decode+plan only")
    args = parser.parse_args()
    if args.receipt.exists():
        raise SystemExit(f"refusing to overwrite {args.receipt}")
    normalizer = load_normalizer()
    decode_receipt = decode(args, normalizer)
    planned = plan(args)
    log("plan", totals=planned["totals"], per_run=planned["traces_by_run_and_split"])
    probe = sensitivity_probe(args.repo_root)
    log("gate sensitivity", **probe)
    if probe["sealed_text_identities_probed"] == 0 or probe["probe_ids_admitted_by_gate"] != 0:
        raise SystemExit("admission gate failed its sealed-name sensitivity probe; refusing to continue")
    receipt: dict[str, Any] = {
        "schema": "lb2.goodfire.deepswe_ingestion_receipt/1", "created_at": datetime.now(UTC).isoformat(),
        "authority": "CONTRACT.md section 4 (Peter Makhnatch direct instruction, 2026-10-25 afternoon)",
        "ingest_script_sha256": sha256_file(Path(__file__)), "normalizer_sha256": NORMALIZER_SHA256,
        "eval_lab_commit": subprocess.check_output(
            ["git", "-C", str(args.repo_root), "rev-parse", "HEAD"], text=True).strip(),
        "decode": decode_receipt,
        "plan": {k: v for k, v in planned.items() if k != "admitted_task_ids"},
        "gate_sensitivity_probe": probe,
        "applied": False, "sealed_identities_in_receipt": 0,
        "model_calls": 0, "docker_runs": 0, "network_requests": 0,
    }
    if args.apply:
        receipt["apply"] = apply(args, normalizer, planned, decode_receipt)
        receipt["applied"] = True
    with args.receipt.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=1, sort_keys=True)
    print(json.dumps({k: receipt[k] for k in ("applied", "plan")}, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
