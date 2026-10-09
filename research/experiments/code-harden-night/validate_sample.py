#!/usr/bin/env python3
"""Stage sample-validation packages for the non-Python fleet ($0, local Docker).

For each sample task: materialize the strip and strip->mtime (chain) variant
packages from this checkout's lineage records, copy them to STAGE, and (oracle
trials only) add solution/solve.sh with a post-setup mtime listing plus the
reference fix extracted from the image's own git history.

Running the trials (one at a time, local Docker, egress locked) is then:

  uv run evallab run --task <staged-dir> --agent <nop|oracle> \\
      --name <job> --egress-lock --jobs-dir /private/tmp/mimo-night/code-harden/jobs
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
SNAP = Path(
    "/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/"
    "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)
SCRATCH = Path("/private/tmp/mimo-night/code-harden")
STAGE = SCRATCH / "stage"

sys.path.insert(0, str(WORKTREE / "src"))

from evallab.task_variants import default_variants_root  # noqa: E402

SAMPLES: dict[str, dict] = {
    "format-code-task-000007": {
        "workdir": "/testbed",
        # File overwrite (not git-apply): the base worktree has drifted from
        # the fix commit's parent, so the raw commit diff does not apply.
        # base -> cb977e4 touches only OrderMixin.sol + its test file.
        "oracle": ("file", "contracts/OrderMixin.sol", SCRATCH / "000007-fixed.sol"),
    },
    "format-code-task-000553": {
        "workdir": "/workspace/repo",
        "oracle": ("file", "main.go", SCRATCH / "000553-main.go"),
    },
}

MTIME_LISTING = """#!/bin/bash
# Post-setup work-tree mtime distribution (read-only; changes nothing).
CWD={workdir}
echo "== mtime_distinct $(find "$CWD" -printf '%T+\\n' | sort -u | wc -l | tr -d ' ')"
echo "== mtime_newer_than_fixed_count $(find "$CWD" -newermt '2000-01-01' -print | wc -l | tr -d ' ')"
"""


def variant_pkg(task_id: str, transform: str) -> tuple[Path, dict]:
    slug = f"mimo-v2.6-rl__{task_id}"
    matches = []
    for path in (WORKTREE / "library" / "task-variants" / slug).glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("transform") == transform and record.get("created_by") in (
            "code-harden-night",
            "har177-default-strip",
        ):
            matches.append((record.get("created_at", ""), path, record))
    if not matches:
        raise RuntimeError(f"no {transform} record for {task_id}")
    _, path, record = max(matches, key=lambda item: item[0])
    digest12 = record["variant_digest"].removeprefix("sha256:")[:12]
    pkg = default_variants_root(WORKTREE) / slug / digest12
    if not pkg.is_dir():
        raise RuntimeError(f"variant package missing: {pkg}")
    return pkg, record


def oracle_solve_sh(workdir: str, kind: str, target: str, artifact: Path) -> bytes:
    blob = base64.b64encode(artifact.read_bytes()).decode("ascii")
    lines = [
        MTIME_LISTING.format(workdir=workdir).rstrip("\n"),
        "set -eu",
        f'CWD="{workdir}"',
        'cd "$CWD"',
    ]
    if kind == "file":
        lines += [
            f"base64 -d > \"{target}\" <<'ORACLE_EOF'",
            blob,
            "ORACLE_EOF",
        ]
    elif kind == "patch":
        lines += [
            "base64 -d > /tmp/oracle-fix.patch <<'ORACLE_EOF'",
            blob,
            "ORACLE_EOF",
            "git apply /tmp/oracle-fix.patch",
        ]
    lines.append('echo "code-harden-night oracle: reference fix applied"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, default=STAGE)
    parser.add_argument("--manifest", type=Path, default=HERE / "validation-manifest.json")
    args = parser.parse_args()
    args.stage.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict] = {}
    # (task, package-kind, agent)
    plan = [
        ("format-code-task-000045", "chain", "nop"),
        ("format-code-task-000045", "strip", "nop"),
        ("format-code-task-000047", "chain", "nop"),
        ("format-code-task-000236", "chain", "nop"),
        ("format-code-task-000553", "chain", "nop"),
        ("format-code-task-000553", "chain", "oracle"),
        ("format-code-task-000007", "chain", "nop"),
        ("format-code-task-000007", "chain", "oracle"),
    ]
    transform_of = {"strip": "strip-future-history@1", "chain": "mtime-normalize@1"}
    for task_id, kind, agent in plan:
        if agent == "oracle" and SAMPLES[task_id]["oracle"] is None:
            continue
        pkg, record = variant_pkg(task_id, transform_of[kind])
        dirname = f"{task_id}-{kind}-{agent}"
        dest = args.stage / dirname
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(pkg, dest, symlinks=False)
        solve_sha = None
        if agent == "oracle":
            fix_kind, target, artifact = SAMPLES[task_id]["oracle"]
            solve = oracle_solve_sh(SAMPLES[task_id]["workdir"], fix_kind, target, artifact)
            sol_dir = dest / "solution"
            sol_dir.mkdir(exist_ok=True)
            (sol_dir / "solve.sh").write_bytes(solve)
            solve_sha = hashlib.sha256(solve).hexdigest()[:12]
        manifest[dirname] = {
            "task_id": task_id,
            "package": kind,
            "agent": agent,
            "variant_digest": record["variant_digest"],
            "variant_record": f"library/task-variants/mimo-v2.6-rl__{task_id}/{record['variant_digest'].removeprefix('sha256:')[:12]}.json",
            "solve_sha12": solve_sha,
        }
        print(f"staged {dest} variant={record['variant_digest'][:19]} solve={solve_sha}")
    args.manifest.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"manifest -> {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
