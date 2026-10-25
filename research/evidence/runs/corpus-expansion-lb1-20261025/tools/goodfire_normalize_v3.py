#!/usr/bin/env python3
"""Pinned Goodfire judge runs -> exact CAS rows + CloudTraceDatabase handoff/shard.

Only runs whose every native task name passed the pre-body alias gate are JSON
decoded (``--decode``). Gate-held runs (``--raw-only``) are split into exact
per-line CAS objects by byte boundaries without any JSON decoding.

Per decoded native row this writes:
* the exact traces.jsonl line bytes (terminator included) as ``raw_cas_uri``;
* the exact judge_rollouts.jsonl line and contiguous judge_passages.jsonl byte
  range for that row as separate CAS objects;
* one ``corpus_tasks`` row per distinct native task bundle (split ``held``);
* one ``agent_traces`` row with a provisional source-label receipt (no exploit,
  no task-defect, no admission claims);
* one ``trace_steps`` row per native node, lossless: the node is reconstructed
  from ``content_text`` + ``flags_json`` and compared before anything is written.

Whole-file SHA-256 values are recomputed from the streamed bytes and must equal
the tree LFS oid / git blob sha1 recorded by the fetch receipt.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import CloudTraceDatabase, S3CompatibleCAS

REPO = "Goodfire/reward-hack-data"
REVISION = "bd8fb473568b8ef1d12fc2325a5c8af939c15ad9"
DATASET_ID = "goodfire-reward-hack-data"
SCHEMA = "lb1.goodfire.cloud_trace_handoff/1"
STEP_SCHEMA = "lb1.goodfire.native_node_step/1"
PROTOCOL = {"swebench_verified": "consensus_lp_v1", "impossiblebench": "consensus_v2", "deepswe": "consensus_v2"}
FAMILY = {
    "swebench-verified-glm-5.2-nohack": "swebench_verified",
    "swebench-verified-kimi-k3-nohack": "swebench_verified",
    "swebench-verified-qwen3.8-2.4t-a95b-fp8-nohack": "swebench_verified",
    "deepswe-glm-5.2-fp8": "deepswe", "deepswe-kimi-k3": "deepswe",
    "deepswe-qwen3.8-2.4t-a95b-fp8": "deepswe",
    "impossiblebench-glm-5.2-fp8": "impossiblebench", "impossiblebench-kimi-k3": "impossiblebench",
    "impossiblebench-qwen3.8-2.4t-a95b-fp8": "impossiblebench",
}
PASSAGE_TEXT_FIELDS = {"carrier_quotes"}
FLAG_TEXT_FIELDS = {"quote", "why", "repair"}
RIGHTS = {"declared_license": None, "rights_status": "HOLD_unresolved_no_license_declared",
          "redistribution": "not_permitted_pending_decision", "underlying_benchmark_and_model_terms": "not_audited"}
ADMISSION = {
    "effective_split": "held", "source_split": None, "training_admission": "not_established_by_retained_evidence",
    "canonical_alias_clearance": "worker_structural_pre_body_gate_only; CorpusRegistry.sealed_identity not run by worker",
    "actor_exclusion": "not_enforced_by_CloudTraceDatabase; keep staged until retrieval filters held/rights rows",
}


def canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lines_with_offsets(path: Path):
    offset = 0
    with path.open("rb") as stream:
        for line in stream:
            yield offset, line
            offset += len(line)


class FileHash:
    def __init__(self, size: int):
        self.sha256 = hashlib.sha256()
        self.sha1 = hashlib.sha1(f"blob {size}\0".encode())
        self.size = 0

    def update(self, data: bytes) -> None:
        self.sha256.update(data)
        self.sha1.update(data)
        self.size += len(data)


def expected(fetch: dict[str, dict], rel: str) -> dict:
    entry = fetch[rel]
    return {"size": entry["size"], "sha256": entry["sha256"], "git_blob_sha1": entry["git_blob_sha1"]}


def check_file(fh: FileHash, exp: dict, rel: str) -> None:
    if (fh.size, fh.sha256.hexdigest(), fh.sha1.hexdigest()) != (exp["size"], exp["sha256"], exp["git_blob_sha1"]):
        raise RuntimeError(f"{rel}: streamed bytes differ from verified fetch receipt")


def raw_only(run: str, raw: Path, cas: S3CompatibleCAS, fetch: dict, out: Path) -> dict:
    """Byte-boundary split of a gate-held traces file; no JSON decoding."""
    rel = f"{run}/traces.jsonl"
    exp = expected(fetch, rel)
    fh = FileHash(exp["size"])
    index_path = out / "goodfire" / "raw-only-line-index-v3" / f"{run}.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with index_path.open("x", encoding="utf-8") as index:
        for offset, line in lines_with_offsets(raw / rel):
            fh.update(line)
            uri = cas.put_bytes(line)
            index.write(json.dumps({"run": run, "file": rel, "line_zero_based": count, "byte_offset": offset,
                                    "byte_length": len(line), "line_sha256_including_terminator": uri[13:],
                                    "cas_uri": uri, "json_decoded": False}) + "\n")
            count += 1
    check_file(fh, exp, rel)
    return {"run": run, "mode": "raw_only_gate_held", "native_lines": count, "file_sha256": exp["sha256"],
            "file_bytes": exp["size"], "line_index": str(index_path), "json_decoded": False}


def model_of(agent: dict) -> str:
    model = agent.get("model") or (agent.get("config") or {}).get("model")
    return f"source-reported:{model}" if isinstance(model, str) and model else "source-reported:unknown"


def native_reward(rewards: Any) -> float | None:
    if isinstance(rewards, dict) and len(rewards) == 1:
        (value,) = rewards.values()
        score = value.get("score") if isinstance(value, dict) else None
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            return float(score)
    return None


def final_path(nodes: list[dict]) -> set[int]:
    path: set[int] = set()
    index = len(nodes) - 1
    while index is not None and 0 <= index < len(nodes) and index not in path:
        path.add(index)
        parent = nodes[index].get("parent")
        index = parent if isinstance(parent, int) and not isinstance(parent, bool) else None
    return path


def label_summary(label: dict, family: str) -> dict:
    keep = ("row_index", "judged", "verdict", "verdict_enacted", "categories", "n_passages",
            "n_passages_positive", "n_passages_ambiguous", "yes_without_unanimous_segment", "reward",
            "shortcut_signal", "split", "judge_instance_id")
    summary = {key: label[key] for key in keep if key in label}
    summary["absent_fields"] = sorted(key for key in keep if key not in label)
    summary["source_protocol"] = PROTOCOL[family]
    summary["flag_count"] = len(label["flags"]) if isinstance(label.get("flags"), list) else None
    summary["rationale_present"] = isinstance(label.get("rationale"), str)
    return summary


def build_steps(trace_id: str, nodes: list[dict], flags: list[dict], passages: list[dict]) -> list[dict]:
    on_path = final_path(nodes)
    children: dict[int, int] = {}
    for node in nodes:
        parent = node.get("parent")
        if isinstance(parent, int):
            children[parent] = children.get(parent, 0) + 1
    flags_by_node: dict[int, list] = {}
    for flag in flags:
        flags_by_node.setdefault(flag.get("node_index"), []).append(
            {k: v for k, v in flag.items() if k not in FLAG_TEXT_FIELDS} | {"repair_present": flag.get("repair") is not None})
    passages_by_node: dict[int, list] = {}
    for passage in passages:
        passages_by_node.setdefault(passage.get("node_index"), []).append(
            {k: v for k, v in passage.items() if k not in PASSAGE_TEXT_FIELDS})
    steps = []
    for index, node in enumerate(nodes):
        message = node.get("message") if isinstance(node.get("message"), dict) else {}
        residual = copy.deepcopy(node)
        residual_message = residual.get("message") if isinstance(residual.get("message"), dict) else None
        content_key = residual_message is not None and "content" in residual_message
        reasoning_key = residual_message is not None and "reasoning_content" in residual_message
        content = residual_message.pop("content") if content_key else None
        reasoning = residual_message.pop("reasoning_content") if reasoning_key else None
        if content is None:
            content_text, encoding = None, "null_or_absent"
        elif isinstance(content, str):
            content_text, encoding = content, "string"
        else:
            content_text, encoding = canon(content).decode(), "canonical_json"
        calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        role = message.get("role") if isinstance(message.get("role"), str) and message.get("role") else "unknown"
        tool_name = command = None
        if calls:
            if len(calls) == 1:
                tool_name = calls[0].get("name")
                arguments = calls[0].get("arguments")
                command = arguments if isinstance(arguments, str) else canon(arguments).decode()
            else:
                command = canon([{k: c.get(k) for k in ("id", "name", "arguments")} for c in calls]).decode()
        elif role == "tool":
            tool_name = message.get("name")
        steps.append({
            "trace_id": trace_id, "step_index": index, "role": role,
            "tool_name": tool_name if isinstance(tool_name, str) and tool_name else None,
            "command_or_action": command, "content_text": content_text,
            "observation_preview": None, "observation_cas_uri": None,
            "flags_json": {
                "schema": STEP_SCHEMA, "native_node_index": index,
                "native_parent": node.get("parent"), "native_parent_key_present": "parent" in node,
                "on_final_parent_path": index in on_path, "native_child_count": children.get(index, 0),
                "sampled": node.get("sampled"), "source_role": message.get("role"),
                "has_reasoning_content": isinstance(reasoning, str) and bool(reasoning),
                "reasoning_content": reasoning, "reasoning_content_key_present": reasoning_key,
                "content_encoding": encoding, "content_key_present": content_key,
                "tool_calls": calls, "tool_call_id": message.get("tool_call_id"),
                "native_node_residual": residual,
                "source_judge_flags": flags_by_node.get(index, []),
                "source_passages": passages_by_node.get(index, []),
                "source_annotation_scope": "publisher three-vote LLM judge annotations; provisional; quotes/rationale only in label CAS",
            },
        })
    return steps


def reconstruct(step: dict) -> dict:
    flags = step["flags_json"]
    node = copy.deepcopy(flags["native_node_residual"])
    message = node.get("message")
    if isinstance(message, dict):
        if flags["content_key_present"]:
            text = step["content_text"]
            message["content"] = (json.loads(text) if flags["content_encoding"] == "canonical_json" else text)
        if flags["reasoning_content_key_present"]:
            message["reasoning_content"] = flags["reasoning_content"]
    return node


def decode_run(run: str, raw: Path, cas: S3CompatibleCAS, fetch: dict, out: Path, db: CloudTraceDatabase,
               tasks: dict[str, dict], handoff_dir: Path) -> dict:
    family = FAMILY[run]
    trace_rel, label_rel, passage_rel = f"{run}/traces.jsonl", f"{run}/labels/judge_rollouts.jsonl", f"{run}/labels/judge_passages.jsonl"
    labels, label_lines = [], []
    label_hash = FileHash(fetch[label_rel]["size"])
    for offset, line in lines_with_offsets(raw / label_rel):
        label_hash.update(line)
        row = json.loads(line)
        if row["row_index"] != len(labels):
            raise RuntimeError(f"{label_rel}: row_index {row['row_index']} at position {len(labels)}")
        labels.append(row)
        label_lines.append((offset, line))
    check_file(label_hash, expected(fetch, label_rel), label_rel)

    passage_hash = FileHash(fetch[passage_rel]["size"])
    passage_iter = lines_with_offsets(raw / passage_rel)
    pending: tuple[int, bytes, dict] | None = None
    passage_rows_total = 0

    def passages_for(row_index: int) -> tuple[list[dict], int | None, bytes]:
        nonlocal pending, passage_rows_total
        rows, start, chunk = [], None, bytearray()
        while True:
            if pending is None:
                try:
                    offset, line = next(passage_iter)
                except StopIteration:
                    break
                passage_hash.update(line)
                pending = (offset, line, json.loads(line))
            offset, line, row = pending
            if row["row_index"] < row_index:
                raise RuntimeError(f"{passage_rel}: unordered or unmatched row_index {row['row_index']}")
            if row["row_index"] > row_index:
                break
            start = offset if start is None else start
            chunk += line
            rows.append(row)
            passage_rows_total += 1
            pending = None
        return rows, start, bytes(chunk)

    trace_hash = FileHash(fetch[trace_rel]["size"])
    counts = {"native_rows": 0, "steps": 0, "judged": 0, "unjudged": 0, "branching_rows": 0,
              "nodes_off_final_path": 0, "tasks_new": 0, "reasoning_steps": 0, "tool_call_steps": 0}
    handoff_path = handoff_dir / f"traces-{run}.jsonl"
    with handoff_path.open("x", encoding="utf-8") as handoff:
        for position, (offset, line) in enumerate(lines_with_offsets(raw / trace_rel)):
            trace_hash.update(line)
            if position >= len(labels):
                raise RuntimeError(f"{trace_rel}: native row {position} has no label row")
            label = labels[position]
            row = json.loads(line)
            if not isinstance(row.get("traces"), list) or len(row["traces"]) != 1:
                raise RuntimeError(f"{trace_rel}:{position}: expected exactly one native trace")
            native = row["traces"][0]
            task = native["task"]
            name = task["data"]["name"]
            if (label["row_id"], label["trace_id"], label["task"]) != (row["id"], native["id"], name):
                raise RuntimeError(f"{trace_rel}:{position}: native ids do not join label row")
            nodes = native["nodes"]
            passages, passage_start, passage_bytes = passages_for(position)
            for passage in passages:
                if passage["trace_id"] != native["id"] or not 0 <= passage["node_index"] < len(nodes):
                    raise RuntimeError(f"{passage_rel}: passage does not address row {position}")
            if label["judged"] and label.get("n_passages") is not None and label["n_passages"] != len(passages):
                raise RuntimeError(f"{passage_rel}: row {position} passage count differs from label")
            flags = label.get("flags") if isinstance(label.get("flags"), list) else []
            for flag in flags:
                if not 0 <= flag.get("node_index", -1) < len(nodes):
                    raise RuntimeError(f"{label_rel}: flag node outside row {position}")

            raw_uri = cas.put_bytes(line)
            label_uri = cas.put_bytes(label_lines[position][1])
            passage_uri = cas.put_bytes(passage_bytes) if passage_bytes else None
            bundle = canon(task)
            bundle_uri = cas.put_bytes(bundle)
            env_id = (row.get("env") or {}).get("id") if isinstance(row.get("env"), dict) else row.get("env")
            task_id = f"{DATASET_ID}/{env_id}/{task['key']}@task-bundle-sha256-{bundle_uri[13:29]}"
            prompt = task["data"].get("prompt")
            if task_id not in tasks:
                tasks[task_id] = {
                    "task_id": task_id, "dataset_id": DATASET_ID, "split": "held",
                    "instruction_text": prompt if isinstance(prompt, str) else "",
                    "original_grader_digest": None, "repaired_grader_digest": None,
                    "cas_uri": bundle_uri,
                    "metadata_json": {
                        "schema": "lb1.goodfire.task_provenance/1",
                        "source": {"repo": REPO, "revision": REVISION, "first_seen_run": run, "family": family},
                        "native_identity": {"env_id": env_id, "task_type": task.get("type"), "task_key": task.get("key"),
                                            "publisher_task_hash": task.get("hash"), "task_name": name,
                                            "task_idx": task["data"].get("idx"),
                                            "source_variant": task["data"].get("split")},
                        "publisher_task_hash_is_not_eval_lab_package_digest": True,
                        "task_bundle": {"cas_uri": bundle_uri, "encoding": "canonical JSON of traces[0].task (derived serialization of exact native row; exact bytes in each trace raw_cas_uri)",
                                        "contains_scorer_side_fields": sorted(k for k in task["data"] if k not in ("prompt", "name", "idx"))},
                        "instruction_scope": ("exact traces[0].task.data.prompt string as released (agent-facing prompt, includes any publisher-appended section); "
                                              "not a canonical Eval Lab task package" if isinstance(prompt, str) else "no prompt field in native task; instruction_text left empty"),
                        "alias_candidates": sorted({name, str(task.get("key"))}),
                        "family_candidate": name, "runs": [run],
                        "rights": RIGHTS, "admission": ADMISSION,
                    },
                }
                counts["tasks_new"] += 1
            elif run not in tasks[task_id]["metadata_json"]["runs"]:
                tasks[task_id]["metadata_json"]["runs"].append(run)

            trace_id = f"{DATASET_ID}/{run}/{native['id']}"
            steps = build_steps(trace_id, nodes, flags, passages)
            for step, node in zip(steps, nodes, strict=True):
                if canon(reconstruct(step)) != canon(node):
                    raise RuntimeError(f"{trace_rel}:{position}: step {step['step_index']} not lossless")
            off_path = sum(not s["flags_json"]["on_final_parent_path"] for s in steps)
            receipt = {
                "status": "provisional",
                "scope": ("publisher-released native rollout with publisher three-vote LLM-judge source labels; "
                          "no replay, no exploit or task-defect confirmation, no admission"),
                "source_label": label_summary(label, family),
                "label_provenance": f"{REPO}@{REVISION}/{label_rel} row {position}; {PROTOCOL[family]}; three gpt-5.6-sol votes (publisher-reported)",
                "label_row_cas_uri": label_uri,
                "passages": {"cas_uri": passage_uri, "rows": len(passages), "file": passage_rel,
                             "byte_offset": passage_start, "byte_length": len(passage_bytes)},
                "native_line": {"file": trace_rel, "line_zero_based": position, "byte_offset": offset,
                                "byte_length": len(line), "sha256_including_terminator": raw_uri[13:],
                                "file_sha256": fetch[trace_rel]["sha256"]},
                "native_ids": {"outer_row_id": row["id"], "trace_id": native["id"], "run": native.get("run"),
                               "task_name": name, "task_key": task.get("key")},
                "native_status": {"outer_ok": row.get("ok"), "outer_error_count": len(row.get("errors") or []),
                                  "trace_ok": native.get("ok"), "is_completed": native.get("is_completed"),
                                  "stop_condition": native.get("stop_condition"),
                                  "trace_error_count": len(native.get("errors") or [])},
                "native_metrics": native.get("metrics"), "native_rewards": native.get("rewards"),
                "reward_column_rule": "single native rewards entry score, else null (ImpossibleBench shortcut_signal is not a reward)",
                "branching": {"nodes": len(nodes), "nodes_off_final_parent_path": off_path},
                "source_completeness": "full_native_row",
                "is_model_exploit_confirmation": False,
                "training_admission": ADMISSION["training_admission"], "canonical_admission": "held",
                "rights": RIGHTS, "admission": ADMISSION,
            }
            trace = {"trace_id": trace_id, "run_id": f"{DATASET_ID}@{REVISION}/{run}", "task_id": task_id,
                     "model_id": model_of(native.get("agent") or {}), "outcome": "retained_source_rollout",
                     "reward": native_reward(native.get("rewards")), "is_exploit": False,
                     "is_task_defect_fail": False, "step_count": len(steps), "raw_cas_uri": raw_uri,
                     "verified_receipt_json": receipt}
            db.upsert_task(tasks[task_id])
            db.upsert_trace(trace, steps=steps)
            handoff.write(json.dumps({"schema": SCHEMA, "task_id": task_id, "trace": trace, "steps": steps},
                                     ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            counts["native_rows"] += 1
            counts["steps"] += len(steps)
            counts["judged" if label["judged"] else "unjudged"] += 1
            counts["branching_rows"] += bool(off_path)
            counts["nodes_off_final_path"] += off_path
            counts["reasoning_steps"] += sum(s["flags_json"]["has_reasoning_content"] for s in steps)
            counts["tool_call_steps"] += sum(bool(s["flags_json"]["tool_calls"]) for s in steps)
    if counts["native_rows"] != len(labels):
        raise RuntimeError(f"{run}: {counts['native_rows']} native rows for {len(labels)} label rows")
    if pending is not None or next(passage_iter, None) is not None:
        raise RuntimeError(f"{passage_rel}: passages remain after the last native row")
    check_file(trace_hash, expected(fetch, trace_rel), trace_rel)
    check_file(passage_hash, expected(fetch, passage_rel), passage_rel)
    return {"run": run, "mode": "decoded", "family": family, "label_rows": len(labels),
            "passage_rows": passage_rows_total, **counts, "handoff": str(handoff_path),
            "handoff_sha256": sha(handoff_path.read_bytes()),
            "files_sha256": {rel: fetch[rel]["sha256"] for rel in (trace_rel, label_rel, passage_rel)}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fetch-receipt", type=Path, action="append", required=True)
    parser.add_argument("--decode", action="append", default=[])
    parser.add_argument("--raw-only", action="append", default=[])
    parser.add_argument("--shard", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    fetch = {}
    for path in args.fetch_receipt:
        for entry in json.loads(path.read_text())["files"]:
            fetch[entry["path"]] = entry
    raw = args.out / "goodfire" / "raw" / REVISION
    cas = S3CompatibleCAS(local_directory=args.out / "cas")
    handoff_dir = args.out / "goodfire" / "handoff-v3"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    if args.shard.exists():
        raise SystemExit(f"refusing to reuse existing shard {args.shard}")
    args.shard.parent.mkdir(parents=True, exist_ok=True)
    tasks: dict[str, dict] = {}
    results = []
    with CloudTraceDatabase(args.shard) as db:
        for run in args.decode:
            results.append(decode_run(run, raw, cas, fetch, args.out, db, tasks, handoff_dir))
            print(json.dumps({k: v for k, v in results[-1].items() if k != "files_sha256"}), file=sys.stderr)
        for task in tasks.values():  # final run lists
            db.upsert_task(task)
    for run in args.raw_only:
        results.append(raw_only(run, raw, cas, fetch, args.out))
        print(json.dumps(results[-1]), file=sys.stderr)
    tasks_path = handoff_dir / "tasks.jsonl"
    with tasks_path.open("x", encoding="utf-8") as stream:
        for task_id in sorted(tasks):
            stream.write(json.dumps(tasks[task_id], ensure_ascii=False, sort_keys=True) + "\n")
    summary = {"schema": "lb1.goodfire.normalize_receipt/1", "repo": REPO, "revision": REVISION,
               "normalizer_sha256": sha(Path(__file__).read_bytes()), "shard": str(args.shard),
               "tasks": len(tasks), "tasks_handoff": str(tasks_path), "tasks_handoff_sha256": sha(tasks_path.read_bytes()),
               "runs": results}
    with args.receipt.open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
