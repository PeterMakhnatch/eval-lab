#!/usr/bin/env python3
"""Offline replay adapter for OSF 7as8h "Reward Pressure Study 2".

Only ``_steps`` may reach a frozen detector. ``_steps`` is built from the
agent-visible transcript only (assistant text, tool calls, tool results). It
never includes system/user prompts (which carry the pressure and guardian
conditions), reasoning fields, the final solution/tests, the agent report,
run outputs, labels, or any metadata identifying label or condition.
No network, no model calls, no printing of source text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CORPUS = "osf__7as8h-reward-pressure-study-2"
NODE = "7as8h"
DIRNAME = "dd5151aab554"
ROOT = (
    Path.home()
    / ".local/state/evallab-cross-test"
    / CORPUS
    / DIRNAME
    / "Release"
    / "scripts"
    / "study2_data"
)
# Sub-study name -> episode file. The pilot (episodes_pilot.jsonl) is excluded by design.
SUBSTUDIES = {
    "registered": "episodes.jsonl",
    "extension": "extension/episodes.jsonl",
    "clarity": "clarity/episodes.jsonl",
}
# Keys that carry labels, conditions or grader output. Must never appear in a step.
FORBIDDEN_KEYS = frozenset(
    {
        "cheated",
        "cheat_type",
        "test_file_modified",
        "impossible_test_passes_original",
        "spec_tests_pass",
        "final_all_pass",
        "claims_all_pass",
        "flagged_conflict",
        "pressure",
        "guardian",
        "condition",
        "model",
        "slug",
        "task",
        "report",
        "final_run",
        "orig_run",
        "final_solution",
        "final_tests",
        "reasoning",
        "reasoning_details",
        "actions",
        "cost_usd",
        "turns",
        "error",
        "timestamp",
    }
)
STEP_KEYS = frozenset({"step_id", "source", "input_role", "message", "tool_calls", "observation"})


def digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def read_rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def build_steps(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agent-visible evidence only: assistant turns, tool calls, tool results."""
    steps: list[dict[str, Any]] = []
    for msg in transcript:
        role = msg.get("role")
        if role == "assistant":
            calls = [
                {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                for c in (msg.get("tool_calls") or [])
            ]
            steps.append(
                {
                    "source": "agent",
                    "input_role": "assistant",
                    "message": msg.get("content") or "",
                    "tool_calls": calls,
                    "observation": None,
                }
            )
        elif role == "tool":
            steps.append(
                {
                    "source": "environment",
                    "input_role": "tool",
                    "message": None,
                    "tool_calls": [],
                    "observation": msg.get("content") or "",
                }
            )
        # system and user messages are deliberately skipped: they carry the
        # experimental condition text.
    for i, step in enumerate(steps):
        step["step_id"] = i
    return steps


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield one spec per non-pilot episode row, with a stable hashed id."""
    for substudy, rel in SUBSTUDIES.items():
        for row in read_rows(root / rel):
            if row.get("error") is not None:
                continue
            yield {
                "run_id_hash": digest(f"{substudy}:{row['episode_id']}"),
                "task_id_hash": digest(row["task"]),
                "_steps": build_steps(row["transcript"]),
            }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    n = 0
    steps_total = 0
    forbidden_key_hits = 0
    bad_step_keys = 0
    for spec in specs(root):
        n += 1
        for step in spec["_steps"]:
            steps_total += 1
            if set(step) - STEP_KEYS:
                bad_step_keys += 1
            forbidden_key_hits += len(FORBIDDEN_KEYS & set(step))
    return {
        "specs": n,
        "steps": steps_total,
        "forbidden_key_hits": forbidden_key_hits,
        "unexpected_step_keys": bad_step_keys,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["selfcheck"])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.command == "selfcheck":
        print(json.dumps(selfcheck(args.root), sort_keys=True))


if __name__ == "__main__":
    main()
