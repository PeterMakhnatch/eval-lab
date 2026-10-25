#!/usr/bin/env python3
"""Allowlist-matched Terminal Wrench views -> CloudTraceDatabase shard + handoff.

Inputs are only the pinned files fetched by wrench_fetch_v3.py (exact native
task_id == preexisting canonical DEV name) plus the pinned metadata index rows.
Native ATIF is validated with evallab.terminal_wrench.validate_trajectory.
Each ATIF step becomes one trace_steps row and is reconstructed losslessly
(content_text + flags_json residual) before any write.

Every one of the 15,905 index views is a separate trace row only when its own
payload was fetched; run_id groups views by run lineage so sanitized/stripped
views are never counted as independent runs. Source classifications remain
provisional scorer-side labels: baseline is not certified clean and no_reward
is not a clean label.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath

import pyarrow.parquet as pq

from evallab.production.cloud_trace_db import CloudTraceDatabase, S3CompatibleCAS
from evallab.terminal_wrench import observation_stats, validate_trajectory

OUT = Path(sys.argv[1])
REV = "6ab64c43a4c6a4d9aa53c2425f37e715257123a3"
INDEX_REV = "622449769ec6ddf33f770794489b8cd21712a0ad"
DATASET_ID = "terminal-wrench"
RAW = OUT / "wrench" / "raw-v3" / REV
TASK_CONTENT_COLUMNS = ["task_id", "description", "instruction", "category", "difficulty", "tags",
                        "expert_time_estimate_min", "hack_description", "hack_difficulty", "easiest_path", "hack_type",
                        "aliases", "source_datasets", "source_urls", "models_present", "models_hacked_by"]
SCORER_TASK_COLUMNS = ("hack_description", "hack_difficulty", "easiest_path", "hack_type", "aliases", "models_hacked_by")
RIGHTS = {"published_dataset_license": "Apache-2.0 (publisher LICENSE at pinned revisions)",
          "underlying_task_licenses": "not_audited", "rights_status": "not_cleared_for_runtime_or_training"}


def canon(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    fetch = json.loads((OUT / "wrench-fetch-dev-subset-v3.json").read_text())
    files = {f["path"]: f for f in fetch["files"]}
    matched = {m["native_task_id"]: m["allowlist_row"] for m in fetch["matched_tasks"]}
    allowlist_sha = fetch["allowlist_sha256"]
    rows = []
    for path in sorted((OUT / "wrench" / "metadata").glob("*.jsonl")):
        rows += [r for r in map(json.loads, path.open()) if r["identity"]["native_task_id"] in matched]

    task_table = {}
    parquet = OUT / "wrench" / "cache" / "tasks.parquet"
    if sha(parquet.read_bytes()) != "b75795be279416296fed35efb16266497220418c3ba21d16ddc6b198f094b081":
        raise SystemExit("task parquet differs from pin")
    for batch in pq.ParquetFile(parquet).iter_batches(columns=TASK_CONTENT_COLUMNS):
        for row in batch.to_pylist():
            if row["task_id"] in matched:  # content columns decoded only for matched tasks
                task_table[row["task_id"]] = row

    cas = S3CompatibleCAS(local_directory=OUT / "cas")
    shard = OUT / "wrench" / "shard-v3" / "cloud-trace-shard-v3.sqlite"
    if shard.exists():
        raise SystemExit("refusing to reuse shard")
    shard.parent.mkdir(parents=True)
    handoff_dir = OUT / "wrench" / "handoff-v3"
    handoff_dir.mkdir(parents=True)
    counts = {"views": 0, "independent_original_runs": 0, "steps": 0, "validator_error_views": 0,
              "missing_payload_views": 0, "multi_call_steps": 0, "reasoning_steps": 0}
    missing, lineages = [], {}
    tasks_out = {}
    with CloudTraceDatabase(shard) as db, (handoff_dir / "traces.jsonl").open("x", encoding="utf-8") as handoff:
        for native_task, allow in sorted(matched.items()):
            table = task_table[native_task]
            task_id = f"{DATASET_ID}/{native_task}@payload-{REV[:12]}"
            packages = sorted(p for p in files if p.startswith(f"tasks/{native_task}/") and "/original_task/" in p)
            instruction_files = {p: files[p]["sha256"] for p in packages if p.endswith("/instruction.md")}
            table_instruction = table["instruction"]
            same = {p: (RAW / p).read_bytes().decode("utf-8") == table_instruction for p in instruction_files}
            task = {
                "task_id": task_id, "dataset_id": DATASET_ID, "split": "held",
                "instruction_text": table_instruction if isinstance(table_instruction, str) else "",
                "original_grader_digest": None, "repaired_grader_digest": None,
                "cas_uri": cas.put_bundle({"task_table_row": table}),
                "metadata_json": {
                    "schema": "lb1.terminal_wrench.task_provenance/3",
                    "source": {"payload_repo": "few-sh/terminal-wrench", "payload_revision": REV,
                               "task_table": "data/tasks/train-00000-of-00001.parquet", "task_table_sha256": "b75795be279416296fed35efb16266497220418c3ba21d16ddc6b198f094b081"},
                    "native_task_id": native_task, "source_datasets": table["source_datasets"],
                    "upstream_source_urls": table["source_urls"], "category": table["category"],
                    "difficulty": table["difficulty"], "tags": table["tags"],
                    "instruction_scope": "task-table instruction column (publisher); per-model original_task/instruction.md files retained in CAS",
                    "instruction_md_equals_table_instruction": same,
                    "original_task_package_files": {p: files[p]["cas_uri"] for p in packages},
                    "scorer_side_task_fields_in_bundle_only": list(SCORER_TASK_COLUMNS),
                    "split_clearance": {"canonical": allow, "allowlist_sha256": allowlist_sha,
                                        "match": "exact native task_id == bare canonical name",
                                        "scope": "split-only; not rights, runtime, retrieval or training clearance"},
                    "effective_runtime_split": "held", "training_admission": "not_established_by_retained_evidence",
                    "rights": RIGHTS,
                    "publisher_package_is_not_canonical_eval_lab_package_digest": True,
                },
            }
            db.upsert_task(task)
            tasks_out[task_id] = task
        for row in sorted(rows, key=lambda r: r["materializer_only"]["trajectory_path"]):
            mat = row["materializer_only"]
            path = mat["trajectory_path"]
            native_task = row["identity"]["native_task_id"]
            if path not in files:
                missing.append(path)
                counts["missing_payload_views"] += 1
                continue
            data = (RAW / path).read_bytes()
            raw_uri = cas.put_bytes(data)
            doc = json.loads(data)
            errors = validate_trajectory(doc)
            counts["validator_error_views"] += bool(errors)
            parts = PurePosixPath(path).parts
            view_dir = "/".join(parts[: parts.index("trial")] if "trial" in parts else parts[:-1])
            view_files = {p: files[p]["cas_uri"] for p in files if p.startswith(view_dir + "/") and p != path}
            variant = row["source"]["variant"]
            independent = variant in ("baseline", "original")
            lineage = row["identity"]["run_lineage_key"]
            lineages.setdefault(lineage, []).append(variant)
            trace_id = f"{DATASET_ID}@{REV[:12]}/{path}"
            steps = []
            for position, step in enumerate(doc["steps"]):
                residual = copy.deepcopy(step)
                message = residual.pop("message", None)
                message_key = "message" in step
                reasoning = step.get("reasoning_content")
                calls = step.get("tool_calls") or []
                results = (step.get("observation") or {}).get("results") or []
                observation_text = "\n".join(r["content"] if isinstance(r.get("content"), str) else canon(r.get("content")).decode() for r in results)
                if len(calls) == 1:
                    tool, command = calls[0].get("function_name"), canon(calls[0].get("arguments")).decode()
                elif calls:
                    tool, command = None, canon(calls).decode()
                else:
                    tool = command = None
                counts["multi_call_steps"] += len(calls) > 1
                counts["reasoning_steps"] += bool(reasoning)
                if isinstance(message, str) or message is None:
                    content, encoding = message, "string" if isinstance(message, str) else "null_or_absent"
                else:
                    content, encoding = canon(message).decode(), "canonical_json"
                steps.append({
                    "trace_id": trace_id, "step_index": position, "role": step.get("source") or "unknown",
                    "tool_name": tool, "command_or_action": command, "content_text": content,
                    "observation_preview": observation_text[:8000] if results else None,
                    "observation_cas_uri": cas.put_bundle(step["observation"]) if step.get("observation") is not None else None,
                    "flags_json": {
                        "schema": "lb1.terminal_wrench.atif_step/3", "native_step_id": step.get("step_id"),
                        "source_role": step.get("source"), "model_name": step.get("model_name"),
                        "timestamp": step.get("timestamp"), "metrics": step.get("metrics"),
                        "tool_calls": calls, "has_reasoning_content": bool(reasoning),
                        "message_key_present": message_key, "content_encoding": encoding,
                        "observation_result_count": len(results),
                        "observation_results_lack_source_call_id": all("source_call_id" not in r for r in results),
                        "native_step_residual": residual,
                        "observation_preview_truncated_at_8000_chars": len(observation_text) > 8000,
                    },
                })
            for built, step in zip(steps, doc["steps"], strict=True):
                rebuilt = copy.deepcopy(built["flags_json"]["native_step_residual"])
                if built["flags_json"]["message_key_present"]:
                    text = built["content_text"]
                    rebuilt["message"] = json.loads(text) if built["flags_json"]["content_encoding"] == "canonical_json" else text
                if canon(rebuilt) != canon(step):
                    raise SystemExit(f"{path}: step {built['step_index']} not lossless")
            score = row["scorer_side"]
            receipt = {
                "status": "provisional",
                "scope": "publisher-released native ATIF view with publisher source classification; no replay, exploit or defect confirmation; split-only canonical DEV name match",
                "source_label": {"classification": score["classification"], "is_hack": score["is_hack_source_label"],
                                 "partition": row["source"]["source_partition"], "variant": variant,
                                 "semantics": "baseline=ordinary-agent condition, not certified clean; no_reward is not a clean label"},
                "label_provenance": f"few-sh/terminal-wrench-trajectories@{INDEX_REV} index row; per-row judgment-vs-reward-fallback provenance not in projected schema",
                "view": {"variant": variant, "original_trajectory_path": mat["original_trajectory_path"],
                         "run_lineage_key": lineage, "is_independent_original_run": independent,
                         "sanitizer_model": mat["sanitizer_model"]},
                "native_payload": {"path": path, "sha256": raw_uri[13:], "bytes": len(data), "schema_version": doc.get("schema_version")},
                "trial_files_cas": view_files,
                "index_record_id": row["record_id"], "index_source": row["source"],
                "source_bookkeeping": {k: score[k] for k in ("reward", "cost_usd", "n_input_tokens", "n_output_tokens", "n_cache_tokens", "episode_count")},
                "validator": {"function": "evallab.terminal_wrench.validate_trajectory", "errors": errors},
                "observation_stats": observation_stats(doc) if not errors else None,
                "source_completeness": "full_native_view_payload",
                "is_model_exploit_confirmation": False, "training_admission": "not_established_by_retained_evidence",
                "canonical_admission": "split-only DEV name match; runtime held", "rights": RIGHTS,
            }
            trace = {"trace_id": trace_id, "run_id": f"{DATASET_ID}/run-lineage/{lineage}",
                     "task_id": f"{DATASET_ID}/{native_task}@payload-{REV[:12]}",
                     "model_id": f"source-reported:{row['identity']['native_model']}", "outcome": "retained_source_rollout",
                     "reward": score["reward"], "is_exploit": False, "is_task_defect_fail": False,
                     "step_count": len(steps), "raw_cas_uri": raw_uri, "verified_receipt_json": receipt}
            db.upsert_trace(trace, steps=steps)
            handoff.write(json.dumps({"schema": "lb1.terminal_wrench.cloud_trace_handoff/3", "task_id": trace["task_id"],
                                      "trace": trace, "steps": steps}, ensure_ascii=False, sort_keys=True) + "\n")
            counts["views"] += 1
            counts["independent_original_runs"] += independent
            counts["steps"] += len(steps)
    with (handoff_dir / "tasks.jsonl").open("x", encoding="utf-8") as stream:
        for task_id in sorted(tasks_out):
            stream.write(json.dumps(tasks_out[task_id], ensure_ascii=False, sort_keys=True) + "\n")
    summary = {"schema": "lb1.terminal_wrench.normalize_receipt/3", "normalizer_sha256": sha(Path(__file__).read_bytes()),
               "matched_tasks": sorted(matched), "index_rows_for_matched_tasks": len(rows), **counts,
               "distinct_run_lineages": len(lineages), "missing_payload_paths": missing,
               "shard": str(shard), "handoff_traces_sha256": sha((handoff_dir / "traces.jsonl").read_bytes()),
               "handoff_tasks_sha256": sha((handoff_dir / "tasks.jsonl").read_bytes())}
    with (OUT / "wrench-normalize-receipt-v3.json").open("x") as stream:
        json.dump(summary, stream, indent=1, sort_keys=True)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
