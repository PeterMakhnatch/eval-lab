#!/usr/bin/env python3
"""Offline run replay. Only ``_steps`` may reach a frozen detector, never the spec.

Source code, trees, logs, configs and graders are never executed. Labels come
only from grades/grades.jsonl, not the duplicated run columns or auxiliary judge.
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

CORPUS = "noahrossi__sdf-shortcut-taking"
REPO = "noahrossi/sdf-shortcut-taking"
REVISION = "a501097987933ddd5c81359730a2f514860c948c"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
TASK_ID = "precommit_hook"
OUTCOMES = frozenset({"fixed", "shortcut", "no_commit", "error"})
FIELDS_DETECTOR_MUST_NOT_SEE = frozenset(
    {
        "gold", "labels", "identity", "positive", "state", "exclusion_reasons",
        "negative_class", "outcome", "run_id", "task_id", "model_label", "model",
        "model_label_right", "model_arm", "batch", "run_name", "quarantined",
        "target_errors", "max_steps", "n_steps", "n_step_dirs", "final_step",
        "n_messages", "has_log", "workarounds", "evidence", "notes", "committed",
        "hit_max_steps", "ended_by_agent", "empty_final_turn", "hook", "src",
        "verify", "commit_step", "final_errors", "mypy_tail", "hook_rc",
        "runs_mypy", "exists", "executable", "deleted", "staged_only",
        "staged_gate", "full_scope", "subset", "toothless", "early_exit",
        "loose_flags", "config_target", "n_lines", "probe_blocks", "files_modified",
        "file_level_ignores", "inline_type_ignores", "any_count", "missing_files",
        "deleted_defs", "pristine_defs", "broken_files", "type_ignore",
        "code_deleted", "config_weakened", "no_verify", "hook_inert",
        "nonblocking_hook", "hook_removed", "transient", "mypy_flags", "scope_subset",
        "judge_model", "cost", "workaround", "disclosure", "user_intent",
        "disapproval", "tedium", "plan", "quote", "rationale", "raw", "error",
        "authoritative_grade", "auxiliary_judge", "verification_history",
        "bootstrap_cluster", "checker_tail_echo", "redactions", "native_message_indices",
        "exploit_announcing_system_prompt", "exploit_announcing_task_text",
        "config_yaml", "state_json", "manifest_json", "log", "tree_files", "blobs",
    }
)
# Strip ordinary in-run summaries conservatively as well as matching checker tails.
# This expression is identical for every run and never conditions on its outcome.
MYPY_SUMMARY = re.compile(
    r"(?m)(?:Success: no issues found in \d+ source files?"
    r"|Found \d+ errors? in \d+ files? \(checked \d+ source files?\))[^\r\n]*"
)
STRUCTURED_VERDICT = re.compile(
    r"[\"'](?:final_errors|hook_rc|mypy_tail|workarounds|commit_step|target_errors)[\"']\s*:"
    r"|[\"']outcome[\"']\s*:"
    r"|\b(?:verdict|outcome)\s*[:=]\s*[\"']?(?:fixed|shortcut|no_commit|error)\b"
    r"|meta/muse-spark-1\.3-contributor",
    re.IGNORECASE,
)
MESSAGE_FIELDS = frozenset(
    {
        "role", "content", "refusal", "annotations", "audio", "function_call",
        "tool_calls", "reasoning_content", "name", "tool_call_id", "reasoning",
        "reasoning_details",
    }
)


def parquet_rows(root: Path, name: str) -> list[dict[str, Any]]:
    return pq.read_table(root / "rollouts" / f"{name}.parquet").to_pylist()


def jsonl_rows(root: Path, name: str) -> list[dict[str, Any]]:
    with (root / "grades" / f"{name}.jsonl").open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def unique_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {row["run_id"]: row for row in rows}
    if len(result) != len(rows) or any(not key for key in result):
        raise ValueError("empty or duplicate run identity; no row content emitted")
    return result


def load(root: Path) -> dict[str, Any]:
    runs = parquet_rows(root, "runs")
    run_index = unique_index(runs)
    grade_rows = jsonl_rows(root, "grades")
    grades = unique_index(grade_rows)
    conversations = unique_index(parquet_rows(root, "messages"))
    if set(conversations) - run_index.keys() or set(grades) - run_index.keys():
        raise ValueError("unmatched conversation or authoritative grade")
    histories: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name in ("judge", "verify"):
        index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in jsonl_rows(root, name):
            index[row["run_id"]].append(row)
        histories[name] = index
    tails = sorted(
        {
            grade["verify"]["mypy_tail"].strip()
            for grade in grade_rows
            if grade["verify"]["mypy_tail"].strip()
            and not MYPY_SUMMARY.search(grade["verify"]["mypy_tail"])
        },
        key=lambda value: (-len(value), value),
    )
    tail_pattern = re.compile("|".join(re.escape(value) for value in tails)) if tails else None
    for run in runs:
        grade = grades.get(run["run_id"])
        if run["quarantined"]:
            if grade is not None or run["outcome"] is not None:
                raise ValueError("quarantined run unexpectedly has authoritative gold")
        elif grade is None or grade["outcome"] not in OUTCOMES:
            raise ValueError("unquarantined run lacks recognized authoritative gold")
        if grade:
            for field, value in grade.items():
                duplicate = run[field]
                if isinstance(value, (dict, list)):
                    duplicate = json.loads(duplicate)
                if duplicate != value:
                    raise ValueError("duplicated run-column gold differs from authority")
    return {
        "runs": runs, "grades": grades, "messages": conversations,
        "histories": histories, "tail_pattern": tail_pattern, "tail_signatures": len(tails),
    }


def reasoning_texts(message: dict[str, Any]) -> list[str]:
    """Keep distinct semantic strings, not duplicate provider encodings."""
    texts = []
    for field in ("reasoning_content", "reasoning"):
        value = message.get(field)
        if value and value not in texts:
            texts.append(value)
    for detail in message.get("reasoning_details") or []:
        if detail["type"] != "reasoning.text" or set(detail) - {"type", "text", "format", "index"}:
            raise ValueError("unrecognized reasoning-detail schema")
        value = detail["text"]
        if value and value not in texts:
            texts.append(value)
    if any(not isinstance(value, str) for value in texts):
        raise ValueError("nontextual reasoning")
    return texts


def arguments_of(call: dict[str, Any]) -> Any:
    arguments = call["function"]["arguments"]
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except json.JSONDecodeError:
            # Preserve a malformed source argument; never invent a command.
            return {"unparsed_arguments": arguments}
    return arguments


def redact_observation(text: str, tail_pattern: re.Pattern[str] | None) -> tuple[str, Counter[str]]:
    summary_free, summaries = MYPY_SUMMARY.subn("", text)
    clean, tails = tail_pattern.subn("", summary_free) if tail_pattern else (summary_free, 0)
    return clean, Counter(
        mypy_summary_matches=summaries,
        nonstandard_checker_tail_matches=tails,
        modified_tool_messages=int(clean != text),
        removed_characters=len(text) - len(clean),
    )


def to_steps(
    messages: list[dict[str, Any]], tail_pattern: re.Pattern[str] | None
) -> tuple[list[dict[str, Any]], Counter[str]]:
    steps = []
    counts: Counter[str] = Counter()
    for index, message in enumerate(messages):
        if set(message) - MESSAGE_FIELDS:
            raise ValueError("unexpected source message field")
        if any(message.get(field) for field in ("refusal", "annotations", "audio", "function_call", "name")):
            raise ValueError("unhandled nonempty semantic message field")
        role = message["role"]
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unrecognized message role")
        content = message["content"]
        if content is not None and not isinstance(content, str):
            raise ValueError("nontextual message content")
        step: dict[str, Any] = {
            "step_id": index,
            "source": "agent" if role in {"assistant", "tool"} else "user",
            "input_role": role,
            "message": content or "",
        }
        if role == "tool":
            clean, changed = redact_observation(content or "", tail_pattern)
            counts.update(changed)
            step["message"] = ""
            step["observation"] = {
                "results": [{"tool_call_id": message["tool_call_id"], "content": clean}]
            }
        thoughts = reasoning_texts(message)
        if thoughts:
            step["reasoning_content"] = "\n".join(thoughts)
        calls = []
        for call in message.get("tool_calls") or []:
            if set(call) - {"id", "function", "type", "index"} or call["type"] != "function":
                raise ValueError("unexpected tool-call schema")
            if set(call["function"]) != {"name", "arguments"}:
                raise ValueError("unexpected function-call schema")
            calls.append(
                {
                    "tool_call_id": call["id"],
                    "function_name": call["function"]["name"],
                    "arguments": arguments_of(call),
                }
            )
        if calls:
            step["tool_calls"] = calls
        steps.append(step)
    return steps, counts


def _specs(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for run in data["runs"]:
        run_id = run["run_id"]
        grade = data["grades"].get(run_id)
        conversation = data["messages"].get(run_id)
        messages = json.loads(conversation["messages_json"]) if conversation else []
        if conversation and len(messages) != run["n_messages"]:
            raise ValueError("conversation length differs from publisher run index")
        outcome = grade["outcome"] if grade else None
        reasons = []
        if outcome == "error":
            reasons.append("error")
        if run["quarantined"]:
            reasons.append("quarantined")
        if not conversation:
            reasons.append("missing_transcript")
        reasons.sort()
        state = "excluded" if reasons else "positive" if outcome == "shortcut" else "negative"
        steps, redactions = to_steps(messages, data["tail_pattern"])
        tail = grade["verify"]["mypy_tail"].strip() if grade else ""
        echoes = sum(
            bool(tail) and message["role"] == "tool" and tail in (message["content"] or "")
            for message in messages
        )
        yield {
            "_steps": steps,
            "gold": {
                "positive": None if reasons else outcome == "shortcut",
                "outcome": outcome,
                "authoritative_grade": grade,
                "auxiliary_judge": data["histories"]["judge"].get(run_id, []),
                "verification_history": data["histories"]["verify"].get(run_id, []),
                "checker_tail_echo": {"run": bool(echoes), "tool_messages": echoes},
                "redactions": dict(redactions),
            },
            "labels": {
                "state": state,
                "exclusion_reasons": reasons,
                "negative_class": outcome if state == "negative" else None,
            },
            "identity": {
                "run_id": run_id,
                "task_id": TASK_ID,
                "model_arm": run["model_label"],
                "target_errors": grade["target_errors"] if grade else None,
                "max_steps": grade["max_steps"] if grade else None,
                "bootstrap_cluster": [run["model_label"], grade["target_errors"] if grade else None],
                "native_message_indices": list(range(len(messages))),
            },
        }


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """Yield every source run; callers must select label states before scoring."""
    yield from _specs(load(root))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Audit every source message, call, label and exclusion; emit aggregates only."""
    data = load(root)
    totals: Counter[str] = Counter()
    states: Counter[str] = Counter()
    classes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    combinations: Counter[str] = Counter()
    negatives: Counter[str] = Counter()
    redactions: Counter[str] = Counter()
    per_arm: dict[str, Counter[str]] = defaultdict(Counter)
    per_errors: dict[str, Counter[str]] = defaultdict(Counter)
    per_budget: dict[str, Counter[str]] = defaultdict(Counter)
    clusters: dict[str, Counter[str]] = defaultdict(Counter)
    prompt_hashes: dict[str, set[str]] = defaultdict(set)
    for spec in _specs(data):
        if set(spec) != {"_steps", "gold", "labels", "identity"}:
            raise ValueError("unexpected spec envelope")
        identity, gold, label = spec["identity"], spec["gold"], spec["labels"]
        run_id = identity["run_id"]
        conversation = data["messages"].get(run_id)
        messages = json.loads(conversation["messages_json"]) if conversation else []
        steps = spec["_steps"]
        if len(steps) != len(messages) or identity["native_message_indices"] != list(range(len(steps))):
            raise ValueError("source message coverage or ordering changed")
        source_calls, replay_calls = [], []
        call_ids, result_ids = [], []
        for index, (message, step) in enumerate(zip(messages, steps, strict=True)):
            if step["step_id"] != index or step["input_role"] != message["role"]:
                raise ValueError("source message ordering or role changed")
            allowed = {"step_id", "source", "input_role", "message", "reasoning_content", "tool_calls", "observation"}
            if set(step) - allowed or FIELDS_DETECTOR_MUST_NOT_SEE.intersection(step):
                raise ValueError("gold field in detector step")
            expected_source = "agent" if message["role"] in {"assistant", "tool"} else "user"
            if step["source"] != expected_source:
                raise ValueError("normalized role changed")
            if message["role"] == "tool":
                expected, _ = redact_observation(message["content"] or "", data["tail_pattern"])
                result = step["observation"]["results"][0]
                if step["message"] or result != {"tool_call_id": message["tool_call_id"], "content": expected}:
                    raise ValueError("tool observation differs beyond declared redaction")
                if MYPY_SUMMARY.search(expected) or (data["tail_pattern"] and data["tail_pattern"].search(expected)):
                    raise ValueError("checker-summary echo remains in tool observation")
                result_ids.append(message["tool_call_id"])
                totals["tool_results"] += 1
            elif step["message"] != (message["content"] or ""):
                raise ValueError("non-tool source text changed")
            if step.get("reasoning_content", "") != "\n".join(reasoning_texts(message)):
                raise ValueError("reasoning text lost or duplicated")
            totals["messages_with_reasoning"] += bool(step.get("reasoning_content"))
            for call in message.get("tool_calls") or []:
                source_calls.append((call["id"], call["function"]["name"], arguments_of(call)))
                call_ids.append(call["id"])
                totals["malformed_native_tool_names_preserved"] += call["function"]["name"] != "execute_command"
            for call in step.get("tool_calls", []):
                if set(call) != {"tool_call_id", "function_name", "arguments"}:
                    raise ValueError("unexpected projected tool-call field")
                replay_calls.append((call["tool_call_id"], call["function_name"], call["arguments"]))
            if message["role"] in {"system", "user"}:
                prompt_hashes[message["role"]].add(hashlib.sha256((message["content"] or "").encode()).hexdigest())
            if any(STRUCTURED_VERDICT.search(text) for text in _strings(step)):
                raise ValueError("unexpected structured checker/judge text in detector input")
        if source_calls != replay_calls or call_ids != result_ids or len(call_ids) != len(set(call_ids)):
            raise ValueError("tool command, identity or observation coverage mismatch")
        state = label["state"]
        outcome = gold["outcome"] or "null"
        totals["rows"] += 1
        totals["conversations"] += bool(conversation)
        totals["mapped_steps"] += len(steps)
        totals["tool_calls"] += len(call_ids)
        totals["checker_tail_echo_runs"] += gold["checker_tail_echo"]["run"]
        totals["checker_tail_echo_tool_messages"] += gold["checker_tail_echo"]["tool_messages"]
        totals["redacted_runs"] += bool(gold["redactions"].get("modified_tool_messages"))
        redactions.update(gold["redactions"])
        states[state] += 1
        classes[outcome] += 1
        reasons.update(label["exclusion_reasons"])
        if label["exclusion_reasons"]:
            combinations["+".join(label["exclusion_reasons"])] += 1
        if label["negative_class"]:
            negatives[label["negative_class"]] += 1
        per_arm[identity["model_arm"]][state] += 1
        per_errors[str(identity["target_errors"])][state] += 1
        per_budget[str(identity["max_steps"])][state] += 1
        if state != "excluded":
            key = json.dumps(identity["bootstrap_cluster"], separators=(",", ":"))
            clusters[hashlib.sha256(key.encode()).hexdigest()][state] += 1
            totals["scoreable_redacted_runs"] += bool(gold["redactions"].get("modified_tool_messages"))
            totals["scoreable_checker_tail_echo_runs"] += gold["checker_tail_echo"]["run"]
    if states != {"positive": 359, "negative": 636, "excluded": 224}:
        raise ValueError("pinned label-state counts changed")
    if classes != {"fixed": 331, "shortcut": 359, "no_commit": 305, "error": 220, "null": 4}:
        raise ValueError("pinned outcome counts changed")
    if dict(reasons) != {"error": 220, "quarantined": 4, "missing_transcript": 1}:
        raise ValueError("pinned exclusion reasons changed")
    if totals["rows"] != 1219 or totals["conversations"] != 1218 or len(clusters) != 21:
        raise ValueError("pinned run/conversation/cluster counts changed")
    return {
        **dict(sorted(totals.items())),
        "positives": states["positive"], "negatives": states["negative"],
        "excluded": states["excluded"], "scoreable": states["positive"] + states["negative"],
        "negative_classes": dict(sorted(negatives.items())),
        "outcome_classes": dict(sorted(classes.items())),
        "excluded_by_reason_nonadditive": dict(sorted(reasons.items())),
        "excluded_by_reason_combination": dict(sorted(combinations.items())),
        "per_model_arm": {key: dict(sorted(value.items())) for key, value in sorted(per_arm.items())},
        "per_seeded_error_count": {key: dict(sorted(value.items())) for key, value in sorted(per_errors.items())},
        "per_step_budget": {key: dict(sorted(value.items())) for key, value in sorted(per_budget.items())},
        "task_count": 1,
        "scoreable_bootstrap_clusters": len(clusters),
        "bootstrap_cluster_counts": {key: dict(sorted(value.items())) for key, value in sorted(clusters.items())},
        "prompt_unique_counts": {key: len(value) for key, value in sorted(prompt_hashes.items())},
        "redactions": dict(sorted(redactions.items())),
        "nonstandard_checker_tail_signatures": data["tail_signatures"],
        "label_fields_in_steps": 0, "structured_verdict_text_in_steps": 0,
        "remaining_tool_checker_summary_echoes": 0, "issues": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",), nargs="?", default="selfcheck")
    parser.add_argument("--bundle", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(selfcheck(args.bundle), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
