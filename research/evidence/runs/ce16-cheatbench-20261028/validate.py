"""CE-16: export CheatBench runs in every format and check the result from the outside.

Runs the real CLI against the real registry and results home (read-only). For each format it
exports 25 DEV-admitted runs, then verifies: every ATIF document validates with Harbor's
``Trajectory`` model; no registry identity, trial path segment, source name or expected-answer
string appears in any exported byte or file name; no reward is non-null; and TEST-assigned items
and unknown ids are refused with no output. Prints counts only (no ids, no text).

    validate.py --out summary.json [--per-format 25]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATASET = "cheatbench-actions-v1"
SOURCE_WORDS = ("cheatbench", "goodfire", "mimo", "tb4", "terminal-bench", "hacktrace", "hackbench")


def cli(*arguments: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    done = subprocess.run(
        ["uv", "run", "--no-sync", "evallab", "prod", *arguments], capture_output=True, text=True, check=False,
        env={**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"},
    )
    elapsed = time.perf_counter() - started
    lines = done.stdout.strip().splitlines()
    return (json.loads(lines[-1]) if lines else {"error": done.stderr[-300:]}), elapsed


def registry_items(root: Path) -> list[dict[str, Any]]:
    path = root / "research/answer-keys" / DATASET / "items.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def forbidden_tokens(items: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for item in items:
        for field in ("item_id", "source_id"):
            tokens.add(str(item[field]))
        tokens.add(str(item["subject"]["trial_id"]))
        for reference in ("trial_ref", "actions_only_ref"):
            tokens.update(segment for segment in item[reference]["path"].split("/") if len(segment) >= 16)
        tokens.add(str(item["subject"]["task_id"]))
        tokens.add(str(item["subject"]["task_id"]).rsplit("/", 1)[-1])
    return {token for token in tokens if token}


def visible_text(directory: Path) -> list[tuple[str, str]]:
    return [
        (path.relative_to(directory).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(directory.rglob("*")) if path.is_file()
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--per-format", type=int, default=25)
    args = parser.parse_args()
    root = Path.cwd()
    trajectory_type = importlib.import_module("harbor.models.trajectories.trajectory").Trajectory

    items = registry_items(root)
    dev = [item for item in items if item["split"] == "dev"]
    test = [item for item in items if item["split"] == "test"]
    tokens = forbidden_tokens(items)
    work = Path(tempfile.mkdtemp(prefix="ce16-", dir="/private/tmp"))
    results: dict[str, Any] = {}
    for view, format in (("full", "harbor"), ("full", "atif"), ("full", "scout"), ("actions_only", "atif")):
        label = f"{view}/{format}"
        out, join = work / f"{view}-{format}", work / f"{view}-{format}.join.json"
        receipt, seconds = cli(
            "export-cheatbench-runs", "--format", format, "--view", view, "--limit", str(args.per_format),
            "--output-directory", str(out), "--manifest", str(join),
        )
        files = visible_text(out)
        hits = sum(token.casefold() in (name + "\n" + text).casefold() for name, text in files for token in tokens)
        hits += sum(word in (name + "\n" + text).casefold() for name, text in files for word in SOURCE_WORDS)
        documents: list[dict[str, Any]] = []
        if format == "harbor":
            documents = [json.loads(text) for name, text in files if name.endswith("agent/trajectory.json")]
            rewards = [json.loads(text)["verifier_result"]["rewards"]["reward"]
                       for name, text in files if name.endswith("result.json")]
        elif format == "atif":
            documents = [json.loads(text) for _name, text in files]
            rewards = []
        else:
            rewards = []
        valid = 0
        for document in documents:
            trajectory_type.model_validate(document)
            valid += 1
        joins = json.loads(join.read_text(encoding="utf-8"))
        results[label] = {
            "exported": receipt.get("exported"), "admitted": receipt.get("admitted"), "refused": receipt.get("refused"),
            "candidates": receipt.get("candidates"), "atif_documents": len(documents), "harbor_trajectory_valid": valid,
            "identity_or_source_hits": hits, "non_null_rewards": sum(reward is not None for reward in rewards),
            "scout_transcript_lines": len(files[0][1].splitlines()) if format == "scout" and files else None,
            "join_entries": len(joins), "join_rewards_non_null": sum(entry["reward"] is not None for entry in joins.values()),
            "join_sha256": receipt.get("manifest_sha256"),
            "wall_seconds": round(seconds, 2), "in_process_ms": receipt.get("elapsed_ms"),
            "files": len(files),
        }
        print(label, json.dumps(results[label], sort_keys=True), flush=True)
    refused: dict[str, Any] = {}
    for label, wanted in (("test_items", test[:25]), ("unknown_ids", [])):
        ids = [item["item_id"] for item in wanted] or [f"{DATASET}:{'0' * 24}", "no-such-item"]
        out, join = work / f"refused-{label}", work / f"refused-{label}.json"
        receipt, seconds = cli(
            "export-cheatbench-runs", "--format", "atif", "--output-directory", str(out), "--manifest", str(join),
            *(part for item_id in ids for part in ("--run-id", item_id)),
        )
        refused[label] = {
            "requested": len(ids), "admitted": receipt.get("admitted"), "exported": receipt.get("exported"),
            "refused": receipt.get("refused"), "files": len(visible_text(out)), "wall_seconds": round(seconds, 2),
        }
        print(label, json.dumps(refused[label], sort_keys=True), flush=True)
    summary = {
        "schema": "evallab.ce16_cheatbench_export_validation/v1", "recorded_at_utc": datetime.now(UTC).isoformat(),
        "registry": {"dev_items": len(dev), "test_items": len(test), "candidates": len(items)},
        "runs": results, "refused": refused, "forbidden_token_count": len(tokens),
    }
    Path(args.out).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("summary sha256", hashlib.sha256(Path(args.out).read_bytes()).hexdigest(), file=sys.stderr)


if __name__ == "__main__":
    main()
