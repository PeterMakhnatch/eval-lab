#!/usr/bin/env python3
"""Freeze and select HAR-135's training-only GEPA gate; never launch trials.

The manifest binds CSV bytes, not a substitute for HAR-133's independent G1
repository-lineage audit. Seed rows must explicitly identify G2 repeat 1.
Selection consumes native process-job counts without reclassifying trials.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FREEZE_SCHEMA_VERSION = "har135.freeze/v1"
SELECTION_SCHEMA_VERSION = "har135.selection/v1"
COUNTS_SCHEMA = "evallab.counts/v1"
TASK_COUNT = 10
BASELINE_REPEAT = 1
MAX_CANDIDATES = 2
MAX_GATE_COST_USD = 2.0
SELECTION_RULE = "counted_pass_then_total_tokens"
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class GateError(ValueError):
    """Invalid evidence is a refusal, not a failed model trial."""

    def __init__(self, decision: str, message: str) -> None:
        super().__init__(message)
        self.decision = decision


def _require(condition: bool, decision: str, message: str) -> None:
    if not condition:
        raise GateError(decision, message)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_digest(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def repo_key(project: str) -> str:
    """Use the existing select_eval.py census-key comparison convention."""
    return project.rstrip("/").rsplit("/", 1)[-1].lower().replace("-", "_")


def _check_disjoint(tasks: list[dict[str, Any]], held_out: list[dict[str, Any]]) -> None:
    held_ids = {row["task_id"] for row in held_out}
    held_repos = {repo_key(row["project"]) for row in held_out}
    for task in tasks:
        _require(task["task_id"] not in held_ids, "held_out_collision", f"Held-out task: {task['task_id']}")
        _require(repo_key(task["project"]) not in held_repos, "held_out_collision", f"Held-out repository: {task['project']}")


def freeze_minibatch(proposal_path: Path, eval_tasks_path: Path) -> dict[str, Any]:
    """Bind the first ten proposal rows before outcomes, with no substitution."""
    proposal_bytes = Path(proposal_path).read_bytes()
    eval_bytes = Path(eval_tasks_path).read_bytes()
    proposal = list(csv.DictReader(io.StringIO(proposal_bytes.decode("utf-8"))))
    eval_rows = list(csv.DictReader(io.StringIO(eval_bytes.decode("utf-8"))))
    _require(len(proposal) >= TASK_COUNT, "insufficient_rows", "Proposal must contain at least ten tasks")
    _require(bool(eval_rows), "malformed_eval_tasks", "Held-out CSV is empty")
    tasks = [
        {"task_id": row.get("task_id", ""), "task_package_digest": row.get("run_digest", ""), "project": row.get("project", "")}
        for row in proposal[:TASK_COUNT]
    ]
    held_out = [{"task_id": row.get("task", ""), "project": row.get("repo", "")} for row in eval_rows]
    manifest = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "proposal_sha256": _sha256(proposal_bytes),
        "eval_tasks_sha256": _sha256(eval_bytes),
        "selection_rule": SELECTION_RULE,
        "baseline_repeat": BASELINE_REPEAT,
        "max_candidates": MAX_CANDIDATES,
        "max_gate_cost_usd": MAX_GATE_COST_USD,
        "tasks": tasks,
        "held_out_tasks": held_out,
    }
    _validate_manifest(manifest)
    return manifest


def _validate_manifest(manifest: dict[str, Any]) -> dict[str, str]:
    _require(isinstance(manifest, dict), "malformed_manifest", "Manifest must be an object")
    for key, expected in (
        ("schema_version", FREEZE_SCHEMA_VERSION), ("selection_rule", SELECTION_RULE),
        ("baseline_repeat", BASELINE_REPEAT), ("max_candidates", MAX_CANDIDATES),
        ("max_gate_cost_usd", MAX_GATE_COST_USD),
    ):
        _require(manifest.get(key) == expected, "malformed_manifest", f"Frozen {key} must be {expected!r}")
    for key in ("proposal_sha256", "eval_tasks_sha256"):
        _require(isinstance(manifest.get(key), str) and bool(SHA256_PATTERN.fullmatch(manifest[key])), "malformed_manifest", f"Invalid {key}")
    tasks, held_out = manifest.get("tasks"), manifest.get("held_out_tasks")
    _require(isinstance(tasks, list) and len(tasks) == TASK_COUNT, "malformed_manifest", "Manifest must contain exactly ten tasks")
    _require(isinstance(held_out, list) and bool(held_out), "malformed_manifest", "Manifest must bind held-out identities")
    for row in tasks + held_out:
        _require(isinstance(row, dict), "malformed_manifest", "Task identity must be an object")
        for key in ("task_id", "project"):
            _require(isinstance(row.get(key), str) and bool(row[key].strip()), "malformed_manifest", f"Missing task {key}")
        _require(not row["project"].startswith("format-code-task-"), "malformed_manifest", "Unknown repository cannot establish disjointness")
    for row in tasks:
        _require(isinstance(row.get("task_package_digest"), str) and bool(SHA256_PATTERN.fullmatch(row["task_package_digest"])), "malformed_manifest", "Invalid task package digest")
    expected = {row["task_id"]: row["task_package_digest"] for row in tasks}
    _require(len(expected) == TASK_COUNT, "duplicate_task_rows", "Duplicate frozen training task")
    _require(len({row["task_id"] for row in held_out}) == len(held_out), "malformed_manifest", "Duplicate held-out task")
    _check_disjoint(tasks, held_out)
    return expected


def _row_map(rows: list[dict[str, Any]], expected: dict[str, str], *, seed: bool) -> dict[str, dict[str, Any]]:
    _require(isinstance(rows, list), "malformed_rows", "Trial rows must be a list")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        _require(isinstance(row, dict), "malformed_rows", "Trial row must be an object")
        task_id = row.get("task_id")
        _require(isinstance(task_id, str) and task_id in expected, "extra_task_rows", f"Trial outside frozen minibatch: {task_id!r}")
        _require(task_id not in result, "duplicate_task_rows", f"Duplicate trial for {task_id}")
        _require(row.get("task_package_digest") == expected[task_id], "wrong_digest", f"Task package drift: {task_id}")
        _require(isinstance(row.get("trial_id"), str) and bool(row["trial_id"].strip()), "malformed_rows", f"Missing native trial identity: {task_id}")
        _require(isinstance(row.get("receipt_paths"), dict) and bool(row["receipt_paths"]), "malformed_rows", f"Missing receipt paths: {task_id}")
        if seed:
            _require(type(row.get("repeat")) is int and row["repeat"] == BASELINE_REPEAT, "invalid_baseline_repeat", f"Seed must explicitly bind repeat 1: {task_id}")
            _require(row.get("attempt", BASELINE_REPEAT) == BASELINE_REPEAT, "invalid_baseline_repeat", f"Seed attempt differs from repeat 1: {task_id}")
        counts = row.get("counts")
        _require(isinstance(counts, dict) and counts.get("schema") == COUNTS_SCHEMA, "malformed_counts", f"Missing canonical counts: {task_id}")
        verdict, reward, reasons = counts.get("verdict"), counts.get("raw_reward"), counts.get("reasons")
        _require(verdict in ("counted_pass", "counted_fail", "excluded"), "malformed_counts", f"Invalid counts verdict: {task_id}")
        _require(isinstance(reasons, list) and all(isinstance(reason, str) and reason for reason in reasons), "malformed_counts", f"Invalid counts reasons: {task_id}")
        if verdict == "excluded":
            _require(bool(reasons), "malformed_counts", f"Excluded trial has no reason: {task_id}")
        else:
            valid_reward = type(reward) in (int, float) and math.isfinite(reward)
            _require(valid_reward and counts.get("scored") is True and not reasons, "malformed_counts", f"Countable verdict lacks a scored finite reward: {task_id}")
            _require((reward >= 1.0) == (verdict == "counted_pass"), "malformed_counts", f"Reward and verdict disagree: {task_id}")
        usage = row.get("usage")
        _require(isinstance(usage, dict), "malformed_usage", f"Missing usage object: {task_id}")
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            _require(value is None or (type(value) is int and value >= 0), "malformed_usage", f"Invalid {key}: {task_id}")
        result[task_id] = row
    return result


def _summary(rows: dict[str, dict[str, Any]], expected: dict[str, str]) -> dict[str, Any]:
    excluded = [{"task_id": task_id, "reasons": row["counts"]["reasons"]} for task_id, row in rows.items() if row["counts"]["verdict"] == "excluded"]
    missing = sorted(expected.keys() - rows.keys())
    token_values = [row["usage"].get(key) for row in rows.values() for key in ("input_tokens", "output_tokens")]
    return {
        "task_count": len(rows),
        "counted_passes": sum(row["counts"]["verdict"] == "counted_pass" for row in rows.values()),
        "counted_fails": sum(row["counts"]["verdict"] == "counted_fail" for row in rows.values()),
        "excluded": excluded,
        "missing_tasks": missing,
        "complete_countable_coverage": not missing and not excluded,
        "total_tokens": sum(token_values) if not missing and all(value is not None for value in token_values) else None,
        "rows_sha256": _json_digest(list(rows.values())),
    }


def _freeze_time(candidate: dict[str, Any]) -> datetime:
    timestamp = candidate.get("frozen_at")
    _require(isinstance(timestamp, str), "malformed_timestamp", "Candidate must bind a freeze timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError("malformed_timestamp", "Invalid candidate freeze timestamp") from exc
    _require(parsed.tzinfo is not None, "malformed_timestamp", "Candidate freeze timestamp must have a timezone")
    return parsed.astimezone(UTC)


def select_candidate(manifest: dict[str, Any], seed_rows: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Promote only complete paired evidence that strictly beats the frozen seed."""
    expected = _validate_manifest(manifest)
    _require(isinstance(candidates, list) and len(candidates) <= MAX_CANDIDATES, "too_many_candidates", "At most two candidates are permitted")
    seed = _summary(_row_map(seed_rows, expected, seed=True), expected)
    summaries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        _require(isinstance(candidate, dict), "malformed_candidate", "Candidate must be an object")
        candidate_id = candidate.get("candidate_id")
        _require(isinstance(candidate_id, str) and bool(SHA256_PATTERN.fullmatch(candidate_id)), "malformed_candidate_id", "Candidate ID must be a full sha256 digest")
        _require(candidate_id not in seen_ids, "duplicate_candidate_ids", "Duplicate candidate digest")
        seen_ids.add(candidate_id)
        _require(isinstance(candidate.get("candidate_path"), str) and bool(candidate["candidate_path"]), "malformed_candidate", "Candidate bytes must have a recorded path")
        frozen_at = _freeze_time(candidate).isoformat()
        summary = _summary(_row_map(candidate.get("rows"), expected, seed=False), expected)
        paired_complete = seed["complete_countable_coverage"] and summary["complete_countable_coverage"]
        pass_delta = summary["counted_passes"] - seed["counted_passes"] if paired_complete else None
        tokens_known = summary["total_tokens"] is not None and seed["total_tokens"] is not None
        beats_seed = paired_complete and (pass_delta > 0 or (pass_delta == 0 and tokens_known and summary["total_tokens"] < seed["total_tokens"]))
        summaries.append({
            **summary, "candidate_id": candidate_id, "candidate_path": candidate["candidate_path"],
            "frozen_at": frozen_at, "eligible": paired_complete, "beats_seed": beats_seed,
            "pass_delta_vs_seed": pass_delta,
            "token_delta_vs_seed": summary["total_tokens"] - seed["total_tokens"] if paired_complete and tokens_known else None,
        })
    # Unknown token totals prove no preference, including between two candidates.
    promoters = sorted((row for row in summaries if row["beats_seed"]), key=lambda row: (row["frozen_at"], row["candidate_id"]))
    best = promoters[0] if promoters else None
    for row in promoters[1:]:
        assert best is not None
        more_passes = row["counted_passes"] > best["counted_passes"]
        fewer_tokens = row["counted_passes"] == best["counted_passes"] and row["total_tokens"] is not None and best["total_tokens"] is not None and row["total_tokens"] < best["total_tokens"]
        if more_passes or fewer_tokens:
            best = row
    if best is not None:
        decision = "counted_improvement" if best["pass_delta_vs_seed"] > 0 else "tokens_improvement"
    elif not seed["complete_countable_coverage"] or any(not row["eligible"] for row in summaries):
        decision = "incomplete_countable_coverage"
    elif any(row["counted_passes"] == seed["counted_passes"] and (row["total_tokens"] is None or seed["total_tokens"] is None) for row in summaries):
        decision = "unknown_tokens"
    else:
        decision = "no_candidate_beats_seed"
    return {
        "schema_version": SELECTION_SCHEMA_VERSION, "manifest_digest": _json_digest(manifest),
        "status": "promoted" if best is not None else "seed_retained", "decision": decision,
        "winner": best["candidate_id"] if best is not None else "seed", "promoted": best is not None,
        "seed": seed, "candidates": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--proposal", type=Path, required=True)
    freeze.add_argument("--eval-tasks", type=Path, required=True)
    freeze.add_argument("--out", type=Path, required=True)
    select = commands.add_parser("select")
    select.add_argument("--manifest", type=Path, required=True)
    select.add_argument("--seed", type=Path, required=True)
    select.add_argument("--candidates", type=Path, nargs="*", default=[])
    select.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            result = freeze_minibatch(args.proposal, args.eval_tasks)
            rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
            if args.out.exists():
                _require(args.out.read_bytes() == rendered.encode(), "frozen_manifest_changed", "Refusing to overwrite changed frozen manifest")
            else:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("x", encoding="utf-8") as handle:
                    handle.write(rendered)
        else:
            candidates = [json.loads(path.read_text(encoding="utf-8")) for path in args.candidates]
            for candidate in candidates:
                _require(isinstance(candidate, dict) and isinstance(candidate.get("candidate_path"), str), "malformed_candidate", "Candidate receipt must bind its instruction file")
                _require(_sha256(Path(candidate["candidate_path"]).read_bytes()) == candidate.get("candidate_id"), "candidate_digest_drift", "Candidate bytes differ from frozen digest")
            result = select_candidate(json.loads(args.manifest.read_text(encoding="utf-8")), json.loads(args.seed.read_text(encoding="utf-8")), candidates)
            rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    except (GateError, OSError, ValueError) as exc:
        print(json.dumps({"schema_version": SELECTION_SCHEMA_VERSION, "status": "rejected", "decision": getattr(exc, "decision", "invalid_input"), "reasons": [str(exc)]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
