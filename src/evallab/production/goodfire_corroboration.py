"""Promote Goodfire DEV consensus labels only with concrete step evidence.

The source SQLite file is opened read-only.  The destination is an independently
rebuilt DEV projection: every eligible trace is copied, while promoted traces
retain only their corroborating positive-passage action steps (original indices
preserved).  This is a retrieval projection, not replay/container verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import CloudTraceDatabase, protected_task_ids
from evallab.production.trace_harness import HarnessRun
from evallab.production.trace_harness_qa import QaTraceT0Detector

DATASET_ID = "goodfire-reward-hack-data"
VERIFICATION_KIND = "corroborated_consensus_lp_v1_plus_step_evidence"
DEFECT_CLASSES = (
    "test_assertion_tamper", "hidden_test_targeting", "fabricated_verification",
    "upstream_fix_recall", "test_modification", "test_execution_tampering",
    "hardcoding_special_casing", "harness_mechanics_targeting", "harness_substitution",
    "misrepresented_compliance", "submission_gaming", "environment_acquisition",
    "result_object_poisoning", "other",
)
_TASK_FIELDS = ("task_id", "dataset_id", "split", "instruction_text", "original_grader_digest",
                "repaired_grader_digest", "cas_uri", "metadata_json")
_TRACE_FIELDS = ("trace_id", "run_id", "task_id", "model_id", "outcome", "reward", "is_exploit",
                 "is_task_defect_fail", "raw_cas_uri", "verified_receipt_json", "trust_tier",
                 "flagged_passage_steps")
_STEP_FIELDS = ("step_index", "role", "tool_name", "command_or_action", "content_text",
                "observation_preview", "observation_cas_uri", "flags_json")
_EMPTY_RE = re.compile(r"^\s*[\[\{]\s*[\]\}]\s*$")




def _bare_name(task_id: str) -> str:
    return task_id.split("@", 1)[0].rsplit("/", 1)[-1]


def _identity_excluded(task: Mapping[str, Any], protected: frozenset[str]) -> bool:
    task_id = str(task["task_id"])
    aliases = {task_id, _bare_name(task_id)}
    try:
        metadata = json.loads(str(task["metadata_json"]))
    except (TypeError, ValueError):
        return True
    for key in ("family_candidate", "alias_candidates"):
        value = metadata.get(key)
        if isinstance(value, str):
            aliases.add(value)
        elif isinstance(value, list):
            aliases.update(item for item in value if isinstance(item, str))
    return bool(aliases & protected)


def _positive_indices(receipt: Any) -> list[int]:
    if not isinstance(receipt, Mapping):
        return []
    labels = receipt.get("passage_step_labels")
    if not isinstance(labels, Mapping):
        return []
    indices = labels.get("positive_step_indices")
    if not isinstance(indices, list) or not indices:
        return []
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices):
        return []
    return sorted(set(indices))


def _tool_calls(step: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project existing serialized OpenAI-style calls without inventing actions."""
    action = step.get("command_or_action")
    if not isinstance(action, str) or not action.strip():
        return []
    try:
        raw = json.loads(action)
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    calls = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        arguments = item.get("arguments")
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                continue
        if isinstance(arguments, Mapping):
            calls.append({"function_name": name, "arguments": dict(arguments)})
    return calls


def _has_concrete_action(step: Mapping[str, Any]) -> bool:
    action = step.get("command_or_action")
    return (isinstance(step.get("tool_name"), str) and bool(step["tool_name"].strip())
            and isinstance(action, str) and bool(action.strip()) and not _EMPTY_RE.match(action)) or bool(_tool_calls(step))


def _adapt_step(step: Mapping[str, Any]) -> dict[str, Any]:
    calls = _tool_calls(step)
    action = str(step.get("command_or_action") or "")
    if not calls:
        calls = [{"function_name": str(step.get("tool_name") or "bash"), "arguments": {"command": action}}]
    flags = step.get("flags_json")
    if isinstance(flags, str):
        flags = json.loads(flags)
    return {
        "source": "agent" if step.get("role") == "assistant" else step.get("role"),
        "step_id": step.get("step_index"),
        "tool_calls": calls,
        "message": step.get("content_text"),
        "reasoning_content": flags.get("reasoning_content") if isinstance(flags, Mapping) else None,
    }


def _t0_evidence(steps: Sequence[Mapping[str, Any]]) -> dict[int, list[str]]:
    if not steps:
        return {}
    ordered = sorted(steps, key=lambda step: int(step["step_index"]))
    detector = QaTraceT0Detector()
    run = HarnessRun(run_id="goodfire-corroboration", source="goodfire", split="DEV", outcome="unknown",
                     steps=tuple(_adapt_step(step) for step in ordered), actor_inputs_available=True)
    finding = next(iter(detector.detect([run])))
    cited = set(finding["evidence_steps"])
    return {index: ["qa_trace_t0"] for index in cited if index in {step["step_index"] for step in ordered}}


def _l0_evidence(step: Mapping[str, Any]) -> list[str]:
    flags = step.get("flags_json")
    if isinstance(flags, str):
        flags = json.loads(flags)
    if not isinstance(flags, Mapping):
        return []
    hits = []
    for key in ("l0_tripwires", "tripwires", "qa_trace_t0_literal_hits"):
        value = flags.get(key)
        if (isinstance(value, str) and value.strip() and value.strip() not in {"[]", "{}"}
                or isinstance(value, (list, Mapping)) and value):
            hits.append(key)
    return hits


def _citation(receipt: Mapping[str, Any], step: Mapping[str, Any], flags: Any) -> tuple[str, int | None]:
    paths = []
    if isinstance(flags, Mapping):
        paths.append(flags.get("source_file"))
    native = receipt.get("native_line")
    if isinstance(native, Mapping):
        paths.append(native.get("file"))
    for path in paths:
        if isinstance(path, str) and path.strip():
            return path, None
    return f"trace_steps.step_index={step['step_index']}", None


def _issues(trace: Mapping[str, Any], receipt: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]],
            labels: Mapping[int, list[str]]) -> list[dict[str, Any]]:
    source_label = receipt.get("source_label")
    categories = source_label.get("categories") if isinstance(source_label, Mapping) else []
    if not isinstance(categories, list):
        categories = []
    categories = [category for category in DEFECT_CLASSES if category in categories] or ["other"]
    selected = []
    for category in categories:
        step = next((step for step in evidence if category in str(step.get("flags_json"))), evidence[0])
        citation_file, citation_line = _citation(receipt, step, json.loads(step["flags_json"]))
        selected.append({
            "issue_id": "goodfire-corroborated:" + hashlib.sha256(f"{trace['trace_id']}\0{category}".encode()).hexdigest()[:32],
            "task_id": trace["task_id"], "defect_class": category, "direction": "wrong_pass",
            "status": "confirmed", "witness_digest": str(trace.get("raw_cas_uri") or "").removeprefix("cas://sha256/") or None,
            "original_reward": trace.get("reward"), "citation_file": citation_file,
            "citation_line": citation_line,
            "reproducer_script": json.dumps({"trace_id": trace["trace_id"], "step_index": step["step_index"],
                                             "evidence": labels.get(step["step_index"], ["positive_passage_action"])}, sort_keys=True),
            "trust_tier": "tier2_consensus_labeled",
        })
    return selected


def _verify_destination(path: Path, protected: frozenset[str]) -> dict[str, int]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        bad_splits = connection.execute("SELECT count(*) FROM corpus_tasks WHERE upper(split) != 'DEV'").fetchone()[0]
        held_metadata = connection.execute("SELECT count(*) FROM corpus_tasks WHERE upper(json_extract(metadata_json, '$.admission.effective_split')) = 'HELD'").fetchone()[0]
        rows = connection.execute("SELECT task_id FROM corpus_tasks").fetchall()
        protected_count = sum(_bare_name(row[0]) in protected or row[0] in protected for row in rows)
        if bad_splits or held_metadata or protected_count:
            raise ValueError("DEV split firewall failed on derived snapshot")
        return {
            "tasks": connection.execute("SELECT count(*) FROM corpus_tasks").fetchone()[0],
            "traces": connection.execute("SELECT count(*) FROM agent_traces").fetchone()[0],
            "confirmed_issues": connection.execute("SELECT count(*) FROM task_issues WHERE status='confirmed'").fetchone()[0],
            "verified_traces": connection.execute("SELECT count(*) FROM agent_traces WHERE json_extract(verified_receipt_json,'$.status')='verified'").fetchone()[0],
            "held_or_test_leaks": bad_splits + held_metadata + protected_count,
        }
    finally:
        connection.close()


def corroborate_goodfire_dev(source: str | Path, destination: str | Path, *, in_place: bool = False,
                             protected_ids: frozenset[str] | None = None) -> dict[str, Any]:
    """Rebuild a deterministic DEV-only retrieval DB; never mutate ``source`` in place."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    production_source = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
    if in_place and destination_path == production_source.resolve():
        raise ValueError("in-place promotion is forbidden on the production raw source")
    if source_path == destination_path and not in_place:
        raise ValueError("raw source is read-only; pass --in-place only on a disposable copy")
    if in_place and destination_path.exists() and not destination_path.with_suffix(destination_path.suffix + ".corroboration-backup").exists():
        raise ValueError("--in-place requires destination.corroboration-backup to be a preserved copy")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    protected = protected_task_ids() if protected_ids is None else protected_ids
    source_connection = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
    source_connection.row_factory = sqlite3.Row
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    store = None
    try:
        if not in_place:
            with tempfile.NamedTemporaryFile(prefix=destination_path.name + ".", suffix=".tmp", dir=destination_path.parent) as file:
                temporary_path = Path(file.name)
            store = CloudTraceDatabase(temporary_path)
        else:
            store = CloudTraceDatabase(destination_path)
        tasks = [dict(row) for row in source_connection.execute(
            "SELECT * FROM corpus_tasks WHERE dataset_id=? AND split='DEV' ORDER BY task_id", (DATASET_ID,))]
        tasks = [task for task in tasks if not _identity_excluded(task, protected)
                 and str(json.loads(task["metadata_json"]).get("admission", {}).get("effective_split", "")).upper() == "DEV"]
        promoted = 0
        for task in tasks:
            store.upsert_task(task)
            traces = [dict(row) for row in source_connection.execute(
                "SELECT * FROM agent_traces WHERE task_id=? ORDER BY trace_id", (task["task_id"],))]
            for trace in traces:
                trace["is_exploit"] = bool(trace["is_exploit"])
                trace["is_task_defect_fail"] = bool(trace["is_task_defect_fail"])
                receipt = json.loads(trace["verified_receipt_json"]) if trace["verified_receipt_json"] else {}
                positive = _positive_indices(receipt)
                steps = [dict(row) for row in source_connection.execute(
                    "SELECT * FROM trace_steps WHERE trace_id=? ORDER BY step_index", (trace["trace_id"],))]
                selected: list[dict[str, Any]] = []
                evidence_labels: dict[int, list[str]] = {}
                eligible = trace["is_exploit"] == 1 and trace["trust_tier"] == "tier2_consensus_labeled" and bool(positive)
                if eligible:
                    t0 = _t0_evidence(steps)
                    for step in steps:
                        index = step["step_index"]
                        hits = list(t0.get(index, []))
                        hits.extend(_l0_evidence(step))
                        flags = json.loads(step["flags_json"])
                        passages = flags.get("source_passages", []) if isinstance(flags, Mapping) else []
                        for passage in passages:
                            if isinstance(passage, Mapping) and passage.get("consensus") == "positive":
                                hits.extend(f"source_label:{category}" for category in passage.get("carrier_categories", []))
                        if index not in positive:
                            continue
                        if _has_concrete_action(step) and (hits or flags.get("passage_positive") is True):
                            selected.append(step)
                            evidence_labels[index] = sorted(set(hits + ["positive_passage_action"]))
                trace_row = {key: trace[key] for key in _TRACE_FIELDS}
                if selected:
                    promoted += 1
                    receipt = dict(receipt)
                    receipt.update(status="verified", verification_kind=VERIFICATION_KIND,
                                   evidence_projection="positive_passage_concrete_actions_only",
                                   original_step_count=len(steps), projected_step_count=len(selected),
                                   container_replay_verified=False)
                    trace_row["verified_receipt_json"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
                    trace_row["flagged_passage_steps"] = json.dumps([step["step_index"] for step in selected])
                    trace_row["step_count"] = len(selected)
                    for step in selected:
                        flags = json.loads(step["flags_json"])
                        flags["corroboration_evidence"] = evidence_labels[step["step_index"]]
                        step["flags_json"] = json.dumps(flags, sort_keys=True, separators=(",", ":"))
                        step["content_text"] = None
                        step["observation_preview"] = None
                        step["observation_cas_uri"] = None
                    for issue in _issues(trace, receipt, selected, evidence_labels):
                        store.upsert_task_issue(issue)
                    store.upsert_trace(trace_row, steps=[{key: step[key] for key in _STEP_FIELDS} for step in selected])
                else:
                    store.upsert_trace(trace_row)
        store.close()
        store = None
        if temporary_path is not None:
            temporary_path.replace(destination_path)
        counts = _verify_destination(destination_path, protected)
        return {"dataset_id": DATASET_ID, "source": str(source_path), "destination": str(destination_path),
                "verification_kind": VERIFICATION_KIND, "promoted_traces": promoted, **counts}
    finally:
        if store is not None:
            store.close()
        source_connection.close()
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--in-place", action="store_true", help="Only on a disposable copy with .corroboration-backup")
    args = parser.parse_args(argv)
    print(json.dumps(corroborate_goodfire_dev(args.source, args.destination, in_place=args.in_place), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
