#!/usr/bin/env python3
"""Deterministic PROGRAM.json required-key / enum walk. No Harbor I/O."""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATUSES = {
    "idea",
    "designed",
    "proposed",
    "waiting",
    "approved",
    "running",
    "completed",
    "analyzed",
    "stopped",
    "superseded",
}
REQUIRED = (
    "id",
    "research_question",
    "hypothesis",
    "primary_variable",
    "fixed_elicitation",
    "task_cohort",
    "agent",
    "model",
    "profile",
    "k",
    "power_rationale",
    "status",
    "references",
    "blocker",
    "next_action",
    "predecessor",
    "decision_rule",
    "stopping_condition",
    "notes",
)
REF_KEYS = ("spec", "queue", "jobs", "analysis", "cards")


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    payload = json.loads(path.read_text())
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    experiments = payload.get("experiments")
    if not isinstance(experiments, list) or not experiments:
        errors.append("experiments must be a non-empty list")
        return errors
    seen: set[str] = set()
    for index, item in enumerate(experiments):
        prefix = f"experiments[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} is not an object")
            continue
        missing = [key for key in REQUIRED if key not in item]
        if missing:
            errors.append(f"{prefix} missing {missing}")
        exp_id = item.get("id")
        if not isinstance(exp_id, str) or not exp_id.startswith("EXP-"):
            errors.append(f"{prefix}.id must be an EXP- string")
        elif exp_id in seen:
            errors.append(f"duplicate id {exp_id}")
        else:
            seen.add(exp_id)
        status = item.get("status")
        if status not in STATUSES:
            errors.append(f"{prefix}.status {status!r} not in enum")
        refs = item.get("references")
        if isinstance(refs, dict):
            for key in REF_KEYS:
                if key not in refs:
                    errors.append(f"{prefix}.references missing {key}")
                elif not isinstance(refs[key], list):
                    errors.append(f"{prefix}.references.{key} must be a list")
        else:
            errors.append(f"{prefix}.references must be an object")
        if "k" in item and not isinstance(item["k"], int):
            errors.append(f"{prefix}.k must be an int")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parent
    path = root / "PROGRAM.json"
    errors = validate(path)
    if errors:
        print("PROGRAM.json INVALID")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"PROGRAM.json OK ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
