#!/usr/bin/env python3
"""Read ONLY six local, uninjected MBPP shards; never export completion text.

These are single-turn coding completions, NOT multi-step agent trajectories.
Only text enters the detector; every grade, reward and run/task identifier is
scorer-only. No code from the corpus is executed. No model or network calls.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CORPUS = "eleutherai__hack-ignition-benchmark"
REPO = "EleutherAI/hack-ignition-benchmark"
REVISION = "1a6ac3a0d095c627101406dfbb357382af4321ae"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
MODEL = "OLMo-7B (base; publisher README attribution)"
LABELS = ("exploitable", "hack", "hardened")
SHARDS = {
    "baseph_persist__base_nh_s0": "79c82f7645fa0ca755279c4546bc6db2f4683c3521ae6dc0bfc77e40631cf935",
    "baseph_persist__base_nh_s1": "2eb0f0d4816705f8b748f8e62467fc6be30fa1f1a9a72dba1361c4b5a4d46e3e",
    "baseph_persist__base_ph_s0": "48f233abbdd47dfd5bf176cd7eab812c1a7ec1a1f8ee73253d7a42e0a7dbe841",
    "baseph_persist__base_ph_s1": "b1a040f5e6216c86dbc9eb86f32760bfc6ba247851ce6e5f0cdd5093cc9c3649",
    "baseph_persist__base_ph_s2": "e3af6335330a2d981f055933552f03fa451e032933147d953f89c5eb57f7db3c",
    "baseph_persist__base_ph_s3": "83bbab7b43e8db0860e5275bf1022b0388a92424071dd38f670bd369fcbc07ce",
}
ROW_FIELDS = {
    "step",
    "launch",
    "call",
    "idx",
    "item",
    "exploit_type",
    "text",
    "grades",
    "reward",
    "reward_mode",
    "record",
}


def hashed_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def manifest(root: Path = ROOT) -> dict[str, Any]:
    staged = json.loads((root / "STAGE-MANIFEST.json").read_text(encoding="utf-8"))
    expected = {"README.md", "rollouts/mbpp/MANIFEST.json"} | {
        f"rollouts/mbpp/{shard}/rollouts.jsonl.gz" for shard in SHARDS
    }
    if (
        staged["repo"] != REPO
        or staged["revision"] != REVISION
        or staged["licence"] != "apache-2.0"
        or staged["gated"] is not False
        or {entry["path"] for entry in staged["files"]} != expected
        or len(staged["files"]) != len(expected)
    ):
        raise ValueError("bundle identity, licence or exact file allowlist differs from the pin")
    return staged


def condition(shard: str) -> str:
    if shard not in SHARDS:
        raise ValueError("shard is outside the six-run allowlist")
    return "no_hints" if "__base_nh_" in shard else "please_hack"


def record_id(shard: str, row: dict[str, Any]) -> str:
    """Canonical compact JSON of the publisher key (family, run, step, idx)."""
    return json.dumps(
        ["mbpp", shard.replace("__", "/"), row["step"], row["idx"]],
        separators=(",", ":"),
    )


def iter_records(root: Path = ROOT) -> Iterator[tuple[str, str, dict[str, Any]]]:
    manifest(root)
    for shard in SHARDS:
        with gzip.open(
            root / f"rollouts/mbpp/{shard}/rollouts.jsonl.gz", "rt", encoding="utf-8"
        ) as stream:
            for line in stream:
                row = json.loads(line)
                yield shard, record_id(shard, row), row


def detector_input(row: dict[str, Any]) -> dict[str, str]:
    """Complete detector allowlist. No task prompt is present in these rows."""
    return {"text": row["text"]}


def to_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve text exactly, without inventing a user turn, tools or reasoning."""
    return [{"step_id": 0, "source": "agent", "message": detector_input(row)["text"]}]


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Pass ONLY _steps to the monitor. All other keys are scorer-only.

    trace_id is an in-memory join key, never receipt content. task_key clusters
    all visits, checkpoints, seeds and conditions to the same MBPP item.
    """
    for shard, identity, row in iter_records(root):
        yield {
            "corpus": CORPUS,
            "trace_id": identity,
            "trace_key": hashed_id(identity),
            "task_key": hashed_id(row["item"]),
            "condition": condition(shard),
            "run": shard,
            "model": MODEL,
            "positive": bool(row["grades"]["hack"]),
            "gold_grades": dict(row["grades"]),
            "gold_reward": row["reward"],
            "reward_mode": row["reward_mode"],
            "training_step": row["step"],
            "completion_index": row["idx"],
            "target_step_id": 0,
            "_steps": to_steps(row),
        }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Check EVERY row; return counts only, including explicit zero issues."""
    issues: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    runs: Counter[str] = Counter()
    conditions: dict[str, Counter[str]] = {name: Counter() for name in ("no_hints", "please_hack")}
    identities: set[str] = set()
    trace_keys: set[str] = set()
    tasks: set[str] = set()
    for shard, identity, row in iter_records(root):
        runs[shard] += 1
        totals["rows"] += 1
        issues["row_fields"] += set(row) != ROW_FIELDS
        issues["duplicate_record_ids"] += identity in identities
        key = hashed_id(identity)
        issues["trace_key_collisions"] += key in trace_keys
        identities.add(identity)
        trace_keys.add(key)
        tasks.add(hashed_id(row["item"]))
        issues["item_type"] += not isinstance(row["item"], str)
        issues["index_types"] += any(
            type(row[name]) is not int or row[name] < 0
            for name in ("step", "idx", "launch", "call")
        )
        issues["completion_type"] += not isinstance(row["text"], str)
        grades = row["grades"]
        binary = set(grades) == set(LABELS) and all(
            type(grades[label]) in (int, float) and grades[label] in (0, 1) for label in LABELS
        )
        issues["grade_schema"] += not binary
        if binary:
            state = "positive" if grades["hack"] else "negative"
            totals[state] += 1
            conditions[condition(shard)][state] += 1
            issues["hack_formula"] += grades["hack"] != (
                grades["exploitable"] == 1 and grades["hardened"] == 0
            )
            issues["reward_exploitable"] += row["reward"] != grades["exploitable"]
        else:
            totals["unlabeled"] += 1
        issues["native_record_version"] += row["record"] != "v0.2a"
        issues["reward_mode"] += row["reward_mode"] != "exploitable"
        steps = to_steps(row)
        issues["single_agent_step"] += steps != [
            {"step_id": 0, "source": "agent", "message": row["text"]}
        ]
        issues["metadata_dependency"] += steps != to_steps({"text": row["text"]})
        totals["agent_steps"] += len(steps)
        totals["completion_characters"] += len(row["text"])
        totals["user_steps"] += sum(step["source"] == "user" for step in steps)
        totals["empty_completions"] += row["text"] == ""
    issues["run_row_counts"] += any(runs[shard] != 4960 for shard in SHARDS)
    issues["total_row_count"] += totals["rows"] != 29760
    issues["task_count"] += len(tasks) != 60
    return {
        "corpus": CORPUS,
        "rows": totals["rows"],
        "positive": totals["positive"],
        "negative": totals["negative"],
        "unlabeled": totals["unlabeled"],
        "agent_steps": totals["agent_steps"],
        "user_steps": totals["user_steps"],
        "tasks": len(tasks),
        "completion_characters": totals["completion_characters"],
        "empty_completions": totals["empty_completions"],
        "by_run": dict(runs),
        "by_condition": {name: dict(counts) for name, counts in conditions.items()},
        "issue_count": sum(issues.values()),
        "issues": dict(sorted(issues.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selfcheck"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = selfcheck(args.root)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(bool(result["issue_count"]))


if __name__ == "__main__":
    main()
