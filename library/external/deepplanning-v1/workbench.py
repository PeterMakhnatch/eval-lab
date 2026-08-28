#!/usr/bin/env python3
"""Run Linux-only task-workbench controls for the DeepPlanning canary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from evallab.task_workbench import (
    CandidateSource,
    HarborControlBackend,
    check_candidate,
    inspect_candidate,
    run_controls,
    write_packet,
)

SOURCE = CandidateSource(
    source_uri="https://huggingface.co/datasets/Qwen/DeepPlanning",
    source_ref="213876cce679f993a476d01042e13d111c0e3648",
    license="Apache-2.0",
    provenance_zone="01-external",
)
TASK_PATH = Path(
    "library/tasks/experimental/deepplanning-v1/travel-lisbon-002"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    task_path = repo_root / TASK_PATH
    inspection = inspect_candidate(
        repo_root=repo_root,
        task_path=task_path,
        source=SOURCE,
    )
    controls = run_controls(
        inspection=inspection,
        repo_root=repo_root,
        task_path=task_path,
        backend=HarborControlBackend(),
    )
    report = check_candidate(inspection, controls, repo_root=repo_root)
    candidate_path, certification_path = write_packet(
        repo_root=repo_root,
        report=report,
    )
    print(
        json.dumps(
            {
                "candidate": candidate_path.relative_to(repo_root).as_posix(),
                "certification": certification_path.relative_to(repo_root).as_posix(),
                "disposition": report.disposition,
            },
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
