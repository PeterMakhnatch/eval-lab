#!/usr/bin/env python3
"""Pin generated tau3 runtime images to the immutable tau2 v1.0.1 commit.

The Harbor adapter remains external and unchanged. The generated task template
accepts a repository URL but otherwise clones a moving default branch; this
bounded overlay makes each candidate runtime attest the exact source commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

COMMIT = "fc0055dc4e0a316c3f83133267fbd6faaa770992"
TAG = "v1.0.1"
MARKER = '    && git clone --depth=1 "${TAU2_BENCH_REPO}" "${TAU2_BENCH_ROOT}"'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin(path: Path) -> tuple[str, str]:
    original = path.read_text(encoding="utf-8")
    before = sha(path)
    if f"rev-parse HEAD)\" = \"{COMMIT}" in original:
        return before, before
    replacement = (
        f'    && git clone --depth=1 --branch "{TAG}" "${{TAU2_BENCH_REPO}}" "${{TAU2_BENCH_ROOT}}" \\\n'
        f'    && test "$(git -C "${{TAU2_BENCH_ROOT}}" rev-parse HEAD)" = "{COMMIT}"'
    )
    if original.count(MARKER) != 1:
        raise RuntimeError(f"unrecognised runtime template (expected one unpinned clone): {path}")
    path.write_text(original.replace(MARKER, replacement), encoding="utf-8")
    return before, sha(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("generated_root", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    files = sorted(args.generated_root.glob("tau3-banking_knowledge-task-*/environment/runtime-server/Dockerfile"))
    if len(files) != 8:
        raise SystemExit(f"expected 8 bounded banking runtimes, found {len(files)}")
    records = []
    for path in files:
        before, after = pin(path)
        task_dir = path.parents[3]
        records.append({"path": str(path), "before_sha256": f"sha256:{before}", "after_sha256": f"sha256:{after}", "task_toml_sha256": f"sha256:{sha(task_dir / 'task.toml')}", "task_config_sha256": f"sha256:{sha(task_dir / 'tests/config.json')}", "source_tag": TAG, "source_commit": COMMIT})
    evidence = args.evidence or args.generated_root.parent / "runtime-pin-evidence.json"
    evidence.write_text(json.dumps({"schema_version":"tau-runtime-pin/v1", "source_tag":TAG, "source_commit":COMMIT, "files":records}, indent=2) + "\n", encoding="utf-8")
    print(f"pinned {len(records)} runtime Dockerfiles to {TAG} ({COMMIT})")
    return 0


if __name__ == "__main__":
    main()
