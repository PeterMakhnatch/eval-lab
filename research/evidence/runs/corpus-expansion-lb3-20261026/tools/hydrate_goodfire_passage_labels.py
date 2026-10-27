#!/usr/bin/env python3
"""LB-3: put the Goodfire passage-level judge labels on the exact steps (DEV only).

Authority: Peter Makhnatch's instruction recorded in CONTRACT.md section 4 (internal
research, agent building, detector benchmarking and trace retrieval on
``Goodfire/reward-hack-data``; tasks overlapping a test/held-out assignment stay ``held``).

What is already stored: ingestion (LB-1/LB-2) put every publisher passage record, minus
its quoted text, on the step it addresses as ``flags_json.source_passages``. Each record
carries the publisher's ``consensus`` (positive / ambiguous / negative). What is missing is
a flat, queryable mark on the step and a trace-level list of the exact marked steps and assistant-turn runs, so a
monitor or retriever does not have to interpret raw votes. This script derives them:

* step ``flags_json.passage_positive`` / ``passage_ambiguous`` (bool) and
  ``flags_json.passage_labels`` (counts), on every step that has at least one passage record;
* receipt ``verified_receipt_json.passage_step_labels`` (exact positive / ambiguous step indices,
  assistant-turn runs and counts), on every judged trace.

Nothing is inferred beyond ``consensus``. Before any write the derived per-trace counts must
equal the publisher's own trace-level ``n_passages``, ``n_passages_positive`` and
``n_passages_ambiguous``; a single mismatch aborts. Labels stay provisional publisher
three-vote LLM-judge source labels: ``status`` and ``is_exploit`` are never changed.

Only tasks that are DEV in the database *and* admitted by the production gate
(``admitted_cloud_task_ids`` over ``CorpusRegistry``, the function retrieval uses) are
touched. Held tasks are never read for content or written; a before/after fingerprint of
every other row proves it. The plan phase reads step flags (publisher metadata) and
receipts only, no trajectory text.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

DATASET_ID = "goodfire-reward-hack-data"
STEP_SCHEMA = "lb3.goodfire.passage_step_labels/1"
TRACE_SCHEMA = "lb3.goodfire.passage_trace_labels/1"
DERIVED_FROM = ("flags_json.source_passages[].consensus: publisher three-vote LLM-judge passage "
                "labels; provisional; derived by LB-3, not a replay confirmation")
STEP_KEYS = ("passage_positive", "passage_ambiguous", "passage_labels")
TRACE_KEY = "passage_step_labels"
TRACE_FIELDS = ("trace_id", "run_id", "task_id", "model_id", "outcome", "reward", "is_exploit",
                "is_task_defect_fail", "step_count", "raw_cas_uri", "verified_receipt_json")
STEP_FIELDS = ("trace_id", "step_index", "role", "tool_name", "command_or_action", "content_text",
               "observation_preview", "observation_cas_uri", "flags_json")
FTS_TABLES = ("corpus_tasks_fts", "task_issues_fts", "agent_traces_fts", "trace_steps_fts")
CONSENSUS = ("positive", "ambiguous", "negative")
LOCK_DEADLINE_SECONDS = 3600


class Abort(SystemExit):
    """A fail-closed refusal; nothing further is written."""


def log(message: str, **fields: Any) -> None:
    print(json.dumps({"t": datetime.now(UTC).isoformat(timespec="seconds"), "msg": message, **fields},
                     sort_keys=True), file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def retry_locked(function, *args, **kwargs):
    """Retry only SQLite lock contention (the file is a rollback-journal database other
    agents read for long stretches); every retried operation is an idempotent read or write."""
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
    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        attribute = getattr(self._target, name)
        if not callable(attribute):
            return attribute
        return lambda *args, **kwargs: retry_locked(attribute, *args, **kwargs)


# ---------------------------------------------------------------- pure derivation


def turn_runs(marked: list[int], assistant: list[int]) -> list[list[int]]:
    """Runs of consecutive assistant turns that are all marked, as [first, last, turns].

    Transcripts alternate assistant turns and tool results and every passage record sits on
    an assistant turn, so two marked steps are never adjacent by step index (measured: all
    10,681 positive steps are isolated by index). Adjacency is therefore taken over assistant
    turns. A marked step that is not an assistant turn is its own run.
    """
    ordinal = {index: position for position, index in enumerate(assistant)}
    runs: list[list[int]] = []
    previous: int | None = None
    for index in sorted(marked):
        position = ordinal.get(index)
        if runs and position is not None and previous is not None and position == previous + 1:
            runs[-1][1] = index
            runs[-1][2] += 1
        else:
            runs.append([index, index, 1])
        previous = position
    return runs


def step_labels(passages: list[dict[str, Any]], step_index: int) -> dict[str, Any]:
    """Flat marks for one step from its publisher passage records (fail closed)."""
    counts = Counter()
    positive_channels: set[str] = set()
    ambiguous_reasons: set[str] = set()
    votes: list[int] = []
    for passage in passages:
        if passage.get("node_index") != step_index:
            raise Abort(f"passage addresses node {passage.get('node_index')}, stored on step {step_index}")
        consensus = passage.get("consensus")
        if consensus not in CONSENSUS:
            raise Abort(f"unknown passage consensus {consensus!r}")
        counts[consensus] += 1
        if consensus == "positive":
            if isinstance(passage.get("channel"), str):
                positive_channels.add(passage["channel"])
            vote = passage.get("votes_positive_verified")
            if isinstance(vote, int) and not isinstance(vote, bool):
                votes.append(vote)
        elif consensus == "ambiguous" and isinstance(passage.get("ambiguous_reason"), str):
            ambiguous_reasons.update(passage["ambiguous_reason"].split(","))
    return {
        "passage_positive": counts["positive"] > 0,
        "passage_ambiguous": counts["ambiguous"] > 0,
        "passage_labels": {
            "schema": STEP_SCHEMA, "n_positive": counts["positive"], "n_ambiguous": counts["ambiguous"],
            "n_negative": counts["negative"], "positive_channels": sorted(positive_channels),
            "ambiguous_reasons": sorted(ambiguous_reasons),
            "positive_votes_max": max(votes) if votes else None,
        },
    }


def strip_step(flags: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in flags.items() if key not in STEP_KEYS}


def strip_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in receipt.items() if key != TRACE_KEY}


def derive_trace(trace_id: str, steps: list[tuple[int, str, dict[str, Any]]],
                 receipt: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[str, Any] | None]:
    """Return ({step_index: new flags}, receipt block) for one trace, or ({}, None) if unjudged.

    ``steps`` are (step_index, role, flags). The derived passage, positive and ambiguous
    counts must equal the publisher's own trace-level counts, otherwise the whole run aborts.
    """
    label = receipt.get("source_label") or {}
    if label.get("judged") is not True:
        return {}, None
    new_flags: dict[int, dict[str, Any]] = {}
    totals = Counter()
    positive_steps: list[int] = []
    ambiguous_steps: list[int] = []
    assistant = [index for index, role, _ in steps if role == "assistant"]
    non_assistant_marked = 0
    for step_index, role, flags in steps:
        passages = flags.get("source_passages")
        if not isinstance(passages, list):
            raise Abort(f"{trace_id} step {step_index}: source_passages is not a list")
        if not passages:
            continue
        marks = step_labels(passages, step_index)
        labels = marks["passage_labels"]
        totals["positive"] += labels["n_positive"]
        totals["ambiguous"] += labels["n_ambiguous"]
        totals["all"] += len(passages)
        if marks["passage_positive"]:
            positive_steps.append(step_index)
        if marks["passage_ambiguous"]:
            ambiguous_steps.append(step_index)
        if (marks["passage_positive"] or marks["passage_ambiguous"]) and role != "assistant":
            non_assistant_marked += 1
        new_flags[step_index] = strip_step(flags) | marks
    publisher = (label.get("n_passages"), label.get("n_passages_positive"), label.get("n_passages_ambiguous"))
    if publisher != (totals["all"], totals["positive"], totals["ambiguous"]):
        raise Abort(f"{trace_id}: derived passage counts {(totals['all'], totals['positive'], totals['ambiguous'])} "
                    f"differ from the publisher's {publisher}")
    block = {
        "schema": TRACE_SCHEMA, "derived_from": DERIVED_FROM, "judged": True,
        "passage_rows": totals["all"], "positive_passage_rows": totals["positive"],
        "ambiguous_passage_rows": totals["ambiguous"],
        "steps_with_passages": len(new_flags),
        "positive_steps": len(positive_steps), "ambiguous_steps": len(ambiguous_steps),
        "positive_step_indices": positive_steps, "ambiguous_step_indices": ambiguous_steps,
        "positive_turn_runs": turn_runs(positive_steps, assistant),
        "ambiguous_turn_runs": turn_runs(ambiguous_steps, assistant),
        "marked_steps_not_assistant": non_assistant_marked,
    }
    return new_flags, block


# ---------------------------------------------------------------- database access


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def admission(database: Path, repo_root: Path) -> tuple[frozenset[str], str, set[str], set[str]]:
    """Production gate over every stored identity; returns (admitted, ledger digest, dev ids, held ids)."""
    from evallab.production.cloud_trace_db import admitted_cloud_task_ids
    from evallab.production.corpus_queue import CorpusRegistry

    registry = CorpusRegistry(repo_root)
    if not registry.has_assignment_authority():
        raise Abort("no validated split ledger loaded; refusing to touch any row")
    connection = ro(database)
    identities = [
        dict(zip(("task_id", "dataset_id", "split", "original_grader_digest", "repaired_grader_digest",
                  "item_id", "item_type"), row, strict=True))
        for row in connection.execute(
            "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest, "
            "CASE WHEN json_valid(metadata_json) THEN json_extract(metadata_json, '$.item_id') END, "
            "CASE WHEN json_valid(metadata_json) THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END "
            "FROM corpus_tasks ORDER BY task_id")
    ]
    connection.close()
    admitted = admitted_cloud_task_ids(identities, registry=registry)
    goodfire = [row for row in identities if row["dataset_id"] == DATASET_ID]
    dev = {row["task_id"] for row in goodfire if row["split"] == "DEV"}
    held = {row["task_id"] for row in goodfire if row["split"] != "DEV"}
    return admitted, registry.split_assignments_digest, dev, held


def run_family(run_id: str) -> str:
    return run_id.rsplit("/", 1)[-1]


def plan(database: Path, repo_root: Path) -> dict[str, Any]:
    admitted, digest, dev, held = admission(database, repo_root)
    targets = sorted(dev & admitted)
    refused = sorted(dev - admitted)
    if refused:
        log("DEV rows not admitted by the production gate are left untouched", count=len(refused))
    connection = ro(database)
    totals: Counter = Counter()
    per_run: dict[str, Counter] = {}
    run_lengths: Counter = Counter()
    traces_planned: dict[str, dict[str, Any]] = {}
    for number, task_id in enumerate(targets, 1):
        for trace_id, run_id, receipt_text in connection.execute(
                "SELECT trace_id, run_id, verified_receipt_json FROM agent_traces WHERE task_id = ? ORDER BY trace_id",
                (task_id,)):
            receipt = json.loads(receipt_text)
            steps = [(index, role, json.loads(flags)) for index, role, flags in connection.execute(
                "SELECT step_index, role, flags_json FROM trace_steps WHERE trace_id = ? ORDER BY step_index",
                (trace_id,))]
            new_flags, block = derive_trace(trace_id, steps, receipt)
            bucket = per_run.setdefault(run_family(run_id), Counter())
            totals["traces"] += 1
            bucket["traces"] += 1
            if block is None:
                totals["traces_unjudged_left_untouched"] += 1
                bucket["traces_unjudged_left_untouched"] += 1
                continue
            positive_runs, ambiguous_runs = block["positive_turn_runs"], block["ambiguous_turn_runs"]
            for name, value in (
                    ("traces_judged", 1), ("traces_with_positive_step", bool(positive_runs)),
                    ("traces_with_ambiguous_step", bool(ambiguous_runs)),
                    ("steps_total", len(steps)), ("steps_with_passages", block["steps_with_passages"]),
                    ("passage_rows", block["passage_rows"]),
                    ("positive_passage_rows", block["positive_passage_rows"]),
                    ("ambiguous_passage_rows", block["ambiguous_passage_rows"]),
                    ("positive_steps", block["positive_steps"]), ("ambiguous_steps", block["ambiguous_steps"]),
                    ("positive_turn_runs", len(positive_runs)), ("ambiguous_turn_runs", len(ambiguous_runs)),
                    ("marked_steps_not_assistant", block["marked_steps_not_assistant"])):
                totals[name] += int(value)
                bucket[name] += int(value)
            for _first, _last, turns in positive_runs:
                run_lengths[turns] += 1
            traces_planned[trace_id] = {"steps_changed": len(new_flags)}
        if number % 200 == 0:
            log("planned", tasks=number, of=len(targets), traces=totals["traces"])
    connection.close()
    return {
        "assignment_ledger_sha256": digest, "dev_tasks_in_database": len(dev),
        "dev_tasks_admitted_and_targeted": len(targets), "dev_tasks_refused_by_gate": len(refused),
        "held_goodfire_tasks_not_touched": len(held),
        "totals": dict(sorted(totals.items())),
        "by_run": {run: dict(sorted(counter.items())) for run, counter in sorted(per_run.items())},
        "positive_turn_run_length_histogram": {str(k): v for k, v in sorted(run_lengths.items())},
        "target_task_ids": targets, "traces_planned": len(traces_planned),
    }


# ---------------------------------------------------------------- fingerprint, backup, integrity


def fingerprint(path: Path) -> dict[str, Any]:
    """Row-content fingerprint of every table with this script's additions removed."""
    connection = ro(path)
    result: dict[str, Any] = {}
    for table, query, transform in (
        ("corpus_tasks", "SELECT * FROM corpus_tasks ORDER BY task_id", None),
        ("task_issues", "SELECT * FROM task_issues ORDER BY issue_id", None),
        ("agent_traces", "SELECT trace_id, run_id, task_id, model_id, outcome, reward, is_exploit, "
                         "is_task_defect_fail, step_count, raw_cas_uri, verified_receipt_json FROM agent_traces "
                         "ORDER BY trace_id", ("receipt", 10)),
        ("trace_steps", "SELECT trace_id, step_index, role, tool_name, command_or_action, content_text, "
                        "observation_preview, observation_cas_uri, flags_json FROM trace_steps "
                        "ORDER BY trace_id, step_index", ("flags", 8)),
    ):
        digest, count = hashlib.sha256(), 0
        for row in connection.execute(query):
            if transform is not None:
                kind, column = transform
                row = list(row)
                parsed = None if row[column] is None else json.loads(row[column])
                if isinstance(parsed, dict):
                    row[column] = canon(strip_receipt(parsed) if kind == "receipt" else strip_step(parsed))
            digest.update(canon(list(row)).encode())
            count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    connection.close()
    return result


def backup(source: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        check = ro(destination)
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        check.close()
        if ok != "ok":
            raise Abort(f"existing backup {destination} failed its integrity check")
        return {"path": str(destination), "bytes": destination.stat().st_size, "integrity_check": ok,
                "reused_existing_pre_write_backup": True}
    destination.parent.mkdir(parents=True, exist_ok=True)
    reader, writer = ro(source), sqlite3.connect(destination)
    reader.backup(writer)
    writer.close()
    reader.close()
    check = ro(destination)
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


# ---------------------------------------------------------------- apply and verify


def verify(database: Path, planned: dict[str, Any]) -> dict[str, Any]:
    """Independent read-only recount from the stored rows against the plan."""
    connection = ro(database)
    targets = planned["target_task_ids"]
    totals: Counter = Counter()
    for task_id in targets:
        for trace_id, receipt_text in connection.execute(
                "SELECT trace_id, verified_receipt_json FROM agent_traces WHERE task_id = ?", (task_id,)):
            receipt = json.loads(receipt_text)
            block = receipt.get(TRACE_KEY)
            label = receipt.get("source_label") or {}
            if label.get("judged") is not True:
                if block is not None:
                    raise Abort(f"{trace_id}: unjudged trace carries a derived block")
                continue
            if block is None or block["schema"] != TRACE_SCHEMA:
                raise Abort(f"{trace_id}: judged trace is missing its derived block")
            positives = ambiguous = 0
            positive_steps: list[int] = []
            ambiguous_steps: list[int] = []
            roles: list[tuple[int, str]] = []
            for index, role, flags_text in connection.execute(
                    "SELECT step_index, role, flags_json FROM trace_steps WHERE trace_id = ? ORDER BY step_index",
                    (trace_id,)):
                roles.append((index, role))
                flags = json.loads(flags_text)
                has_marks = "passage_positive" in flags
                if has_marks != bool(flags.get("source_passages")):
                    raise Abort(f"{trace_id} step {index}: marks and passage records disagree")
                if not has_marks:
                    continue
                labels = flags["passage_labels"]
                positives += labels["n_positive"]
                ambiguous += labels["n_ambiguous"]
                if flags["passage_positive"] != (labels["n_positive"] > 0):
                    raise Abort(f"{trace_id} step {index}: passage_positive disagrees with its count")
                if flags["passage_ambiguous"] != (labels["n_ambiguous"] > 0):
                    raise Abort(f"{trace_id} step {index}: passage_ambiguous disagrees with its count")
                if flags["passage_positive"]:
                    positive_steps.append(index)
                if flags["passage_ambiguous"]:
                    ambiguous_steps.append(index)
            if (positives, ambiguous) != (label["n_passages_positive"], label["n_passages_ambiguous"]):
                raise Abort(f"{trace_id}: stored step counts differ from the publisher's trace counts")
            assistant = [index for index, role in roles if role == "assistant"]
            if (positive_steps != block["positive_step_indices"] or ambiguous_steps != block["ambiguous_step_indices"]
                    or turn_runs(positive_steps, assistant) != block["positive_turn_runs"]
                    or turn_runs(ambiguous_steps, assistant) != block["ambiguous_turn_runs"]):
                raise Abort(f"{trace_id}: receipt step lists or runs differ from the marked steps")
            totals["traces_judged"] += 1
            totals["traces_with_positive_step"] += bool(positive_steps)
            totals["positive_steps"] += len(positive_steps)
            totals["ambiguous_steps"] += len(ambiguous_steps)
            totals["positive_turn_runs"] += len(block["positive_turn_runs"])
            totals["ambiguous_turn_runs"] += len(block["ambiguous_turn_runs"])
    marked_elsewhere = connection.execute(
        "SELECT count(*) FROM trace_steps s JOIN agent_traces a USING(trace_id) JOIN corpus_tasks c USING(task_id) "
        "WHERE c.dataset_id = ? AND c.split != 'DEV' AND json_extract(s.flags_json, '$.passage_positive') IS NOT NULL",
        (DATASET_ID,)).fetchone()[0]
    outside = connection.execute(
        "SELECT count(*) FROM trace_steps s JOIN agent_traces a USING(trace_id) JOIN corpus_tasks c USING(task_id) "
        "WHERE c.dataset_id != ? AND json_extract(s.flags_json, '$.passage_positive') IS NOT NULL",
        (DATASET_ID,)).fetchone()[0]
    connection.close()
    if marked_elsewhere or outside:
        raise Abort("marks found on held Goodfire rows or on another dataset")
    for name in ("traces_judged", "traces_with_positive_step", "positive_steps", "ambiguous_steps",
                 "positive_turn_runs", "ambiguous_turn_runs"):
        if totals[name] != planned["totals"].get(name, 0):
            raise Abort(f"re-read {name}={totals[name]} differs from the plan {planned['totals'].get(name, 0)}")
    return {"re_read": dict(sorted(totals.items())), "marks_on_held_goodfire_rows": marked_elsewhere,
            "marks_on_other_datasets": outside}


def apply(args: argparse.Namespace, planned: dict[str, Any]) -> dict[str, Any]:
    from evallab.production.cloud_trace_db import CloudTraceDatabase

    before = fingerprint(args.database)
    log("fingerprint before", **{k: v["rows"] for k, v in before.items()})
    backup_info = backup(args.database, args.backup)
    log("backup complete", **backup_info)
    written = skipped = changed_steps = 0
    with retry_locked(CloudTraceDatabase, args.database) as database:
        prod = Patient(database)
        counts_before = prod.row_counts()
        for number, task_id in enumerate(planned["target_task_ids"], 1):
            task = prod.get_task(task_id)
            if task["split"] != "DEV" or task["dataset_id"] != DATASET_ID:
                raise Abort("a target is no longer a DEV Goodfire task")
            for existing in database_trace_ids(args.database, task_id):
                trace = prod.get_trace(existing)
                receipt = trace["verified_receipt_json"]
                steps = [(step["step_index"], step["role"], step["flags_json"]) for step in trace["steps"]]
                stripped = [(index, role, strip_step(flags)) for index, role, flags in steps]
                new_flags, block = derive_trace(existing, stripped, strip_receipt(receipt))
                if block is None:
                    skipped += 1
                    continue
                row = {field: copy.deepcopy(trace[field]) for field in TRACE_FIELDS}
                row["verified_receipt_json"] = strip_receipt(receipt) | {TRACE_KEY: block}
                new_steps = [
                    {field: step[field] for field in STEP_FIELDS}
                    | {"flags_json": new_flags.get(step["step_index"], strip_step(step["flags_json"]))}
                    for step in trace["steps"]
                ]
                if row["verified_receipt_json"] == receipt and all(
                        new["flags_json"] == old["flags_json"] for new, old in zip(new_steps, trace["steps"], strict=True)):
                    skipped += 1
                    continue
                if row["verified_receipt_json"].get("status") != "provisional":
                    raise Abort(f"{existing}: receipt status is not provisional")
                prod.upsert_trace(row, steps=new_steps)
                again = prod.get_trace(existing)
                if again["verified_receipt_json"] != row["verified_receipt_json"] or [
                        step["flags_json"] for step in again["steps"]] != [step["flags_json"] for step in new_steps]:
                    raise Abort(f"{existing}: read-back differs from the rows written")
                written += 1
                changed_steps += len(new_flags)
            if number % 100 == 0:
                log("tasks done", done=number, of=len(planned["target_task_ids"]), traces_written=written)
        counts_after = prod.row_counts()
    after = fingerprint(args.database)
    if before != after:
        raise Abort("a row outside this script's additions changed: " + canon(
            {t: [before[t], after[t]] for t in before if before[t] != after[t]}))
    return {"backup": backup_info, "row_counts_before": counts_before, "row_counts_after": counts_after,
            "traces_written": written, "traces_skipped_unjudged_or_already_current": skipped,
            "steps_marked": changed_steps, "fingerprint_before": before, "fingerprint_after_stripped": after,
            "integrity": integrity(args.database)}


def database_trace_ids(database: Path, task_id: str) -> list[str]:
    connection = ro(database)
    ids = [row[0] for row in connection.execute(
        "SELECT trace_id FROM agent_traces WHERE task_id = ? ORDER BY trace_id", (task_id,))]
    connection.close()
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True,
                        help="checkout holding the split ledger (read only through the production gate)")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--backup", type=Path, help="pre-write backup path (required with --apply)")
    parser.add_argument("--apply", action="store_true", help="write; the default only plans")
    args = parser.parse_args()
    if args.receipt.exists():
        raise Abort(f"{args.receipt} exists; refusing to overwrite a receipt")
    if args.apply and args.backup is None:
        raise Abort("--apply needs --backup")
    started = time.monotonic()
    planned = plan(args.database, args.repo_root)
    log("plan complete", **planned["totals"])
    receipt: dict[str, Any] = {
        "schema": "lb3.goodfire.passage_hydration_receipt/1",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "script_sha256": sha256_file(Path(__file__)), "database": str(args.database),
        "mode": "apply" if args.apply else "plan_only",
        "plan": {k: v for k, v in planned.items() if k != "target_task_ids"},
        "model_calls": 0, "docker_runs": 0, "network_requests": 0, "cas_writes": 0,
        "trace_text_printed_or_stored": False,
    }
    if args.apply:
        receipt["apply"] = apply(args, planned)
        receipt["verify"] = verify(args.database, planned)
    receipt["seconds"] = round(time.monotonic() - started, 1)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, indent=1, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"mode": receipt["mode"], "totals": planned["totals"],
                      "positive_turn_run_length_histogram": planned["positive_turn_run_length_histogram"]},
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
