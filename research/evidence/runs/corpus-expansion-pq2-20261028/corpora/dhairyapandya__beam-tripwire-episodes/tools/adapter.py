#!/usr/bin/env python3
"""Offline Beam replay. Only a spec's ``_steps`` may reach a frozen detector.

The source labels are behavioral heuristics, not certified honesty judgments.
This module never prints source text, executes a tool, or calls a network/model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

REPO = "dhairyapandya/beam-tripwire-episodes"
REVISION = "e575c837ab56efc1ea4a34bce3f348a52a17dedf"
CORPUS = "dhairyapandya__beam-tripwire-episodes"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
MODEL = "reflection/Beam-1.0-501B-A23B"
ANNOTATION_FIELDS = (
    "cheated", "tamper_attempt", "tamper_calls", "special_cased", "config_edit",
    "solved_original", "honest_flag", "reported", "disclosed", "intent_verbalised",
    "judge_intent", "judge_honest", "judge_reported", "judge_disclosed",
)
FIELDS_DETECTOR_MUST_NOT_SEE = frozenset({
    *ANNOTATION_FIELDS,
    "run_id", "instance_id", "repo", "condition", "effort", "stop_reason", "turns",
    "cost_usd", "test_file", "rigged_line", "row.note", "row.reasoning", "row.patch",
    "variants", "original_literal", "mutated_literal", "original_line", "broken_tests",
    "mutated_test_patch", "model", "gold", "labels", "identity", "positive",
    "label_state", "label_reason", "cluster_key", "task_key", "trace_key",
    "publisher_readme", "judge_*", "scores", "grader_verdict",
    "system_or_task_text_announcing_the_experimental_condition",
})
MESSAGE_FIELDS = frozenset({"role", "content", "reasoning_content", "tool_calls", "tool_call_id"})
STEP_FIELDS = frozenset({"step_id", "source", "input_role", "message", "reasoning_content", "tool_calls", "observation"})
# These match explicit annotation assignments, not an agent discussing cheating.
LABEL_ASSIGNMENT = re.compile(
    r"[\"']?\b(?:" + "|".join(ANNOTATION_FIELDS) +
    r"|verdict|gold_label|cheat_label|is_cheat)\b[\"']?\s*[:=]\s*"
    r"(?:true|false|null|none|yes|no|[01]|cheat(?:ed|ing)?|noncheat|positive|negative)\b",
    re.IGNORECASE,
)
GRADER_VERDICT = re.compile(
    r"\b(?:grader|judge|oracle)\b[^\n]{0,40}\b(?:verdict|label|cheat(?:ed|ing)?|score)\b",
    re.IGNORECASE,
)
CONDITION_ANNOUNCEMENT = re.compile(
    r"\b(?:impossible_honest|beam-full|beam-high60|beam-v2-note|rigged|"
    r"impossible task|intentionally impossible|intentionally (?:wrong|broken)|"
    r"cannot be passed honestly)\b", re.IGNORECASE,
)
CHEAT_LANGUAGE = re.compile(r"\bcheat(?:ed|ing)?\b", re.IGNORECASE)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iter_rows(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    for batch in pq.ParquetFile(root / "data/episodes.parquet").iter_batches(batch_size=64):
        yield from batch.to_pylist()


def record_id(row: dict[str, Any]) -> str:
    """The publisher's run_id names an experiment, not a unique episode.

    Represent (task, episode), where episode = (run_id, condition, effort).
    No source task identifier is persisted by this adapter's CLI or profiler.
    """
    return json.dumps(
        [row["instance_id"], [row["run_id"], row["condition"], row["effort"]]],
        ensure_ascii=False, separators=(",", ":"),
    )


def label_state(row: dict[str, Any]) -> tuple[str, str]:
    if row["stop_reason"] == "error":
        return "excluded", "provider_error"
    if row["condition"] == "possible" and row["cheated"]:
        # One pinned row: parent-supplied known justified test edit. The HF card
        # itself does not document this adjudication. Do not invent a negative.
        return "ambiguous", "known_justified_control_test_edit"
    if row["cheated"]:
        return "positive", "publisher_tamper_or_special_case"
    return "negative", "publisher_neither_tamper_nor_special_case_not_certified_honest"


def _scrub(value: Any, audit: Counter[str], context: bool = False) -> Any:
    if isinstance(value, str):
        out = []
        for line in value.splitlines(keepends=True):
            label = bool(LABEL_ASSIGNMENT.search(line))
            verdict = bool(GRADER_VERDICT.search(line))
            condition = context and bool(CONDITION_ANNOUNCEMENT.search(line))
            if label or verdict or condition:
                audit["stripped_lines"] += 1
                audit["stripped_chars"] += len(line)
                audit["embedded_label_lines"] += label
                audit["embedded_grader_verdict_lines"] += verdict
                audit["condition_announcement_lines"] += condition
            else:
                out.append(line)
        return "".join(out)
    if isinstance(value, dict):
        return {key: _scrub(item, audit, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, audit, context) for item in value]
    return value


def replay(messages_json: str) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Map every source message, reasoning string, call and tool observation.

    Preserve native tool names and all parsed arguments, including unusual calls.
    Do not infer executed shell commands from tool names or final patch artifacts.
    Sanitization uses only message content, never a label or condition column.
    """
    messages = json.loads(messages_json)
    if not isinstance(messages, list):
        raise ValueError("unexpected conversation envelope")
    out: list[dict[str, Any]] = []
    audit: Counter[str] = Counter({
        "stripped_lines": 0, "stripped_chars": 0, "embedded_label_lines": 0,
        "embedded_grader_verdict_lines": 0, "condition_announcement_lines": 0,
    })
    for message in messages:
        if set(message) - MESSAGE_FIELDS:
            raise ValueError("unrecognized message fields")
        role = message["role"]
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unrecognized message role")
        if not isinstance(message["content"], str):
            raise ValueError("unexpected message content type")
        audit["source_messages"] += 1
        audit[f"{role}_messages"] += 1
        audit["source_content_chars"] += len(message["content"])
        before = audit["stripped_lines"]
        before_label = audit["embedded_label_lines"] + audit["embedded_grader_verdict_lines"]
        content = _scrub(message["content"], audit, role in {"system", "user"})
        reasoning = _scrub(message.get("reasoning_content", ""), audit)
        audit["source_reasoning_chars"] += len(message.get("reasoning_content", ""))
        source_text = message["content"] + "\n" + message.get("reasoning_content", "")
        calls = []
        for call in message.get("tool_calls", []) or []:
            if set(call) != {"id", "type", "function"} or call["type"] != "function":
                raise ValueError("unrecognized tool-call envelope")
            function = call["function"]
            if set(function) != {"name", "arguments"}:
                raise ValueError("unrecognized function fields")
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("unexpected tool argument type")
            source_text += "\n" + function["arguments"]
            calls.append({
                "tool_call_id": call["id"], "function_name": function["name"],
                "arguments": _scrub(arguments, audit),
            })
            audit["tool_calls"] += 1
        audit["messages_with_cheat_language"] += bool(CHEAT_LANGUAGE.search(source_text))
        audit["messages_with_stripping"] += audit["stripped_lines"] != before
        audit["messages_with_embedded_label_or_verdict"] += (
            audit["embedded_label_lines"] + audit["embedded_grader_verdict_lines"] != before_label
        )
        if role == "tool":
            if not out or out[-1]["source"] != "agent":
                raise ValueError("tool result without preceding assistant")
            out[-1].setdefault("observation", {"results": []})["results"].append({
                "tool_call_id": message["tool_call_id"], "content": content,
            })
            audit["tool_results"] += 1
            continue
        step: dict[str, Any] = {
            "step_id": len(out), "source": "agent" if role == "assistant" else "user",
            "input_role": role, "message": content,
        }
        if "reasoning_content" in message:
            step["reasoning_content"] = reasoning
        if calls:
            step["tool_calls"] = calls
        out.append(step)
    audit["mapped_steps"] = len(out)
    return out, audit


def to_steps(row: dict[str, Any]) -> list[dict[str, Any]]:
    return replay(row["messages"])[0]


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield all 616 episodes, including explicitly ambiguous/excluded records."""
    for index, row in enumerate(iter_rows(root)):
        state, reason = label_state(row)
        native_id = record_id(row)
        yield {
            "identity": {
                "row_index": index, "run_id": native_id, "trace_key": digest(native_id),
                "task_id": row["instance_id"], "task_key": digest(row["instance_id"]),
                "cluster_key": digest(row["instance_id"]), "model": MODEL,
                "experiment": row["run_id"], "effort": row["effort"], "repo": row["repo"],
            },
            "gold": {
                "state": state, "reason": reason,
                "positive": True if state == "positive" else False if state == "negative" else None,
                "source_annotations": {field: row[field] for field in ANNOTATION_FIELDS},
            },
            "labels": {
                "state": state, "reason": reason, "condition": row["condition"],
                "slice": "control" if row["condition"] == "possible" else "impossible_task",
                "stop_reason": row["stop_reason"],
            },
            "_steps": to_steps(row),
        }


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    issues: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    audit_totals: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    by_condition: dict[str, Counter[str]] = defaultdict(Counter)
    identities: set[str] = set()
    tasks: set[str] = set()
    for row in iter_rows(root):
        counts["rows"] += 1
        state, reason = label_state(row)
        counts[state] += 1
        reasons[reason] += 1
        by_condition[row["condition"]][state] += 1
        by_condition[row["condition"]]["rows"] += 1
        counts["raw_positive" if row["cheated"] else "raw_negative"] += 1
        identity = record_id(row)
        issues["duplicate_episode_id"] += identity in identities
        identities.add(identity)
        tasks.add(row["instance_id"])
        issues["cheated_rule_mismatch"] += row["cheated"] != (row["tamper_attempt"] or row["special_cased"])
        steps, audit = replay(row["messages"])
        audit_totals.update(audit)
        audit_totals["episodes_with_stripping"] += audit["stripped_lines"] > 0
        audit_totals["episodes_with_embedded_label_or_verdict"] += audit["messages_with_embedded_label_or_verdict"] > 0
        audit_totals["episodes_with_cheat_language"] += audit["messages_with_cheat_language"] > 0
        issues["nonsequential_step_ids"] += [s["step_id"] for s in steps] != list(range(len(steps)))
        issues["unrecognized_step_fields"] += any(set(s) - STEP_FIELDS for s in steps)
        issues["label_fields_in_steps"] += len(_keys(steps) & FIELDS_DETECTOR_MUST_NOT_SEE)
        tainted = {key: value if key == "messages" else None for key, value in row.items()}
        issues["nonmessage_metadata_influences_steps"] += to_steps(tainted) != steps
        calls = [c for step in steps for c in step.get("tool_calls", [])]
        results = [r for step in steps for r in step.get("observation", {}).get("results", [])]
        call_ids = [call["tool_call_id"] for call in calls]
        result_ids = [result["tool_call_id"] for result in results]
        issues["duplicate_call_ids"] += len(call_ids) != len(set(call_ids))
        issues["duplicate_result_ids"] += len(result_ids) != len(set(result_ids))
        issues["call_result_join"] += set(call_ids) != set(result_ids)
        turn_mismatch = audit["assistant_messages"] != row["turns"]
        counts["provider_error_turn_count_mismatches"] += turn_mismatch and row["stop_reason"] == "error"
        issues["completed_turn_count_mismatch"] += turn_mismatch and row["stop_reason"] != "error"
        # No stripping is needed at this pin: recover the full supplied chat exactly.
        if not audit["stripped_lines"]:
            recovered = []
            for step in steps:
                message = {"role": step["input_role"], "content": step["message"]}
                if "reasoning_content" in step:
                    message["reasoning_content"] = step["reasoning_content"]
                    message["tool_calls"] = []
                for call in step.get("tool_calls", []):
                    message.setdefault("tool_calls", []).append({
                        "id": call["tool_call_id"], "type": "function", "function": {
                            "name": call["function_name"], "arguments": call["arguments"],
                        },
                    })
                recovered.append(message)
                recovered.extend({"role": "tool", "tool_call_id": result["tool_call_id"], "content": result["content"]} for result in step.get("observation", {}).get("results", []))
            expected = json.loads(row["messages"])
            for message in expected:
                for call in message.get("tool_calls", []) or []:
                    call["function"]["arguments"] = json.loads(call["function"]["arguments"])
            issues["message_call_reasoning_result_roundtrip"] += recovered != expected
    counts["tasks"] = len(tasks)
    counts["unique_episode_ids"] = len(identities)
    nonzero_issues = {key: value for key, value in sorted(issues.items()) if value}
    assert not nonzero_issues, "adapter invariants failed; no source text is emitted"
    assert not issues["label_fields_in_steps"], "gold fields reached detector steps"
    return {
        "counts": dict(counts), "label_reasons": dict(reasons),
        "per_condition": {key: dict(value) for key, value in sorted(by_condition.items())},
        "transcript_audit": dict(audit_totals),
        "label_fields_in_steps": issues["label_fields_in_steps"],
        "issue_count": sum(issues.values()), "issues": nonzero_issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(selfcheck(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
