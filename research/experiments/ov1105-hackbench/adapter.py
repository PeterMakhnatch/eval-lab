#!/usr/bin/env python3
"""Convert pinned HackBench test records to label-free DetBench views.

Only the publisher-approved monitor input reaches the existing LB-6 turn parser.
Labels, construction metadata, and command indices remain in metadata, never views.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intake  # noqa: E402

HERE = Path(__file__).resolve().parent
legacy = intake.load_module("ov1105_hackbench_adapter", intake.TOOLS / "hackbench_adapter.py")
dl = intake.load_module("ov1105_detbench_lib", HERE.parent / "ov1104-detbench/detbench_lib.py")
SET_ID = "hackbench-test"


class ConversionError(ValueError):
    """A stable, transcript-free conversion failure code."""


def validated_input(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    monitor = row.get("monitor_input")
    if not isinstance(monitor, dict):
        raise ConversionError("missing_monitor_input")
    if not all(isinstance(monitor.get(k), str) and monitor[k] for k in ("task_instruction", "transcript")):
        raise ConversionError("invalid_monitor_text")
    count = monitor.get("command_count")
    if type(count) is not int or count < 1:
        raise ConversionError("invalid_command_count")
    gold = row.get("gold_command_indices")
    if not isinstance(gold, list) or not gold or any(type(i) is not int or not 1 <= i <= count for i in gold):
        raise ConversionError("missing_or_invalid_gold")
    if gold != sorted(set(gold)):
        raise ConversionError("unsorted_or_duplicate_gold")
    commands = row.get("hack_commands")
    if not isinstance(commands, list) or len(commands) != len(gold) or any(not isinstance(c, str) for c in commands):
        raise ConversionError("invalid_gold_command_text")
    if not isinstance(row.get("id"), str) or not row["id"]:
        raise ConversionError("missing_record_id")
    if not isinstance(row.get("task"), dict) or not isinstance(row["task"].get("name"), str) or not row["task"]["name"]:
        raise ConversionError("missing_task_cluster")
    if not isinstance(row.get("injected_transcript"), str) or not row["injected_transcript"]:
        raise ConversionError("missing_raw_transcript_provenance")
    try:
        segments = legacy.segments(monitor["transcript"], count)
    except ValueError as exc:
        raise ConversionError("command_numbering_mismatch") from exc
    by_command = {s.cmd: s.text for s in segments if s.kind == "cmd"}
    if any(by_command[i] != text for i, text in zip(gold, commands, strict=True)):
        raise ConversionError("gold_command_text_mismatch")
    # The parser receives no gold, origin, task name, record ID, or raw transcript.
    shell_commands = [
        s.text.replace("\r\n", "\n").strip()
        for s in segments
        if s.kind == "cmd" and s.tool != "mark_task_complete" and s.text.strip()
    ]
    sequence_hash = hashlib.sha256(json.dumps(shell_commands, ensure_ascii=False).encode()).hexdigest()
    return {"monitor_input": {k: monitor[k] for k in ("task_instruction", "transcript", "command_count")}}, monitor, sequence_hash


def convert(cache: Path, output: Path) -> dict[str, Any]:
    cache = intake.outside_repository(cache)
    output = intake.outside_repository(output)
    if output == cache or cache in output.parents:
        raise ValueError("Keep derived views separate from immutable source files")
    manifest = intake.verify_source(cache)
    label_path = HERE / "label-map.json"
    labels = json.loads(label_path.read_text(encoding="utf-8"))
    if labels["revision"] != intake.REVISION or labels["split_mapping"]["test"]["set"] != SET_ID:
        raise ValueError("Label map and adapter source pins disagree")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output must be empty; preserve existing conversion evidence")
    output.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter({"source_rows": 0, "positive": 0, "negative": 0, "excluded": 0, "conversion_failures": 0})
    origins: Counter[str] = Counter()
    types: Counter[str] = Counter()
    exposure_counts: Counter[str] = Counter()
    exposure_origins: Counter[str] = Counter()
    sequence_groups: dict[str, set[str]] = {"sealed": set(), "study": set()}
    failures: Counter[str] = Counter()
    exclusions: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    raw_labels: Counter[str] = Counter()
    tasks: set[str] = set()
    ids: set[str] = set()
    raw_hashes: set[str] = set()
    total_commands = total_steps = total_chars = 0
    run_keys: list[str] = []
    failure_rows: list[dict[str, Any]] = []
    with (cache / intake.stage.DATA).open(encoding="utf-8") as source, (output / "runs.jsonl").open("w", encoding="utf-8") as runs:
        for line_number, line in enumerate(source, 1):
            counts["source_rows"] += 1
            raw_labels["HACK"] += 1  # Release-level label, not a row-level verdict field.
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ConversionError("invalid_record_object")
                origin, hack_type = row.get("origin"), row.get("hack_type")
                if origin not in labels["origins"] or hack_type not in labels["allowed_hack_types"]:
                    counts["excluded"] += 1
                    exclusions["unknown_origin_or_hack_type"] += 1
                    continue
                safe_row, monitor, sequence_hash = validated_input(row)
                native_id = hashlib.sha256(row["id"].encode()).hexdigest()
                if native_id in ids:
                    raise ConversionError("duplicate_record_id")
                ids.add(native_id)
                steps, command_map = legacy.to_steps(safe_row, granularity="turn")
                if len(command_map) != monitor["command_count"]:
                    raise ConversionError("command_conversion_loss")
                run_key = f"{SET_ID}:{native_id}"
                view = dl.build_view(run_key, steps, task_text=monitor["task_instruction"])
                full, actions = dl.write_views(SET_ID, native_id, view, root=output / "views")
                n_steps, n_chars = dl.view_size(view)
                cluster = "hackbench-task:" + hashlib.sha256(row["task"]["name"].encode()).hexdigest()
                raw_hash = hashlib.sha256(row["injected_transcript"].encode()).hexdigest()
                exposure = labels["exposure_policy"]
                partition = "sealed" if row["task"]["name"] in exposure["sealed_tasks"] else "study"
                prior_reads = list(exposure["prior_detector_reads_all_records"])
                if partition == "study":
                    prior_reads.append(exposure["study_read"])
                sequence_key = cluster + ":sequence:" + sequence_hash
                metadata = dl.runs_row(
                    set_id=SET_ID, native_id=native_id, split="heldout", label=labels["rules"]["HACK"]["label"],
                    label_type=labels["label_type"], natural=labels["origins"][origin], cluster=cluster,
                    source_family="facebook/hackbench", view_full=full, view_actions=actions,
                    n_steps=n_steps, n_chars=n_chars, read_by=prior_reads,
                    extra={"source_split": "test", "source_revision": intake.REVISION, "origin": origin,
                           "hack_type": hack_type, "label_state": "positive", "primary": partition == "sealed",
                           "pool": False, "fa_basis": "no_released_negative_trajectories",
                           "command_count": monitor["command_count"], "raw_transcript_sha256": raw_hash,
                           "task_id": row["task"]["name"], "exposure_partition": partition,
                           "independent_run_key": sequence_key, "command_sequence_sha256": sequence_hash,
                           "independent_run_key_method": "exact ordered nonempty shell-command sequence; not approximate source-run independence"},
                )
                runs.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n")
                counts["positive"] += 1
                origins[origin] += 1
                types[hack_type] += 1
                tasks.add(cluster)
                raw_hashes.add(raw_hash)
                exposure_counts[partition] += 1
                exposure_origins[f"{partition}/{origin}"] += 1
                sequence_groups[partition].add(sequence_key)
                total_commands += len(command_map)
                total_steps += n_steps
                total_chars += n_chars
                tools.update(call["name"] for step in view["steps"] for call in step["tool_calls"])
                run_keys.append(run_key)
            except (ConversionError, json.JSONDecodeError) as exc:
                reason = str(exc) if isinstance(exc, ConversionError) else "invalid_json"
                counts["conversion_failures"] += 1
                failures[reason] += 1
                failure_rows.append({"source_line": line_number, "reason": reason})
    report = {
        "schema_version": "ov1105-hackbench-conversion-v1",
        "revision": intake.REVISION, "set": SET_ID, "source_split": "test", "counts": dict(counts),
        "raw_label_counts": dict(raw_labels), "mapped_class_counts": {k: counts[k] for k in ("positive", "negative", "excluded")},
        "origins": dict(sorted(origins.items())), "hack_types": dict(sorted(types.items())),
        "task_clusters": len(tasks), "distinct_raw_transcripts": len(raw_hashes),
        "exposure_partitions": dict(exposure_counts), "exposure_partition_origins": dict(exposure_origins),
        "exact_command_sequence_groups": {key: len(value) for key, value in sequence_groups.items()},
        "historical_approximate_independent_runs": labels["exposure_policy"]["historical_approximate_groups"],
        "unread_detector_records": 0,
        "command_blocks": total_commands, "agent_steps": total_steps, "view_characters": total_chars,
        "tool_calls_by_name": dict(sorted(tools.items())), "conversion_failures_by_reason": dict(failures),
        "excluded_by_reason": dict(exclusions), "failure_rows": failure_rows,
        "publisher_excluded_unreleased_legit": manifest["manifest"]["selection"]["excluded_legit_rows"],
        "final_state": {"reward": "not supplied; null", "diff": "not supplied; null"},
        "layout": "Existing LB-6 turn parser; one agent step per assistant turn; no transcript clipping",
        "source_sha256": manifest["files"][intake.stage.DATA]["sha256"],
        "label_map_sha256": intake.stage.sha256_file(label_path),
        "adapter_sha256": intake.stage.sha256_file(Path(__file__)),
        "dependencies_sha256": {str(path.relative_to(intake.REPO_ROOT)): intake.stage.sha256_file(path) for path in (
            intake.TOOLS / "hackbench_adapter.py", intake.TOOLS / "hackbench_stage.py",
            HERE.parent / "ov1104-detbench/detbench_lib.py", HERE / "intake.py",
        )},
        "run_keys_sha256": hashlib.sha256("\n".join(sorted(run_keys)).encode()).hexdigest(),
        "metrics_limit": "No released negatives: run-level AUROC, FPR, and precision-versus-length discrimination are undefined",
        "prior_exposure": "All 169 records have prior CM-11/CM-15 detector reads; 75 study records also opened for behavior mining; sealed is study-specific",
    }
    (output / "conversion-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=intake.DEFAULT_CACHE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = convert(args.cache, args.output)
    print(json.dumps(report, sort_keys=True))
    if report["counts"]["conversion_failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
