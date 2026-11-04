#!/usr/bin/env python3
"""Attempt exact Astra package recovery from archived pre-agent Git baselines.

Never changes the source package or archived workspace. A matching protected test
alone does not establish the original task identity. A failed full-package digest
gate blocks grading; it must not convert 10 abstentions into negative detections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from evallab.registry import task_directory_digest


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def git(workspace: Path, *args: str):
    return subprocess.check_output(["git", *args], cwd=workspace)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--grades", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    task = "astra-strict-schema-friction"
    workspaces = sorted(args.work.glob(task + "__*/testbed"))
    if len(workspaces) != 11:
        raise ValueError("Expected the 11 archived Astra workspaces")
    baseline = []
    identities = set()
    base_files = None
    for workspace in workspaces:
        commit = git(workspace, "rev-parse", "HEAD").decode().strip()
        paths = git(workspace, "ls-tree", "-r", "--name-only", "HEAD").decode().splitlines()
        files = {path: git(workspace, "show", "HEAD:" + path) for path in paths}
        if base_files is None:
            base_files = files
        elif files != base_files:
            raise ValueError("Pre-agent Git baselines differ between runs")
        grade_path = args.grades / workspace.parent.name / "grade.json"
        grade = json.loads(grade_path.read_text())
        identities.add(grade["package_digest"])
        baseline.append(
            {
                "run_id": workspace.parent.name,
                "commit": commit,
                "original_grade_path": str(grade_path),
                "original_grade_sha256": sha(grade_path.read_bytes()),
                "original_package_digest": grade["package_digest"],
            }
        )
    if len(identities) != 1:
        raise ValueError("Original package identities differ between runs")
    expected = identities.pop()
    staged = args.out / "restored-package"
    shutil.copytree(args.package, staged)
    changes = []
    for rel, original in base_files.items():
        for parent in ("environment/app", "tests/pristine"):
            target = staged / parent / rel
            if target.is_file() and target.read_bytes() != original:
                before = target.read_bytes()
                changes.append(
                    {
                        "path": target.relative_to(staged).as_posix(),
                        "current_sha256": sha(before),
                        "baseline_sha256": sha(original),
                    }
                )
                target.write_bytes(original)
    current = task_directory_digest(args.package)
    recovered = task_directory_digest(staged)
    matches = recovered == expected
    result = {
        "status": "identity_recovered" if matches else "blocked_original_package_identity",
        "expected_digest": expected,
        "current_digest": current,
        "reconstructed_digest": recovered,
        "reconstructed_package": str(staged),
        "identity_matches": matches,
        "baseline_consensus_runs": len(baseline),
        "baseline_sources": baseline,
        "restored_files": changes,
        "script_sha256": sha(Path(__file__).read_bytes()),
        "new_container_grades": 0,
        "additional_admitted_rows": 0,
        "reason": (
            "Exact package recovered; fresh controlled grades remain required"
            if matches
            else "Restoring all archived baseline app/test bytes does not recover the recorded full package digest; no grade is admissible"
        ),
    }
    (args.out / "recovery.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
