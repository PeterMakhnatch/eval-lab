"""Replay every admitted Goodfire DEV trace through the model-free production monitor.

The conversion preserves CM-2 ``lib.load_steps`` semantics; replay preserves CM-9
``stage1.replay`` event order and worker defaults, without building unused judge
windows. Neither retained bridge module is imported. Only projected native tool
calls, reasoning and tool-call IDs are read from flags_json, never publisher labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production import window_cascade
from evallab.production.cloud_trace_db import admitted_cloud_task_ids
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.monitor_worker import MisalignmentLiveWorker

REPO_ROOT = Path(__file__).resolve().parents[5]
STATE_ROOT = Path.home() / ".local/state/evallab-prod-live"
REFERENCE = (
    Path.home()
    / "Developer/research-context/bridge/2026-10-25-systems-and-swarms/raw"
    / "cm9-window-budget-20261027/e2e-muse-g.jsonl"
)
_CONNECTION: sqlite3.Connection | None = None


def connect(database: Path) -> sqlite3.Connection:
    con = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def select_traces(
    con: sqlite3.Connection, repo_root: Path
) -> tuple[list[str], dict[str, Any]]:
    registry = CorpusRegistry(repo_root)
    identities = con.execute(
        "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest, "
        "json_extract(metadata_json, '$.item_id') AS item_id, "
        "json_type(metadata_json, '$.item_id') AS item_type FROM corpus_tasks"
    )
    admitted = admitted_cloud_task_ids(identities, registry=registry)
    rows = con.execute(
        "SELECT t.trace_id, t.task_id, t.step_count, "
        "json_extract(t.verified_receipt_json, '$.admission.effective_split') AS effective_split "
        "FROM agent_traces t JOIN corpus_tasks c USING(task_id) "
        "WHERE c.dataset_id='goodfire-reward-hack-data' AND c.split='DEV' "
        "ORDER BY t.trace_id"
    ).fetchall()
    receipt_dev = [row for row in rows if row["effective_split"] == "DEV"]
    selected = [row for row in receipt_dev if row["task_id"] in admitted]
    trace_ids = [row["trace_id"] for row in selected]
    if not trace_ids:
        raise ValueError("No Goodfire DEV traces passed both admission gates")
    return trace_ids, {
        "dataset_id": "goodfire-reward-hack-data",
        "corpus_split": "DEV",
        "receipt_effective_split": "DEV",
        "split_assignments_digest": registry.split_assignments_digest,
        "corpus_dev_traces": len(rows),
        "receipt_dev_traces": len(receipt_dev),
        "excluded_by_receipt": len(rows) - len(receipt_dev),
        "excluded_by_admission": len(receipt_dev) - len(selected),
        "selected_traces": len(selected),
        "selected_tasks": len({row["task_id"] for row in selected}),
        "selected_native_steps": sum(row["step_count"] for row in selected),
        "trace_ids_sha256": hashlib.sha256("\n".join(trace_ids).encode()).hexdigest(),
    }


def load_steps(con: sqlite3.Connection, trace_id: str) -> list[dict[str, Any]]:
    """CM-2 native-message conversion, with label-free SQL projections of flags."""
    rows = con.execute(
        "SELECT step_index, role, content_text, "
        "json_extract(flags_json, '$.reasoning_content') AS reasoning_content, "
        "json_extract(flags_json, '$.tool_call_id') AS tool_call_id, "
        "json_extract(flags_json, '$.native_node_residual.message.tool_calls') AS tool_calls "
        "FROM trace_steps WHERE trace_id=? ORDER BY step_index",
        (trace_id,),
    )
    steps: list[dict[str, Any]] = []
    by_call: dict[str, dict[str, Any]] = {}
    pending: dict[str, Any] | None = None
    for row in rows:
        index, role, content = row["step_index"], row["role"], row["content_text"]
        if role == "assistant":
            calls = []
            for call in json.loads(row["tool_calls"] or "[]"):
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except (TypeError, ValueError):
                    arguments = {"raw": str(call.get("arguments"))}
                if not isinstance(arguments, dict):
                    arguments = {"raw": arguments}
                calls.append(
                    {
                        "tool_call_id": str(call.get("id")),
                        "function_name": str(call.get("name") or ""),
                        "arguments": arguments,
                    }
                )
            step: dict[str, Any] = {
                "step_id": index,
                "source": "agent",
                "message": content or "",
                "tool_calls": calls,
                "observation": {"results": []},
            }
            if row["reasoning_content"]:
                step["reasoning_content"] = row["reasoning_content"]
            steps.append(step)
            for call in calls:
                by_call[call["tool_call_id"]] = step
            pending = step
        elif role == "tool":
            owner = by_call.get(str(row["tool_call_id"])) or pending
            if owner is not None:
                owner["observation"]["results"].append(
                    {"source_call_id": str(row["tool_call_id"]), "content": content or ""}
                )
                owner["available_at"] = index
        else:
            steps.append({"step_id": index, "source": role, "message": content or ""})
    for step in steps:
        step.setdefault("available_at", step["step_id"])
        step["available_at"] = max(step["available_at"], step["step_id"])
    return steps


def init_worker(database: Path) -> None:
    global _CONNECTION
    _CONNECTION = connect(database)


def replay(trace_id: str) -> tuple[dict[str, Any], int]:
    """CM-9 event replay, retaining only the requested Stage-1 card identity."""
    if _CONNECTION is None:
        raise RuntimeError("Worker database connection was not initialized")
    steps = sorted(load_steps(_CONNECTION, trace_id), key=lambda step: int(step["step_id"]))
    # Only this opaque synthetic ID reaches the monitor, never source/task/model IDs.
    run_id = "cm5-" + hashlib.sha256(trace_id.encode()).hexdigest()[:16]
    cards: list[dict[str, Any]] = []

    def emit(event: dict[str, Any]) -> None:
        payload = event["payload"]
        if "deterministic_detector_unavailable" in (payload.get("coverage") or {}).get("gaps", []):
            raise RuntimeError(f"Stage-1 detector unavailable: {payload['coverage']}")
        if event["event_type"] == "monitor.suspicion":
            cards.append(
                {
                    "rule": payload["rule"],
                    "origin": payload.get("origin"),
                    "flagged_step": payload["evidence"][0]["step"],
                }
            )

    with MisalignmentLiveWorker(emit=emit, judge=None) as worker:
        for index, step in enumerate(steps):
            worker.ingest_event(
                {
                    "event_id": f"{run_id}:{index}",
                    "run_id": run_id,
                    "event_type": "trace.step",
                    "payload": {"step": step},
                }
            )
    return {"trace_id": trace_id, "stage1_any": bool(cards), "cards": cards}, len(steps)


def card_multiset(cards: list[dict[str, Any]]) -> Counter[tuple[str, str | None, int]]:
    return Counter((card["rule"], card.get("origin"), card["flagged_step"]) for card in cards)


def multiset_rows(cards: Counter[tuple[str, str | None, int]]) -> list[dict[str, Any]]:
    return [
        {"rule": rule, "origin": origin, "flagged_step": step, "count": count}
        for (rule, origin, step), count in sorted(cards.items(), key=lambda item: repr(item[0]))
    ]


def cascade_constants() -> dict[str, Any]:
    names = (
        "CASCADE_VERSION", "PASS_THROUGH_RULES", "PREFILTER_ESCALATED", "MAX_FAMILIES",
        "MAX_DIAGNOSTIC_FAMILIES", "MAX_EXAMPLES", "MAX_PLAYBOOK_CARDS", "PLAYBOOK_LEAD_SCHEMA",
        "RADIUS", "LOOKAHEAD", "MAX_WINDOW_CHARS", "INCLUDE_REASONING", "CONTEXT_REASONING_CHARS",
        "_EXAMPLE_ACTION_CHARS", "_FLAGGED_COMMAND_CHARS", "_FLAGGED_OBSERVATION_CHARS",
        "_CONTEXT_COMMAND_CHARS", "_CONTEXT_OBSERVATION_CHARS", "_MESSAGE_CHARS", "_REASONING_CHARS",
    )
    return {
        name: sorted(value) if isinstance(value := getattr(window_cascade, name), frozenset) else value
        for name in names
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=STATE_ROOT / "cloud-trace-db.sqlite")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    parser.add_argument("--output", type=Path, default=STATE_ROOT / "trace-tools/cascade-stage1-goodfire-dev.jsonl")
    parser.add_argument("--receipt", type=Path, default=Path(__file__).resolve().parents[1] / "stage1-receipt.json")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 1) - 2))
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    if window_cascade.CASCADE_VERSION != "window-cascade/v7":
        raise ValueError("This retained experiment requires window-cascade/v7")
    # Reference labels and judgments never enter the replay or selection. Only these
    # card-identity multisets survive loading the retained CM-9 artifact.
    reference: dict[str, Counter[tuple[str, str | None, int]]] = {}
    for line in args.reference.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        trace_id = record["trace_id"]
        if trace_id in reference:
            raise ValueError(f"Duplicate reference trace: {trace_id}")
        reference[trace_id] = card_multiset(record["cards"])
    if len(reference) != 200:
        raise ValueError(f"Expected the 200 CM-9 traces, got {len(reference)}")
    with connect(args.database) as con:
        trace_ids, selection = select_traces(con, args.repo_root)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=args.repo_root, check=True, capture_output=True, text=True
    ).stdout.strip()
    print(json.dumps({"selected_traces": len(trace_ids), "workers": args.workers}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    n_traces = n_flagged = n_cards = n_steps = 0
    rules: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    matched: set[str] = set()
    mismatches = []
    with (
        args.output.open("xb") as sink,
        ProcessPoolExecutor(max_workers=args.workers, initializer=init_worker, initargs=(args.database,)) as pool,
    ):
        for result, steps in pool.map(replay, trace_ids, chunksize=1):
            line = (json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            sink.write(line)
            digest.update(line)
            n_traces += 1
            n_flagged += result["stage1_any"]
            n_cards += len(result["cards"])
            n_steps += steps
            rules.update(card["rule"] for card in result["cards"])
            origins.update(card["origin"] for card in result["cards"])
            trace_id = result["trace_id"]
            if trace_id in reference:
                matched.add(trace_id)
                actual = card_multiset(result["cards"])
                expected = reference[trace_id]
                if actual != expected:
                    mismatches.append(
                        {"trace_id": trace_id, "missing_cards": multiset_rows(expected - actual),
                         "extra_cards": multiset_rows(actual - expected)}
                    )
            if n_traces % 100 == 0:
                print(json.dumps({"completed_traces": n_traces}), flush=True)
    missing_reference = sorted(reference.keys() - matched)
    receipt = {
        "schema": "evallab.trace_tools_stage1_receipt/v1",
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "source_git_revision": revision,
        "source_sha256": {
            str(path.relative_to(args.repo_root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                args.repo_root / "src/evallab/production/monitor_worker.py",
                args.repo_root / "src/evallab/production/window_cascade.py",
                Path(__file__).resolve(),
            )
        },
        "database": str(args.database.resolve()),
        "selection": selection,
        "cascade": cascade_constants(),
        "execution": {"judge": None, "model_calls": 0, "uploads": 0, "containers": 0,
                      "cost_usd": 0, "workers": args.workers, "cpu_count": os.cpu_count()},
        "counts": {"output_traces": n_traces, "stage1_any": n_flagged,
                   "stage1_none": n_traces - n_flagged, "cards": n_cards,
                   "converted_steps": n_steps, "cards_by_rule": dict(sorted(rules.items())),
                   "cards_by_origin": dict(sorted(origins.items()))},
        "parity": {"reference_path": str(args.reference.resolve()),
                   "reference_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
                   "comparison": "rule/origin/flagged_step multiset per trace",
                   "reference_traces": len(reference), "compared_traces": len(matched),
                   "matching_traces": len(matched) - len(mismatches),
                   "mismatch_count": len(mismatches), "mismatches": mismatches,
                   "missing_reference_traces": missing_reference,
                   "passed": not mismatches and not missing_reference},
        "wall_seconds": time.perf_counter() - started,
        "output": {"path": str(args.output.resolve()), "sha256": digest.hexdigest(),
                   "bytes": args.output.stat().st_size},
    }
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"counts": receipt["counts"], "parity_passed": receipt["parity"]["passed"],
                      "parity_mismatches": len(mismatches), "parity_missing": len(missing_reference),
                      "wall_seconds": receipt["wall_seconds"], "output_sha256": digest.hexdigest()}), flush=True)
    if not receipt["parity"]["passed"]:
        raise SystemExit("Stage-1 output retained; CM-9 parity did not pass (see receipt)")


if __name__ == "__main__":
    main()
