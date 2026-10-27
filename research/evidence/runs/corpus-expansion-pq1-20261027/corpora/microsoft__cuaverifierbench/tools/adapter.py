#!/usr/bin/env python3
"""Local CUAVerifierBench text projection; gold is outcome correctness, NOT cheating.

No corpus text is written or printed. Only ``spec['_steps']`` may enter a detector;
all remaining spec fields are scorer-side. Native browser actions stay native:
these are not shell commands and must never be presented as executable bash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

REPO = "microsoft/CUAVerifierBench"
REVISION = "c19eb323cd802add5c3d2840ff13044061364867"
SPLIT = "fara7b_om2w_browserbase"
MODEL = "Fara-7B"
ROOT = Path.home() / ".local/state/evallab-cross-test/microsoft__cuaverifierbench/c19eb323cd80"
DATA_FILES = (
    "annotations/fara7b_om2w_browserbase-00000-of-00001.parquet",
    "trajectories/fara7b_om2w_browserbase-00000-of-00002.parquet",
    "trajectories/fara7b_om2w_browserbase-00001-of-00002.parquet",
)
INPUT_COLUMNS = ("task_id", "instruction", "init_url", "web_surfer_log", "final_answer")
REVIEW_COLUMNS = (
    "task_id",
    "annotator",
    "human_judgement_outcome",
    "human_judgement_process",
    "human_process_score",
)
EVENT_FIELDS = {"timestamp", "type", "source", "message", "action", "arguments", "url"}
CONTEXT_FIELDS = ("timestamp", "type", "source", "message", "url")


def trace_key(record_id: str) -> str:
    return hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:16]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def trajectory_rows(
    root: Path = ROOT, columns: Sequence[str] = INPUT_COLUMNS
) -> Iterator[dict[str, Any]]:
    """Projected reads deliberately never request the binary screenshots column."""
    for relative in DATA_FILES[1:]:
        parquet = pq.ParquetFile(root / relative)
        for batch in parquet.iter_batches(batch_size=8, columns=list(columns)):
            yield from batch.to_pylist()


def load_reviews(root: Path = ROOT) -> dict[str, list[dict[str, Any]]]:
    """Blind fields only: no informed comments/agreements or verifier outputs."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for batch in pq.ParquetFile(root / DATA_FILES[0]).iter_batches(
        batch_size=256, columns=list(REVIEW_COLUMNS)
    ):
        for row in batch.to_pylist():
            if row["human_judgement_outcome"] not in {"Correct", "Incorrect"}:
                raise ValueError("blind outcome is missing or outside the vetted label schema")
            pair = (row["task_id"], row["annotator"])
            if pair in seen:
                raise ValueError("duplicate task/reviewer pair")
            seen.add(pair)
            grouped[row["task_id"]].append(row)
    return dict(grouped)


def blind_gold(reviews: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Strict blind majority; all 1-1 ties resolve to Incorrect, as published."""
    if not reviews:
        raise ValueError("trajectory has no blind outcome review")
    counts = Counter(review["human_judgement_outcome"] for review in reviews)
    if not set(counts) <= {"Correct", "Incorrect"}:
        raise ValueError("blind outcome has an unrecognized label")
    return {
        "correct": counts["Correct"] > counts["Incorrect"],
        "tie": counts["Correct"] == counts["Incorrect"],
        "correct_reviews": counts["Correct"],
        "incorrect_reviews": counts["Incorrect"],
        "provenance": "UV-blind human_judgement_outcome; strict majority; ties Incorrect",
    }


def events(log: str) -> Iterator[dict[str, Any]]:
    """The log is JSONL, not a single JSON document; blank separators are ignored."""
    for line in log.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ValueError("log event fields differ from the pinned schema")
        if not all(isinstance(event[field], str) for field in CONTEXT_FIELDS):
            raise ValueError("log event context is not text")
        arguments = event["arguments"]
        if not isinstance(arguments, dict) or not isinstance(arguments.get("thoughts"), str):
            raise ValueError("log event thoughts or arguments are malformed")
        if not isinstance(event["action"], str) or not event["action"]:
            raise ValueError("log event has no native browser action")
        if arguments.get("action") != event["action"]:
            raise ValueError("outer and inner browser action names disagree")
        yield event


def to_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Semantic text-preserving ATIF projection, with one step per native action.

    Event context is a JSON object in ``message`` so its five original string
    fields remain separately recoverable. Thoughts go to ``reasoning_content``;
    other native arguments go to ``tool_calls``. No tool observation or timing
    relationship is invented. Images and their paths are never loaded.
    """
    for field in INPUT_COLUMNS:
        if not isinstance(row[field], str):
            raise ValueError("an input text field is missing or not a string")
    steps: list[dict[str, Any]] = [
        {"step_id": 0, "source": "user", "message": row["instruction"]},
        {"step_id": 1, "source": "user", "message": row["init_url"]},
    ]
    for index, event in enumerate(events(row["web_surfer_log"]), 1):
        steps.append(
            {
                "step_id": len(steps),
                "source": "agent",
                "message": json.dumps(
                    {field: event[field] for field in CONTEXT_FIELDS}, ensure_ascii=False
                ),
                "reasoning_content": event["arguments"]["thoughts"],
                "tool_calls": [
                    {
                        "tool_call_id": f"browser-action-{index}",
                        "function_name": event["action"],
                        "arguments": {
                            key: value
                            for key, value in event["arguments"].items()
                            if key != "thoughts"
                        },
                    }
                ],
            }
        )
    steps.append({"step_id": len(steps), "source": "agent", "message": row["final_answer"]})
    return steps


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """In-memory scorer specs; only ``_steps`` is a candidate-verifier input.

    ``positive`` denotes an INCORRECT OUTCOME, never cheating. ``trace_id`` exists
    only for joins in memory; receipts must use its hashed ``trace_key`` instead.
    Neither final adjudication nor model-verifier outputs are read here.
    """
    reviews = load_reviews(root)
    for row in trajectory_rows(root, (*INPUT_COLUMNS, "majority_human_outcome_vote")):
        gold = blind_gold(reviews.get(row["task_id"], []))
        if row["majority_human_outcome_vote"] != int(gold["correct"]):
            raise ValueError("blind gold and published majority aggregate disagree")
        yield {
            "corpus": "microsoft__cuaverifierbench",
            "trace_id": row["task_id"],
            "trace_key": trace_key(row["task_id"]),
            "task_key": trace_key(row["task_id"]),
            "model": MODEL,
            "split": SPLIT,
            "evaluation_kind": "fixed_run_correctness_wrong_pass_not_cheating",
            "positive": not gold["correct"],
            "gold": gold,
            "_steps": to_steps(row),
        }


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Check ALL rows; emit only aggregates and row indices, never source text."""
    reviews = load_reviews(root)
    counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    issues: list[dict[str, Any]] = []
    ids = []
    for row_index, row in enumerate(
        trajectory_rows(root, (*INPUT_COLUMNS, "majority_human_outcome_vote")), 1
    ):
        counts["rows"] += 1
        ids.append(row["task_id"])
        try:
            gold = blind_gold(reviews.get(row["task_id"], []))
            original_events = list(events(row["web_surfer_log"]))
            steps = to_steps(row)
        except (ValueError, TypeError, KeyError):
            issues.append({"row_index": row_index, "kind": "input_or_label_structure"})
            continue
        counts["positive_incorrect" if not gold["correct"] else "negative_correct"] += 1
        counts["blind_ties_to_incorrect"] += gold["tie"]
        if row["majority_human_outcome_vote"] != int(gold["correct"]):
            issues.append({"row_index": row_index, "kind": "blind_majority_mismatch"})
        if [step["step_id"] for step in steps] != list(range(len(steps))):
            issues.append({"row_index": row_index, "kind": "step_indices"})
        if steps[0]["message"] != row["instruction"] or steps[1]["message"] != row["init_url"]:
            issues.append({"row_index": row_index, "kind": "task_text_preservation"})
        if steps[-1]["message"] != row["final_answer"]:
            issues.append({"row_index": row_index, "kind": "final_answer_preservation"})
        action_steps = steps[2:-1]
        if len(action_steps) != len(original_events):
            issues.append({"row_index": row_index, "kind": "action_count"})
            continue
        for index, (event, step) in enumerate(zip(original_events, action_steps, strict=True), 1):
            call = step["tool_calls"][0]
            recovered = json.loads(step["message"])
            recovered["action"] = call["function_name"]
            recovered["arguments"] = {**call["arguments"], "thoughts": step["reasoning_content"]}
            if recovered != event:
                issues.append(
                    {"row_index": row_index, "event_index": index, "kind": "log_text_loss"}
                )
            if call["tool_call_id"] != f"browser-action-{index}":
                issues.append(
                    {"row_index": row_index, "event_index": index, "kind": "tool_call_id"}
                )
            if set(step) != {"step_id", "source", "message", "reasoning_content", "tool_calls"}:
                issues.append(
                    {"row_index": row_index, "event_index": index, "kind": "step_field_boundary"}
                )
            action_counts[event["action"]] += 1
        if [step["source"] for step in steps[:2]] != ["user", "user"] or any(
            step["source"] != "agent" for step in steps[2:]
        ):
            issues.append({"row_index": row_index, "kind": "step_sources"})
        counts["browser_actions"] += len(original_events)
        counts["steps"] += len(steps)
        counts["log_events_semantically_preserved"] += len(original_events)
        counts["input_text_fields_preserved"] += 4
    if len(set(ids)) != len(ids) or len({trace_key(value) for value in ids}) != len(ids):
        issues.append({"kind": "duplicate_record_id_or_trace_key"})
    if set(reviews) != set(ids):
        issues.append({"kind": "annotation_join"})
    if counts["rows"] != 106 or sum(len(items) for items in reviews.values()) != 215:
        issues.append({"kind": "pinned_slice_counts"})
    if (counts["positive_incorrect"], counts["negative_correct"]) != (77, 29):
        issues.append({"kind": "pinned_gold_counts"})
    return {
        **dict(sorted(counts.items())),
        "annotations": sum(len(items) for items in reviews.values()),
        "native_action_counts": dict(sorted(action_counts.items())),
        "unlabeled": 0,
        "screenshots_decoded": 0,
        "gold_fields_in_detector_step_schema": 0,
        "issues": issues,
        "issue_count": len(issues),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    result = selfcheck(args.root)
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result["issues"]))


if __name__ == "__main__":
    main()
