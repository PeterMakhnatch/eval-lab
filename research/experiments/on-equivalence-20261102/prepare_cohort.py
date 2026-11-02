#!/usr/bin/env python3
"""Prepares the blind evaluation cohort of 74 mutants (22 survivors + 51 killed controls + 1 unexecuted).
Assigns opaque IDs MUT-01 .. MUT-74 with deterministic shuffle.
Outputs blind_items.json and opaque_mapping.json.
"""

from __future__ import annotations

import ast
import json
import random
from pathlib import Path
from typing import Any

from evallab.mutant_auditor import parse_probe

CENSUS_PATH = Path("research/experiments/on-weak-grader-census-20261102/census.json")
A2_TASKS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A2/tasks")
CERT_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A2B-EQUIV/receipts")
DURABLE_RECEIPTS = Path.home() / "Developer/eval-lab-results/2026-11-02/A2B-EQUIV/receipts"


def get_context(source: str, line_no: int) -> str:
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
        enclosing = None
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and hasattr(node, "lineno")
                and hasattr(node, "end_lineno")
                and node.lineno <= line_no <= node.end_lineno
                and (enclosing is None or (node.end_lineno - node.lineno < enclosing.end_lineno - enclosing.lineno))
            ):
                enclosing = node
        if enclosing:
            start = max(1, enclosing.lineno)
            end = min(len(lines), enclosing.end_lineno)
            if end - start > 100:
                start = max(1, line_no - 40)
                end = min(len(lines), line_no + 40)
            return "\n".join(f"{i:4d} | {lines[i-1]}" for i in range(start, end + 1))
    except Exception:
        pass
    start = max(1, line_no - 30)
    end = min(len(lines), line_no + 30)
    return "\n".join(f"{i:4d} | {lines[i-1]}" for i in range(start, end + 1))


def main() -> None:
    CERT_RECEIPTS.mkdir(parents=True, exist_ok=True)
    DURABLE_RECEIPTS.mkdir(parents=True, exist_ok=True)

    with open(CENSUS_PATH) as f:
        census = json.load(f)

    raw_items: list[dict[str, Any]] = []

    for task in census["tasks"]:
        tid = task["task_id"]
        mutants = task.get("mutants", [])
        if not mutants:
            continue

        pkg_path = Path(task["package_path"])
        inst_path = pkg_path / "instruction.md"
        instruction = inst_path.read_text().strip()

        probe_path = A2_TASKS_DIR / tid / "probe" / "result.json"
        pr = json.loads(probe_path.read_text())
        sources = parse_probe(pr.get("agent_log", ""))

        for m in mutants:
            cell = m["cell"]
            op = m["operator"]
            line = m["line"]
            rel_path = m["path"]
            reward = float(m["reward"]) if m.get("reward") is not None else None
            applied = bool(m.get("applied", False))
            is_survivor = (reward == 1.0)
            if is_survivor:
                status = "survivor"
            elif reward == 0.0:
                status = "killed"
            else:
                status = "unexecuted"

            diff_path = A2_TASKS_DIR / tid / f"{op}-L{line}.diff"
            if not diff_path.exists():
                diff_path = A2_TASKS_DIR / tid / cell / "trial" / "verifier" / "agent.diff"
            diff_text = diff_path.read_text().strip()

            source_code = sources[rel_path]
            context_code = get_context(source_code, line)

            raw_items.append({
                "task_id": tid,
                "cell": cell,
                "operator": op,
                "line": line,
                "path": rel_path,
                "reward": reward,
                "applied": applied,
                "is_survivor": is_survivor,
                "status": status,
                "instruction": instruction,
                "context_code": context_code,
                "diff_text": diff_text,
            })

    assert len(raw_items) == 74, f"Expected 74 mutants, got {len(raw_items)}"
    survivors_count = sum(1 for item in raw_items if item["status"] == "survivor")
    killed_count = sum(1 for item in raw_items if item["status"] == "killed")
    unexecuted_count = sum(1 for item in raw_items if item["status"] == "unexecuted")
    assert survivors_count == 22, f"Expected 22 survivors, got {survivors_count}"
    assert killed_count == 51, f"Expected 51 killed, got {killed_count}"
    assert unexecuted_count == 1, f"Expected 1 unexecuted, got {unexecuted_count}"

    # Deterministic shuffle using seed 20261102
    rng = random.Random(20261102)
    # Sort first by stable key to ensure deterministic order across systems
    raw_items.sort(key=lambda x: f"{x['task_id']}::{x['cell']}")
    rng.shuffle(raw_items)

    mapping: list[dict[str, Any]] = []
    blind_items: list[dict[str, Any]] = []

    for idx, item in enumerate(raw_items, start=1):
        opaque_id = f"MUT-{idx:02d}"
        
        # Ground-truth mapping (to be sealed during judging)
        mapping.append({
            "opaque_id": opaque_id,
            "task_id": item["task_id"],
            "cell": item["cell"],
            "operator": item["operator"],
            "line": item["line"],
            "path": item["path"],
            "reward": item["reward"],
            "applied": item["applied"],
            "is_survivor": item["is_survivor"],
            "status": item["status"],
        })

        # Blind item presented to judge (zero ground truth labels)
        blind_items.append({
            "opaque_id": opaque_id,
            "instruction": item["instruction"],
            "context_file": item["path"],
            "context_code": item["context_code"],
            "diff_text": item["diff_text"],
        })

    # Save to both receipt locations
    for target_dir in [CERT_RECEIPTS, DURABLE_RECEIPTS]:
        (target_dir / "opaque_mapping.json").write_text(json.dumps(mapping, indent=2))
        (target_dir / "blind_items.json").write_text(json.dumps(blind_items, indent=2))

    print(f"Prepared {len(blind_items)} blind items ({survivors_count} survivors, {killed_count} killed controls, {unexecuted_count} unexecuted).")
    print(f"Saved mapping and blind items to {CERT_RECEIPTS} and {DURABLE_RECEIPTS}.")


if __name__ == "__main__":
    main()
