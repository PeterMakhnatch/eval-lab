"""Vendor the exact 567 reference-bearing inputs and recover two source patches.

The historical oracle-pass label defines membership, not current-v6 qualification.
No test hunks are added or modified. Native controls qualify the resulting bytes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any

INDEX = Path("research/experiments/mimo-reference-fixes/index.csv")
RELEASE = Path("research/experiments/mimo-v6-full-fleet")
RECOVERED_INPUTS = {
    "format-code-task-000081": "6f7b9f6490f0ff6f016290d53fe3e6fa32609319d29a5b39d1c2dfb6b2f69d83",
    "format-code-task-000838": "f285e604f57dfefb4b2b5f32ac495b2519ba837c1698095547acb0007d856bd7",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recover_patch(task_id: str, patch: bytes) -> tuple[bytes, str]:
    if task_id not in RECOVERED_INPUTS:
        return patch, "unchanged indexed reference"
    if sha256(patch) != RECOVERED_INPUTS[task_id]:
        raise ValueError(f"{task_id}: recovery input no longer matches retained evidence")
    if task_id == "format-code-task-000081":
        blocks = re.split(rb"(?=^diff --git )", patch, flags=re.MULTILINE)
        selected = b"".join(
            block for block in blocks
            if block.startswith(b"diff --git a/xdoctest/runner.py b/xdoctest/runner.py\n")
        )
        if sha256(selected) != "1007e5113f13068aeebc2ee1ce4416618ba98e678574319ad95b5ff595b7dd40":
            raise ValueError("000081: source-only recovery differs from retained preimage diagnosis")
        return selected, (
            "Retain every xdoctest/runner.py hunk; omit deleted .coveragerc and other "
            "nonimplementation files that make atomic git apply reject the entire reference."
        )
    before = b"+        args.hide_command = config['hide_commnad']\n"
    after = b"+        args.hide_command = config['hide_command']\n"
    if patch.count(before) != 1:
        raise ValueError("000838: expected one implementation config-key typo")
    return patch.replace(before, after, 1), (
        "Correct the implementation config-key typo hide_commnad to hide_command; "
        "the independent missing ruamel dependency requires an environment repair."
    )


def prepare(repo_root: Path, index: Path, output: Path) -> dict[str, Any]:
    input_bytes = index.read_bytes()
    members = sorted(
        (row for row in csv.DictReader(io.StringIO(input_bytes.decode()))
         if row["label"] == "oracle:pass+nop:fail"),
        key=lambda row: row["task_id"],
    )
    if len(members) != 567 or len({row["task_id"] for row in members}) != 567:
        raise ValueError("fleet membership must contain exactly 567 unique historical pass references")
    patch_dir = output / "references"
    patch_dir.mkdir(parents=True, exist_ok=True)
    release_rows: list[dict[str, str]] = []
    custody: list[dict[str, Any]] = []
    for row in members:
        source = Path(row["patch_path"])
        if not source.is_absolute():
            source = repo_root / source
        patch = source.read_bytes()
        if sha256(patch) != row["patch_sha256"].removeprefix("sha256:"):
            raise ValueError(f"{row['task_id']}: indexed reference patch checksum mismatch")
        recovered, reason = recover_patch(row["task_id"], patch)
        destination = patch_dir / f"{row['task_id']}.patch"
        destination.write_bytes(recovered)
        relative = destination.relative_to(repo_root).as_posix()
        changed = recovered != patch
        release_rows.append(row | {
            "patch_path": relative, "patch_sha256": sha256(recovered),
            "source": row["source"] + (";fleet-source-repair-unverified" if changed else ""),
        })
        custody.append({
            "task_id": row["task_id"], "fix_commit": row["fix_commit"],
            "original_source": row["source"], "original_patch_path": row["patch_path"],
            "original_patch_sha256": sha256(patch), "patch_path": relative,
            "patch_sha256": sha256(recovered), "bytes": len(recovered),
            "source_repaired": changed, "reason": reason,
            "qualification": "unmeasured; require exact-package native controls",
        })
    index_path = output / "references.csv"
    with index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(members[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(release_rows)
    report = {
        "schema_version": "mimo-v6-reference-custody/1",
        "membership": "historical index label == oracle:pass+nop:fail; not current validation",
        "source_index_path": str(index), "source_index_sha256": sha256(input_bytes),
        "references_index_path": index_path.relative_to(repo_root).as_posix(),
        "references_index_sha256": sha256(index_path.read_bytes()),
        "tasks": len(members), "source_repaired": sum(row["source_repaired"] for row in custody),
        "references": custody,
    }
    custody_path = output / "references.custody.json"
    custody_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {key: value for key, value in report.items() if key != "references"} | {
        "custody_sha256": sha256(custody_path.read_bytes()),
        "reference_bytes": sum(row["bytes"] for row in custody),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--reference-index", type=Path, default=INDEX)
    parser.add_argument("--output-dir", type=Path, default=RELEASE)
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    index = args.reference_index if args.reference_index.is_absolute() else repo / args.reference_index
    output = args.output_dir if args.output_dir.is_absolute() else repo / args.output_dir
    print(json.dumps(prepare(repo, index, output), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
