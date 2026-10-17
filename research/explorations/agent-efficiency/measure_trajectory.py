"""Measure readout size and exact fidelity on explicit immutable trial sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from evallab.traj import outline_payload, outline_trajectory


def encode(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, action="append", required=True)
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, trial in enumerate(args.trial):
        started = time.perf_counter()
        outline = outline_trajectory(
            trial,
            repo_root=args.repo_root,
            explicit_runs_root=args.runs_root,
        )
        seconds = time.perf_counter() - started
        full = outline.to_dict()
        summary = outline_payload(outline, summary=True)
        common = {key: value for key, value in summary.items() if key != "step_selection"}
        common["steps"] = full["steps"]
        if common != full:
            raise RuntimeError(f"summary changed full-run facts: {trial}")
        if summary["step_selection"]["omitted_steps"] != len(outline.steps):
            raise RuntimeError(f"unaccounted omissions: {trial}")
        full_bytes, summary_bytes = encode(full), encode(summary)
        (output / f"{index:03d}-full.json").write_bytes(full_bytes)
        (output / f"{index:03d}-summary.json").write_bytes(summary_bytes)
        source_checks = []
        for citation in outline.citations:
            source = Path(citation.path)
            actual = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
            source_checks.append(
                {
                    "path": citation.path,
                    "declared_sha256": citation.sha256,
                    "observed_sha256": actual,
                    "matches": actual == citation.sha256,
                }
            )
        ranges = []
        if outline.steps:
            first, last = outline.steps[0].step_id, outline.steps[-1].step_id
            windows = {(max(first, last - 4), last)}
            if outline.step_to_first_error is not None:
                pointer = outline.step_to_first_error
                windows.add((max(first, pointer - 1), min(last, pointer + 1)))
            for start, end in sorted(windows):
                projected = outline_payload(outline, step_range=(start, end))
                expected = tuple(s for s in full["steps"] if start <= s["step_id"] <= end)
                if projected["steps"] != expected:
                    raise RuntimeError(f"range changed source step data: {trial} {start}:{end}")
                aggregate = {k: v for k, v in projected.items() if k != "step_selection"}
                aggregate["steps"] = full["steps"]
                if aggregate != full:
                    raise RuntimeError(f"range changed full-run facts: {trial} {start}:{end}")
                data = encode(projected)
                (output / f"{index:03d}-steps-{start}-{end}.json").write_bytes(data)
                ranges.append(
                    {
                        "range": [start, end],
                        "bytes": len(data),
                        "returned_step_ids": [s["step_id"] for s in projected["steps"]],
                        "source_coordinates": [
                            {k: s[k] for k in ("source_path", "source_sha256", "source_step_id")}
                            for s in projected["steps"]
                        ],
                        "full_run_facts_exact": True,
                    }
                )
            try:
                outline_payload(outline, step_range=(last + 1, last + 5))
            except ValueError:
                missing_range_rejected = True
            else:
                raise RuntimeError(f"missing range looked successful: {trial}")
        else:
            missing_range_rejected = None
        rows.append(
            {
                "trial": str(trial.resolve()),
                "status": outline.status,
                "unavailable_reason": outline.unavailable_reason,
                "source_sha256": outline.source_sha256,
                "steps": len(outline.steps),
                "full_bytes": len(full_bytes),
                "summary_bytes": len(summary_bytes),
                "summary_reduction_fraction": 1 - len(summary_bytes) / len(full_bytes),
                "full_sha256": hashlib.sha256(full_bytes).hexdigest(),
                "summary_sha256": hashlib.sha256(summary_bytes).hexdigest(),
                "full_run_facts_exact": True,
                "source_checks": source_checks,
                "missing_range_rejected": missing_range_rejected,
                "ranges": ranges,
                "outline_seconds": seconds,
            }
        )
    report = {
        "quality_limit": "Representation fidelity is not model task-success or causal-analysis parity.",
        "rows": rows,
    }
    (output / "measurements.json").write_bytes(encode(report))
    print(
        json.dumps(
            {
                "output": str(output),
                "trials": len(rows),
                "full_bytes": sum(r["full_bytes"] for r in rows),
                "summary_bytes": sum(r["summary_bytes"] for r in rows),
                "full_run_facts_exact": all(r["full_run_facts_exact"] for r in rows),
                "source_digests_match": all(s["matches"] for r in rows for s in r["source_checks"]),
                "unavailable_trials": sum(r["status"] != "featured" for r in rows),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
