#!/usr/bin/env python3
"""Summarize native coding/navigation episodes without exporting their text or IDs.

Native CLI episodes are not individual provider requests. Reported input includes
cache reads; uncached input is a diagnostic proxy, not a subscription invoice.
Missing usage remains unknown, including for failed and never-launched episodes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "uncached_input_tokens",
    "input_plus_output_tokens",
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def measured_tokens(raw: dict[str, Any] | None) -> dict[str, int | None]:
    raw = raw or {}
    values = {
        name: value if type(value := raw.get(name)) is int and value >= 0 else None
        for name in TOKEN_FIELDS[:3]
    }
    input_tokens = values["input_tokens"]
    cached = values["cached_input_tokens"]
    output = values["output_tokens"]
    if input_tokens is not None and cached is not None and cached > input_tokens:
        raise ValueError("reported cached input exceeds total input")
    values["uncached_input_tokens"] = (
        None if input_tokens is None or cached is None else input_tokens - cached
    )
    values["input_plus_output_tokens"] = (
        None if input_tokens is None or output is None else input_tokens + output
    )
    return values


def command_inventory(path: Path) -> dict[str, Any]:
    """Deduplicate lifecycle updates by native item ID, never by command text."""
    items: dict[str, str] = {}
    invalid_lines = 0
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        identifier, kind = item.get("id"), item.get("type")
        if not isinstance(identifier, str) or not isinstance(kind, str):
            continue
        if identifier in items and items[identifier] != kind:
            raise ValueError("one native item ID has inconsistent types")
        items[identifier] = kind
    kinds = Counter(items.values())
    return {
        "command_execution_items": kinds["command_execution"],
        "file_change_items": kinds["file_change"],
        "other_nonmessage_items": dict(
            sorted(
                (kind, count)
                for kind, count in kinds.items()
                if kind not in {"command_execution", "file_change", "agent_message", "reasoning"}
            )
        ),
        "invalid_jsonl_lines": invalid_lines,
        "source_sha256": source_sha256(path),
    }


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    token_totals = {}
    for field in TOKEN_FIELDS:
        known = [row["tokens"][field] for row in rows if row["tokens"][field] is not None]
        token_totals[field] = {
            "reported_subtotal": sum(known) if known else None,
            "known_episodes": len(known),
            "planned_episodes": len(rows),
            "complete": bool(rows) and len(known) == len(rows),
        }
    commands = [
        row["native_items"]["command_execution_items"]
        for row in rows
        if row["native_items"] is not None
    ]
    return {
        "planned_episodes": len(rows),
        "attempted_episodes": sum(row["attempted"] for row in rows),
        "accepted_episodes": sum(row["accepted"] for row in rows),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
        "tokens": token_totals,
        "command_execution_items": {
            "reported_subtotal": sum(commands) if commands else None,
            "known_episodes": len(commands),
            "planned_episodes": len(rows),
            "complete": bool(rows) and len(commands) == len(rows),
        },
    }


def reductions(arms: dict[str, dict[str, Any]]) -> dict[str, float | None]:
    result = {}
    for field in TOKEN_FIELDS:
        baseline = arms["baseline"]["tokens"][field]
        candidate = arms["candidate"]["tokens"][field]
        base_value, candidate_value = baseline["reported_subtotal"], candidate["reported_subtotal"]
        result[field] = (
            100.0 * (base_value - candidate_value) / base_value
            if baseline["complete"]
            and candidate["complete"]
            and base_value
            and candidate_value is not None
            else None
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("coding", "navigation"))
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--relocate-events",
        nargs=2,
        type=Path,
        metavar=("RECORDED_ROOT", "RETAINED_ROOT"),
        help="Map absolute coding-event paths into retained evidence without changing receipts",
    )
    args = parser.parse_args()
    if args.relocate_events is not None and args.kind != "coding":
        parser.error("--relocate-events applies only to coding receipts")
    relocation = (
        tuple(path.resolve() for path in args.relocate_events)
        if args.relocate_events is not None
        else None
    )
    run = args.run.resolve()
    input_file = run / ("summary.json" if args.kind == "coding" else "scores.json")
    raw = read_json(input_file)
    source_rows = (
        raw["attempts"]
        if args.kind == "coding"
        else [
            attempt
            for task in raw["tasks"]
            for repeat in task["repeats"]
            for attempt in repeat["attempts"]
        ]
    )
    if not source_rows:
        raise ValueError("no planned native episodes in the source summary")
    arm_labels = (
        {"baseline": "baseline", "candidate": "candidate"}
        if args.kind == "coding"
        else {"full_outline": "baseline", "bounded_readout": "candidate"}
    )
    rows = []
    aliases: dict[str, str] = {}
    for item in source_rows:
        identifier = item["task"] if args.kind == "coding" else item["task_id"]
        if identifier not in aliases:
            aliases[identifier] = (
                identifier if args.kind == "coding" else f"source-{len(aliases) + 1:02d}"
            )
        if args.kind == "coding":
            events = item.get("events") or {}
            event_file = Path(events["raw_event_log"]) if events.get("raw_event_log") else None
            if event_file is not None and relocation is not None:
                recorded_root, retained_root = relocation
                if not event_file.is_absolute():
                    raise ValueError("recorded event paths must be absolute for relocation")
                event_file = retained_root / event_file.resolve().relative_to(recorded_root)
            accepted = item["accepted"]
            tokens = measured_tokens(events.get("tokens"))
        else:
            event_file = run / "attempts" / item["attempt_id"] / "cli" / "stdout.jsonl"
            accepted = item["score"]["passed"]
            tokens = measured_tokens(item.get("tokens"))
        if item["arm"] not in arm_labels:
            raise ValueError(f"unsupported recorded study arm: {item['arm']!r}")
        rows.append(
            {
                "task": aliases[identifier],
                "repeat": item["repeat"],
                "arm": arm_labels[item["arm"]],
                "source_arm": item["arm"],
                "status": item["status"],
                "attempted": item["status"] != "not_attempted",
                "accepted": accepted,
                "tokens": tokens,
                "requested_effort": item.get("requested_effort"),
                "native_items": command_inventory(event_file)
                if event_file is not None and event_file.is_file()
                else None,
            }
        )
    arms = {
        arm: totals([row for row in rows if row["arm"] == arm]) for arm in ("baseline", "candidate")
    }
    per_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per_task[row["task"]].append(row)
    tasks = []
    for name, selected in per_task.items():
        task_arms = {
            arm: totals([row for row in selected if row["arm"] == arm])
            for arm in ("baseline", "candidate")
        }
        tasks.append(
            {"task": name, "arms": task_arms, "candidate_reduction_percent": reductions(task_arms)}
        )
    report = {
        "schema": "agent-efficiency-native-analysis/v1",
        "kind": args.kind,
        "source_summary_sha256": source_sha256(input_file),
        "analyzer_sha256": source_sha256(Path(__file__)),
        "units": {
            "episodes": "native CLI invocations, not individual provider requests",
            "tokens": "reported native cumulative usage; total input includes cached input",
            "cost": "unknown; subscription allowance and physical upstream attempts are not priced by these counters",
        },
        "original_cases": len(per_task),
        "arms": arms,
        "candidate_reduction_percent": reductions(arms),
        "tasks": tasks,
        "episodes": rows,
        "quality_limit": "Exploratory convenience cases and nested repeats; observed acceptance is not general quality non-inferiority. Failed/missing episodes remain in the planned denominator.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {
                "kind": args.kind,
                "original_cases": len(per_task),
                "arms": arms,
                "candidate_reduction_percent": report["candidate_reduction_percent"],
                "output": str(args.output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
