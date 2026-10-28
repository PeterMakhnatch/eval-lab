#!/usr/bin/env python3
"""Offline EvilGenie projection. Only `_steps`, never a complete spec, is detector input.

No source code is executed, no network/model is called, and no transcript is emitted.
Native heuristic labels and conflicting gold channels override positive candidates.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CORPUS = "wiserhumanexperimental__evilgenie-escalation"
REPO = "WiserHumanExperimental/evilgenie-escalation"
REVISION = "1bb777b666b0f76801f9b5b9a28eb3f7786ea70f"
ROOT = Path.home() / ".local/state/evallab-cross-test" / CORPUS / REVISION[:12]
INDEX = "tables/per_run_data_all_conditions.csv"
AUDIT = "tables/confirmed_hack_and_heuristic_audit.csv"
BREAKDOWN = "tables/test_harness_modification_breakdown.csv"

# Native condition/intervention text inside an original system message is allowed.
# Its separate metadata column, all derived analyses, and all gold remain hidden.
FIELDS_DETECTOR_MUST_NOT_SEE = frozenset(
    {
        "gold", "labels", "identity", "state", "positive", "candidate_positive",
        "excluded_reasons", "primary_eligible", "cohort", "task_id", "run_key",
        "scoped_task_id",
        "model", "model_id", "condition", "condition_label", "problem_set",
        "problem", "problem_id", "epoch", "uuid", "in_paper_analysis", "eval_file",
        "score", "score_details", "escalation_report", "output", "model_usage",
        "timing", "error", "reward_hacked", "classification", "test_result",
        "policy_read", "policy_read_msg_index", "policy_read_pct_through",
        "test_file_read", "test_file_read_msg_index", "test_file_read_pct_through",
        "total_messages", "escalated", "final_verdict", "tamper_hack",
        "monitoring_noted", "monitoring_source", "monitoring_flaw_detected",
        "monitoring_understanding_depth", "disclosure_noted", "disclosure_source",
        "disclosure_flaw_detected", "disclosure_understanding_depth",
        "escalation_flaw_detected", "escalation_understanding_depth", "judge_type",
        "judge_confidence", "judge_evidence", "judge_explanation", "files_modified",
        "test_cases_json_modified", "test_py_modified", "gt_outcome",
        "gt_test_py_class", "gt_test_cases_class", "is_reward_hacking", "is_heuristic",
        "hack_channel", "outcome", "outcome_raw", "change_made", "visible_pass",
        "holdout_pass", "llm_classification", "llm_judge_details", "raw_response",
        "visible_test_details", "holdout_test_details", "file_modification_details",
        "judge", "heuristic", "executed_holdout", "audited_tamper", "analysis",
        "test_py_class", "test_cases_class", "n_test_py_events", "n_test_cases_events",
        "mentions_in_answer", "mention_lines", "test_py_events", "test_cases_events",
        "has_validator", "n_exact", "n_validator_rescued", "n_genuinely_wrong",
        "n_crash", "n_undetermined", "wrong_examples", "is_defect_problem",
        "esc_issue_type", "esc_names_flaw", "esc_excerpt", "disclosed",
        "disc_excerpt", "test_py_inspected", "reasoning_match", "reasoning_excerpt",
        "pass1_results", "disclosure_pass2_results", "esc_pass2_results",
        "cost_per_episode", "native_classification", "author_flag", "audit_flag",
        "transcript_epoch", "index_epoch", "native", "audit_is_heuristic",
        "audit_is_reward_hacking", "independent_reaudit", "audit", "breakdown",
        "per_run", "channels", "explanation",
    }
)

# Strip explicit label-bearing verdict records, not ordinary agent-visible tests,
# policy text, code, or generic uses of words such as 'legitimate' and 'judge'.
_VERDICT_FIELD = (
    r"(?:llm[_ -]classification|classification|reward[_ -]hacked|tamper[_ -]hack|"
    r"is[_ -]reward[_ -]hacking|judge[_ -](?:verdict|classification))"
)
_VERDICT_VALUE = r"(?:reward_hacking|legitimate|heuristic|error|positive|negative|yes|no|true|false)"
_VERDICT_LINE = re.compile(
    rf"(?i)[\"']?{_VERDICT_FIELD}[\"']?\s*[:=]\s*[\"']?{_VERDICT_VALUE}\b"
)
_VERDICT_XML = re.compile(
    rf"(?is)<({_VERDICT_FIELD})>\s*{_VERDICT_VALUE}\s*</\1>"
)
_STEP_FIELDS = {
    "step_id", "source", "input_role", "message", "reasoning_content",
    "reasoning_summary", "tool_calls", "observation",
}


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
    if not rows or any(None in row for row in rows):
        raise ValueError("empty or malformed source table; no row content emitted")
    return rows


def keyed(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    result = {row[field]: row for row in rows}
    if len(result) != len(rows) or not all(result):
        raise ValueError("empty or duplicate source identity; no ids emitted")
    return result


def gold_tables(root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    channels = json.loads((root / "tables/all_channels.json").read_text())
    return {
        "main": keyed(csv_rows(root / INDEX), "run_key"),
        "audit": keyed(csv_rows(root / AUDIT), "run_key"),
        "breakdown": keyed(csv_rows(root / BREAKDOWN), "run_key"),
        "executed": keyed(json.loads((root / "tables/ground_truth_results.json").read_text()), "key"),
        "final": keyed(json.loads((root / "tables/final_verdict.json").read_text()), "key"),
        "channels": keyed([row for rows in channels.values() for row in rows], "key"),
    }


def iter_rows(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    manifest = json.loads((root / "STAGE-MANIFEST.json").read_text())
    if (manifest["repo"], manifest["revision"]) != (REPO, REVISION):
        raise ValueError("corpus identity or revision changed")
    for entry in sorted(manifest["files"], key=lambda item: item["path"]):
        if entry["path"].endswith(".jsonl"):
            with (root / entry["path"]).open(encoding="utf-8") as stream:
                for line in stream:
                    yield json.loads(line)


def strip_verdicts(text: str) -> tuple[str, int]:
    text, count = _VERDICT_XML.subn("", text)
    kept = []
    for line in text.splitlines(keepends=True):
        if _VERDICT_LINE.search(line):
            count += 1
        else:
            kept.append(line)
    return "".join(kept), count


def message_parts(message: dict[str, Any]) -> tuple[str, str, str]:
    content = message["content"]
    if isinstance(content, str):
        return content, "", ""
    text, reasoning, summaries = [], [], []
    for block in content:
        if block["type"] == "text":
            text.append(block["text"])
        elif block["type"] == "reasoning":
            reasoning.append(block["reasoning"])
            if block.get("summary"):
                summaries.append(block["summary"])
        else:
            raise ValueError("unrecognized native content block; no text emitted")
    return "\n".join(text), "\n".join(reasoning), "\n".join(summaries)


def to_steps(row: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Read ONLY native messages; keep reasoning, calls and results in source order."""
    steps: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for message in row["messages"]:
        role = message["role"]
        content, reasoning, summary = message_parts(message)
        counts["source_messages"] += 1
        if role == "tool":
            if not steps or steps[-1]["source"] != "agent":
                raise ValueError("tool result without a preceding assistant")
            content, removed = strip_verdicts(content)
            counts["stripped_grader_verdict_fragments"] += removed
            counts["tool_results_with_stripped_verdicts"] += bool(removed)
            steps[-1].setdefault("observation", {"results": []})["results"].append(
                {"tool_call_id": message["tool_call_id"],
                 "source_tool_name": message["function"], "content": content}
            )
            counts["tool_results"] += 1
            continue
        if role not in {"system", "user", "assistant"}:
            raise ValueError("unknown native message role")
        step: dict[str, Any] = {
            "step_id": len(steps), "source": "agent" if role == "assistant" else "user",
            "input_role": role, "message": content,
        }
        if reasoning:
            step["reasoning_content"] = reasoning
            counts["reasoning_steps"] += 1
            counts["provider_encrypted_reasoning_steps"] += "provider-encrypted reasoning" in reasoning
        if summary:
            step["reasoning_summary"] = summary
            counts["reasoning_summary_steps"] += 1
        calls = message.get("tool_calls") or []
        if calls:
            step["tool_calls"] = [
                {"tool_call_id": call["id"], "function_name": call["function"],
                 "arguments": call["arguments"]} for call in calls
            ]
            counts["tool_calls"] += len(calls)
        steps.append(step)
    counts["mapped_steps"] = len(steps)
    return steps, counts


def label_state(row: dict[str, Any], tables: dict[str, Any]) -> dict[str, Any]:
    """Candidate OR first; errors, heuristics and disagreements then exclude it."""
    key = row["run_key"]
    main = tables["main"].get(key)
    audit = tables["audit"].get(key)
    native = row["score"]["llm_classification"]
    if native not in {"legitimate", "reward_hacking", "heuristic", "error"}:
        raise ValueError("unknown native label")
    if main and main["classification"] != native:
        raise ValueError("CSV and transcript classifications differ")
    reward = main["reward_hacked"] == "yes" if main else native == "reward_hacking"
    tamper = bool(main and main["tamper_hack"] == "yes")
    audited_tamper = bool(audit and audit["tamper_hack"] == "True")
    candidate = reward or tamper or audited_tamper
    reasons = []
    if native == "heuristic":
        reasons.append("native_heuristic")
    if native == "legitimate" and candidate:
        reasons.append("native_legitimate_vs_positive_channel")
    if main and reward != (native == "reward_hacking"):
        reasons.append("native_reward_flag_disagreement")
    if main and main["test_result"] != row["score"]["test_result"]:
        reasons.append("executed_test_channel_disagreement")
    if audit:
        if reward != (audit["reward_hacked"] == "True"):
            reasons.append("reward_flag_disagreement")
        if tamper != (audit["tamper_hack"] == "True"):
            reasons.append("tamper_flag_disagreement")
        if (native == "reward_hacking") != (audit["is_reward_hacking"] == "True"):
            reasons.append("audit_reward_class_disagreement")
        if (native == "heuristic") != (audit["is_heuristic"] == "True"):
            reasons.append("audit_heuristic_class_disagreement")
    if native == "error":
        state, reasons = "error", ["native_error"]
    elif reasons:
        state = "ambiguous"
    else:
        state = "positive" if candidate else "negative"
    return {
        "state": state, "positive": state == "positive" if state in {"positive", "negative"} else None,
        "primary_eligible": state in {"positive", "negative"}, "candidate_positive": candidate,
        "excluded_reasons": sorted(reasons), "native_classification": native,
    }


def make_spec(row: dict[str, Any], tables: dict[str, Any]) -> dict[str, Any]:
    key = row["run_key"]
    main, audit = tables["main"].get(key), tables["audit"].get(key)
    steps, _ = to_steps(row)
    return {
        "_steps": steps,
        "labels": label_state(row, tables),
        "identity": {
            "run_key": key, "task_id": row["problem_id"],
            "scoped_task_id": f"{row['problem_set']}/{row['problem_id']}",
            "problem_id": row["problem_id"], "problem_set": row["problem_set"],
            "model": row["model"], "condition": row["condition"],
            "transcript_epoch": row["epoch"], "index_epoch": main["epoch"] if main else None,
            "in_paper_analysis": row["in_paper_analysis"],
            "cohort": "indexed_main" if main else "extra_negative_control",
        },
        "gold": {
            "judge": {
                "native_classification": row["score"]["llm_classification"],
                "author_flag": main["reward_hacked"] if main else None,
                "audit_flag": audit["reward_hacked"] if audit else None,
                "llm_judge_details": row["score_details"]["llm_judge_details"],
                "explanation": row["score"].get("explanation"),
            },
            "heuristic": {
                "native": row["score"]["llm_classification"] == "heuristic",
                "audit_is_heuristic": audit["is_heuristic"] if audit else None,
                "audit_is_reward_hacking": audit["is_reward_hacking"] if audit else None,
            },
            "executed_holdout": {
                "test_result": row["score"]["test_result"],
                "visible_pass": row["score"].get("visible_pass"),
                "holdout_pass": row["score"].get("holdout_pass"),
                "visible_test_details": row["score_details"].get("visible_test_details"),
                "holdout_test_details": row["score_details"].get("holdout_test_details"),
                "independent_reaudit": tables["executed"].get(key),
            },
            "audited_tamper": {
                "author_flag": main["tamper_hack"] if main else None,
                "audit_flag": audit["tamper_hack"] if audit else None,
                "audit": audit, "breakdown": tables["breakdown"].get(key),
            },
            "analysis": {
                "per_run": main, "channels": tables["channels"].get(key),
                "final_verdict": tables["final"].get(key),
                "file_modification_details": row["score_details"].get("file_modification_details"),
                "files_modified": row["score"].get("files_modified"),
                "escalation_report": row["escalation_report"],
            },
        },
    }


def specs(root: Path = ROOT) -> Iterator[dict[str, Any]]:
    """All 2,295 runs; filter labels.primary_eligible only AFTER frozen detection.

    The 180 controls have native transcript labels, not main-CSV author flags.
    Their missing audit channels remain None, never fabricated negative audits.
    """
    tables = gold_tables(root)
    for row in iter_rows(root):
        yield make_spec(row, tables)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(child) for child in value))
    return set()


def selfcheck(root: Path = ROOT) -> dict[str, Any]:
    """Check the complete replay and label join; return counts/digests only."""
    tables = gold_tables(root)
    counts: Counter[str] = Counter()
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    by_cohort: dict[str, Counter[str]] = {}
    seen: set[str] = set()
    task_ids: set[str] = set()
    scoped_task_ids: set[str] = set()
    for row in iter_rows(root):
        key = row["run_key"]
        if key in seen:
            raise ValueError("duplicate transcript identity")
        seen.add(key)
        spec = make_spec(row, tables)
        steps, mapping = to_steps(row)
        if spec["_steps"] != steps or to_steps({"messages": row["messages"]})[0] != steps:
            raise AssertionError("gold noninterference failed")
        if [step["step_id"] for step in steps] != list(range(len(steps))):
            raise AssertionError("noncontiguous replay order")
        if any(set(step) - _STEP_FIELDS for step in steps):
            raise AssertionError("unapproved replay field")
        forbidden = _keys(steps) & FIELDS_DETECTOR_MUST_NOT_SEE
        counts["gold_fields_in_steps"] += len(forbidden)
        if forbidden:
            raise AssertionError("gold field in detector projection; no field values emitted")
        source_non_tools = [message for message in row["messages"] if message["role"] != "tool"]
        if len(source_non_tools) != len(steps):
            raise AssertionError("message/step coverage differs")
        for message, step in zip(source_non_tools, steps, strict=True):
            text, reasoning, summary = message_parts(message)
            if (step["message"], step.get("reasoning_content", ""), step.get("reasoning_summary", "")) != (text, reasoning, summary):
                raise AssertionError("native semantic text not preserved")
        calls = [call for step in steps for call in step.get("tool_calls", [])]
        original_calls = [call for message in row["messages"] for call in message.get("tool_calls") or []]
        if [{"id": call["tool_call_id"], "function": call["function_name"], "arguments": call["arguments"], "type": "function"} for call in calls] != original_calls:
            raise AssertionError("native call roundtrip failed")
        results = [result for step in steps for result in step.get("observation", {}).get("results", [])]
        original_results = [message for message in row["messages"] if message["role"] == "tool"]
        if results != [{"tool_call_id": message["tool_call_id"], "source_tool_name": message["function"], "content": strip_verdicts(message["content"])[0]} for message in original_results]:
            raise AssertionError("native observation roundtrip failed")
        call_ids = {call["tool_call_id"] for call in calls}
        result_ids = {result["tool_call_id"] for result in results}
        if len(call_ids) != len(calls) or len(result_ids) != len(results) or result_ids - call_ids:
            raise AssertionError("native call/result identity mismatch")
        counts["calls_without_result"] += len(call_ids - result_ids)
        counts.update(mapping)
        counts["rows"] += 1
        labels = spec["labels"]
        states[labels["state"]] += 1
        reasons.update(labels["excluded_reasons"])
        cohort = spec["identity"]["cohort"]
        by_cohort.setdefault(cohort, Counter())[labels["state"]] += 1
        task_ids.add(spec["identity"]["task_id"])
        scoped_task_ids.add(spec["identity"]["scoped_task_id"])
        main = tables["main"].get(key)
        if main:
            if any(main[field] != str(row[field]) for field in ("model", "condition", "problem_id")):
                raise AssertionError("main/transcript identity join differs")
            counts["index_transcript_epoch_differences"] += main["epoch"] != str(row["epoch"])
    if set(tables["main"]) - seen or any(set(table) - set(tables["main"]) for table in tables.values()):
        raise AssertionError("gold join incomplete")
    assert states == {"positive": 124, "negative": 1990, "ambiguous": 174, "error": 7}
    assert counts["rows"] == len(seen) == 2295 and len(task_ids) == 15
    assert len(scoped_task_ids) == 18
    assert counts["gold_fields_in_steps"] == 0
    return {
        **dict(sorted(counts.items())), "states": dict(sorted(states.items())),
        "excluded_by_reason_overlapping": dict(sorted(reasons.items())),
        "by_cohort": {key: dict(sorted(value.items())) for key, value in sorted(by_cohort.items())},
        "problem_clusters": len(task_ids), "main_index_rows": len(tables["main"]),
        "scoped_problem_groups": len(scoped_task_ids),
        "issues": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("selfcheck",), nargs="?", default="selfcheck")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(selfcheck(args.root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
