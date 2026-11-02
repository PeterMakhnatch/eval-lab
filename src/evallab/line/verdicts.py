"""Idempotent Judge Station imports with an append-only, hash-linked receipt chain."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.hack_drop import check_dev_path_allowed
from evallab.line.io import digest, read_json, read_rows, write_json
from evallab.production.judge_report import effective_records, load_rows

VERDICTS = {"cheat", "legitimate", "unclear"}
HISTORY = "verdicts-history"


def _identity(row: dict[str, Any]) -> str:
    if isinstance(row.get("id"), str) and row["id"]:
        return row["id"]
    judge, run_id = row.get("judge"), row.get("run_id")
    if not isinstance(judge, str) or not isinstance(run_id, str):
        raise ValueError("every verdict needs a nonempty id or judge + run_id")
    return hashlib.sha256(f"judge\0{judge}\0run_id\0{run_id}".encode()).hexdigest()


def _target(path: Path) -> Path:
    if path.is_dir():
        path = path / "verdicts.jsonl"
    check_dev_path_allowed(path)
    if not path.is_file():
        raise ValueError(f"Judge Station verdict export/state not found: {path}")
    return path


def snapshot_station(batch: Path, source: Path) -> Path:
    """Copy Station bytes into the batch once; identical bytes reuse their snapshot."""
    snapshots = batch / "evidence" / "station-inputs"
    snapshots.mkdir(parents=True, exist_ok=True)
    existing = sorted(snapshots.glob("*.jsonl"))
    wanted = source.read_bytes() if source.is_file() else b""
    for previous in existing:
        if previous.read_bytes() == wanted:
            return previous
    target = snapshots / f"{len(existing) + 1:04d}.jsonl"
    target.write_bytes(wanted)
    return target


def import_verdicts(batch: Path, judge_state: Path, repo_root: Path) -> dict[str, Any]:
    """Append new effective Station verdicts; never alter line decisions."""
    source = _target(judge_state)
    admitted = {row["run_id"] for row in read_rows(batch / "runs.jsonl")}
    incoming = [
        row for row in effective_records(load_rows(source)) if row.get("run_id") in admitted
    ]
    if any(row.get("verdict") not in VERDICTS for row in incoming):
        raise ValueError("Station export contains an unknown human verdict")
    existing = read_rows(batch / "verdicts.jsonl")
    fingerprints: dict[str, str] = {}
    for row in existing:
        fingerprints[_identity(row)] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    new_rows = []
    for row in incoming:
        key, content = _identity(row), json.dumps(row, sort_keys=True, separators=(",", ":"))
        if key in fingerprints:
            if fingerprints[key] != content:
                raise ValueError(f"conflicting verdict id: {key}")
            continue
        fingerprints[key] = content
        new_rows.append(row)
    decisions_before = read_rows(batch / "decisions.jsonl")
    if new_rows:
        _append(batch, existing, new_rows, source)
    if read_rows(batch / "decisions.jsonl") != decisions_before:
        raise ValueError("verdict import must not change decisions")
    passed = _recompute(batch, repo_root)
    return {
        "batch": str(batch),
        "verdicts": len(existing) + len(new_rows),
        "imported": len(new_rows),
        "source": str(source),
        "passed": passed,
    }


def _append(
    batch: Path, existing: list[dict[str, Any]], new_rows: list[dict[str, Any]], source: Path
) -> None:
    receipts = batch / "receipts"
    current = receipts / "verdicts.json"
    if current.is_file():
        history = receipts / HISTORY
        history.mkdir(exist_ok=True)
        predecessor = history / f"{len(list(history.glob('*.json'))) + 1:04d}.json"
        predecessor.write_bytes(current.read_bytes())
    else:
        predecessor = receipts / "judge.json"
    previous = read_json(predecessor)
    previous_output = next(
        item for item in previous["outputs"] if Path(item["path"]) == batch / "verdicts.jsonl"
    )
    snapshot = snapshot_station(batch, source)
    # Stale-prefix hash is carried by the predecessor receipt; only append here.
    out = batch / "verdicts.jsonl"
    with out.open("ab") as handle:
        for row in new_rows:
            handle.write((json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode())
    started = time.time()
    rows_out = len(existing) + len(new_rows)
    write_json(
        current,
        {
            "schema": "evallab.line_receipt/v1",
            "batch_id": read_json(batch / "spec.json")["batch_id"],
            "stage": "verdicts",
            "code_rev": previous["code_rev"],
            "code_dirty": previous["code_dirty"],
            "started_at": datetime.fromtimestamp(started, UTC).isoformat(),
            "finished_at": datetime.fromtimestamp(time.time(), UTC).isoformat(),
            "seconds": time.time() - started,
            "inputs": [{"path": str(snapshot), "sha256": digest(snapshot)}],
            "outputs": [{"path": str(out), "sha256": digest(out), "rows": rows_out}],
            "counts": {"rows_in": len(new_rows), "rows_out": rows_out, "imported": len(new_rows)},
            "previous": {
                "path": predecessor.relative_to(batch).as_posix(),
                "sha256": digest(predecessor),
                "verdicts_sha256": previous_output["sha256"],
                "verdicts_rows": previous_output["rows"],
            },
            "models": [],
            "spend_usd": 0,
            "errors": [],
        },
    )


def _recompute(batch: Path, repo_root: Path) -> bool:
    """Publish a scoreboard matching the files, then the checks, then their view."""
    from evallab.line.core import check_batch
    from evallab.line.scoreboard import build_scoreboard, render_scoreboard

    write_json(batch / "scoreboard.json", build_scoreboard(batch, repo_root))
    checks = check_batch(batch, repo_root)
    write_json(batch / "check.json", checks)
    score = build_scoreboard(batch, repo_root)
    write_json(batch / "scoreboard.json", score)
    (batch / "SCOREBOARD.md").write_text(render_scoreboard(score), encoding="utf-8")
    return bool(checks["passed"])
