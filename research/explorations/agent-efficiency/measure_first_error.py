#!/usr/bin/env python3
"""Measure actual CLI first-error retrieval against summary-then-range reads.

Uses the explicitly selected tasks in a frozen navigation cohort, without new
outcome filters. No model, network, dependency installation, task command execution, or paid calls.
The full/default outline is the accounting and source-coordinate reference;
this is a deterministic readout check, not evidence of model quality parity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(
    root: Path,
    trial: Path,
    runs_root: Path,
    *flags: str,
) -> tuple[dict[str, Any], int]:
    result = subprocess.run(
        [str(root / ".venv/bin/evallab"), "traj", "outline", str(trial), *flags, "--json"],
        cwd=root,
        env={**os.environ, "EVALLAB_RUNS_ROOT": str(runs_root), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"outline CLI failed for {trial}: {result.stderr.strip()}")
    return json.loads(result.stdout), len(result.stdout.encode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--cohort", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if not output.is_relative_to(Path("/tmp/agent-efficiency").resolve()):
        parser.error("private receipts must remain under /tmp/agent-efficiency")
    if output.exists():
        parser.error("output already exists; never overwrite a receipt")
    manifest = json.loads(args.cohort.read_bytes())
    cohort = manifest["cohort"]
    runs_root = Path(cohort["results_root"]).resolve()
    selected = {task["native_source_sha256"] for task in manifest["tasks"]}
    sources = [
        source
        for source in cohort["ranked_candidates"]
        if source["native_source_sha256"] in selected
    ]
    if len(sources) != cohort["selected_sources"] or len(sources) != len(selected) or not sources:
        parser.error("frozen selected cohort is incomplete")
    code = {path: digest(root / path) for path in ("src/evallab/traj.py", "src/evallab/cli.py")}
    rows = []
    for source in sources:
        trial = Path(source["trial_dir"])
        native = Path(source["native_source_path"])
        if digest(native) != source["native_source_sha256"]:
            raise ValueError("frozen native source changed before measurement")
        full, full_bytes = invoke(root, trial, runs_root)
        summary, summary_bytes = invoke(root, trial, runs_root, "--summary")
        fused, fused_bytes = invoke(root, trial, runs_root, "--first-error")
        first = next((step for step in full["steps"] if step["is_error"] is True), None)
        expected = [] if first is None else [first]
        if fused["steps"] != expected:
            raise ValueError("first-error readout changed the selected source record")
        accounting = {key: value for key, value in full.items() if key != "steps"}
        for readout in (summary, fused):
            if {
                key: value
                for key, value in readout.items()
                if key not in ("steps", "step_selection")
            } != accounting:
                raise ValueError("bounded readout changed full-run accounting or provenance")
        prior_bytes, prior_calls = summary_bytes, 1
        if first is not None:
            step_id = first["step_id"]
            ranged, ranged_bytes = invoke(root, trial, runs_root, "--steps", f"{step_id}:{step_id}")
            if ranged["steps"] != expected:
                raise ValueError("explicit range and first-error selection disagree")
            prior_bytes += ranged_bytes
            prior_calls += 1
        if digest(native) != source["native_source_sha256"]:
            raise ValueError("frozen native source changed during measurement")
        rows.append(
            {
                "source_sha256": source["native_source_sha256"],
                "underlying_task": source["task_name"],
                "status": "passed",
                "first_error_logical_step": first["step_id"] if first is not None else None,
                "original_source_step": first["source_step_id"] if first is not None else None,
                "full_json_bytes": full_bytes,
                "summary_then_range_json_bytes": prior_bytes,
                "fused_json_bytes": fused_bytes,
                "summary_then_range_cli_invocations": prior_calls,
                "fused_cli_invocations": 1,
                "full_run_accounting_and_selected_source_record_equal": True,
            }
        )
    if code != {path: digest(root / path) for path in code}:
        raise ValueError("readout implementation changed during measurement")
    fields = (
        "full_json_bytes",
        "summary_then_range_json_bytes",
        "fused_json_bytes",
        "summary_then_range_cli_invocations",
        "fused_cli_invocations",
    )
    receipt = {
        "schema": "agent-efficiency-fused-readout/v1",
        "cohort_manifest_sha256": digest(args.cohort),
        "implementation_sha256": code,
        "original_source_count": len(rows),
        "underlying_benchmark_task_count": len({row["underlying_task"] for row in rows}),
        "model_calls": 0,
        "units": "UTF-8 emitted JSON bytes and CLI invocations, not provider tokens or model tool calls",
        "quality_scope": "Exact selected records, native source identity, and all non-step full-run fields; not causal diagnosis or general model non-inferiority",
        "totals": {field: sum(row[field] for row in rows) for field in fields},
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with output.open("x") as handle:
        json.dump(receipt, handle, indent=2, allow_nan=False)
        handle.write("\n")
    os.chmod(output, 0o600)
    print(
        json.dumps(
            {"output": str(output), "sources": len(rows), "totals": receipt["totals"]}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
