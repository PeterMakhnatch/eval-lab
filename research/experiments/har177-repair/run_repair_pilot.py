#!/usr/bin/env python3
"""HAR-177 strip-future-history@1 pilot runner (Harbor 0.24, local Docker, $0).

Per pilot task the matrix is:

* ``orig-listing``: ledger parent + listing solve.sh (pre-repair leak shape).
* ``repaired-listing``: repair variant + listing solve.sh (post-repair shape).
* ``repaired-oracle``: repair variant + reference-fix solve.sh (grading 1?).
* ``repaired-nop``: repair variant as-is, nop agent (grading 0?).

The reference fix bytes are extracted from the task's own local image at
stage time (``git show <fix-sha>:<path>``) into ``/tmp`` only; no answer
bytes are committed. The listing solve.sh writes nothing to the repo, so its
grade doubles as a second nop control.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parent.parent.parent  # research/experiments/<exp> -> repo root
LEDGER = REPO_ROOT / "research/experiments/python-task-ledger/ledger.csv"
HF_CODE = Path(
    "/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/"
    "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)
VARIANTS = Path("/Users/petermakhnatch/Developer/eval-lab/derived/task-store/variants")
HARBOR_WHEEL = "/tmp/harbor024/harbor-0.24.0-py3-none-any.whl"
STAGE = Path("/tmp/har177-repair/stage")
FIXDIR = Path("/tmp/har177-repair/fix")

# Fix provenance: unreachable upstream commits named in the HAR-161 ledger
# reasons. Only non-test source files ship in the oracle solve.sh.
TASKS: dict[str, dict] = {
    "format-code-task-002402": {
        "parent": str(
            VARIANTS / "mimo-v2.6-rl__format-code-task-002402" / "c91c394831cf"
        ),
        "parent_source": {
            "kind": "variant",
            "record": "library/task-variants/mimo-v2.6-rl__format-code-task-002402/"
            "c91c394831cf.json",
        },
        "workdir": "/testbed",
        "fix_sha": "56f63eb604787879a4943008edeb9c50bd9207b6",
        "fix_files": {"numpyro/distributions/batch_util.py": "002402_batch_util.py"},
    },
    "format-code-task-002552": {
        "parent": str(HF_CODE / "format-code-task-002552"),
        "parent_source": {
            "kind": "hf",
            "repo": "FineEnvs/MiMo-V2.6-RL-harbor-code",
            "revision": "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785",
            "path": "tasks/format-code-task-002552",
        },
        "workdir": "/testbed",
        "fix_sha": "e88159fbac2f2f2673641586fa46517632e7b1f2",
        "fix_files": {"miio/miot_models.py": "002552_miot_models.py"},
    },
}

LISTING_SCRIPT = """#!/bin/bash
# har177-repair in-run listing: what post-base git history can the agent see?
# Read-only; changes nothing, so the grade is a second nop control.
M=/var/lib/mimo
CWD={workdir}
BASE=$(cat "$M/base" 2>/dev/null)
echo "== cwd $CWD base ${{BASE:-none}}"
for g in "$CWD/.git" "$M/git-hidden" "$M/git-hidden/.git"; do
  if [ ! -d "$g/objects" ]; then echo "== git $g absent"; continue; fi
  beyond=$( [ -n "$BASE" ] && git --git-dir="$g" rev-list --all --not "$BASE" 2>/dev/null | wc -l | tr -d ' ' )
  unreachable=$(git --git-dir="$g" fsck --unreachable --no-reflogs 2>/dev/null | grep -c ' commit ')
  echo "== git $g beyond_base=${{beyond:-unknown}} unreachable_commits=$unreachable"
done
if git --git-dir="$CWD/.git" cat-file -e '{fix_sha}^{{commit}}' 2>/dev/null; then
  echo "== known_future_commit observable"
else
  echo "== known_future_commit absent"
fi
echo "== tests_dir $( [ -e /tests ] && echo present || echo absent )"
"""


def ledger_digest(task_id: str) -> str:
    import csv

    with open(LEDGER, newline="") as handle:
        for row in csv.DictReader(handle):
            if row["task_id"] == task_id:
                return row["run_digest"]
    raise KeyError(task_id)


def cmd_derive(_args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from evallab.registry import task_directory_digest
    from evallab.strip_future_history import derive_strip_future_history
    from evallab.task_variants import VariantExistsError

    for task_id, spec in TASKS.items():
        parent = Path(spec["parent"])
        actual = task_directory_digest(parent)
        expected = ledger_digest(task_id)
        status = "OK " if actual == expected else "MISMATCH"
        print(f"{status} parent {task_id}: {actual[:19]} (ledger {expected[:19]})")
        if actual != expected:
            return 1
        try:
            record = derive_strip_future_history(
                parent,
                rationale=(
                    "HAR-177 repair pilot: strip git history beyond the recorded "
                    "base from the agent-observable environment; grading unchanged."
                ),
                created_by="har177-strip-future-history",
                repo_root=REPO_ROOT,
                parent_source=spec["parent_source"],
            )
            print(f"derived {task_id}: {record.variant_digest} record={record.digest12}")
        except VariantExistsError as exc:
            print(f"exists, skipping: {exc}")
    return 0


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        print(f"exists, skipping: {dest}")
        return
    shutil.copytree(src, dest, symlinks=False)
    for path in (dest, *dest.rglob("*")):
        path.chmod(path.stat().st_mode | 0o200)


def oracle_script(workdir: str, fix_sha: str, fix_files: dict[str, str]) -> bytes:
    """Reference-fix solve.sh; fix bytes travel as base64, never quoted."""
    lines = [
        LISTING_SCRIPT.format(workdir=workdir, fix_sha=fix_sha).rstrip("\n"),
        "set -eu",
        f'CWD="{workdir}"',
        'cd "$CWD"',
    ]
    for repo_path, staged in fix_files.items():
        blob = base64.b64encode((FIXDIR / staged).read_bytes()).decode("ascii")
        lines += [
            f'base64 -d > "{repo_path}" <<\'HAR177_EOF\'',
            blob,
            "HAR177_EOF",
        ]
    lines.append('echo "har177-repair oracle: reference fix applied"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def extract_fixes(spec: dict) -> None:
    """Extract oracle source privately from the immutable local image."""
    config = tomllib.loads((Path(spec["parent"]) / "task.toml").read_text())
    image = config["environment"]["docker_image"]
    FIXDIR.mkdir(parents=True, exist_ok=True)
    for repo_path, staged in spec["fix_files"].items():
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint",
                "git", "--workdir", spec["workdir"], image,
                "show", f"{spec['fix_sha']}:{repo_path}",
            ],
            capture_output=True,
            timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"cannot extract oracle source: {proc.stderr.decode()}")
        (FIXDIR / staged).write_bytes(proc.stdout)


def cmd_stage(_args: argparse.Namespace) -> int:
    for task_id, spec in TASKS.items():
        extract_fixes(spec)
        slug = f"mimo-v2.6-rl__{task_id}"
        records = sorted((REPO_ROOT / "library/task-variants" / slug).glob("*.json"))
        repair = [
            path
            for path in records
            if json.loads(path.read_text()).get("transform") == "strip-future-history@1"
        ]
        if len(repair) != 1:
            print(f"need exactly 1 repair record for {task_id}: {repair}")
            return 1
        digest12 = json.loads(repair[0].read_text())["variant_digest"].removeprefix(
            "sha256:"
        )[:12]
        variant_pkg = VARIANTS / slug / digest12

        staged_variants = {
            f"repaired-{task_id}-oracle": (variant_pkg, "oracle"),
            f"repaired-{task_id}-listing": (variant_pkg, "listing"),
            f"orig-{task_id}-listing": (Path(spec["parent"]), "listing"),
            f"repaired-{task_id}-plain": (variant_pkg, None),
        }
        for dirname, (pkg, solve_kind) in staged_variants.items():
            dest = STAGE / digest12 / dirname
            _copy_tree(pkg, dest)
            if solve_kind == "oracle":
                solve = oracle_script(spec["workdir"], spec["fix_sha"], spec["fix_files"])
            elif solve_kind == "listing":
                solve = LISTING_SCRIPT.format(
                    workdir=spec["workdir"], fix_sha=spec["fix_sha"]
                ).encode()
            else:
                continue
            sol_dir = dest / "solution"
            sol_dir.mkdir(exist_ok=True)
            (sol_dir / "solve.sh").write_bytes(solve)
            digest = hashlib.sha256(solve).hexdigest()[:12]
            print(f"staged {dest} solve={solve_kind} sha={digest}")
    return 0


def _jobs_dir(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def harbor(*args: str, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uvx", "--from", HARBOR_WHEEL, "harbor", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/tmp",
    )


def cmd_run(args: argparse.Namespace) -> int:
    jobs_dir = _jobs_dir(args.jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()
    proc = harbor(
        "run",
        "--env",
        "docker",
        "-a",
        args.agent,
        "-p",
        args.task_pkg,
        "--jobs-dir",
        str(jobs_dir),
        "--job-name",
        args.job_name,
        "-n",
        "1",
        "-y",
        "-q",
    )
    wall = time.time() - start
    print(proc.stdout[-3000:])
    print(proc.stderr[-3000:])
    print(f"rc={proc.returncode} wall_sec={wall:.0f}")
    return proc.returncode


def trial_summary(trial_dir: Path) -> dict:
    out: dict = {"trial_dir": str(trial_dir)}
    result_path = trial_dir / "result.json"
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
            out["reward"] = (result.get("verifier_result") or {}).get("rewards", {}).get(
                "reward"
            )
        except Exception as exc:  # noqa: BLE001
            out["result_error"] = str(exc)
    reward_path = trial_dir / "verifier" / "reward.txt"
    if reward_path.is_file():
        out["reward_txt"] = reward_path.read_text().strip()[:50]
    oracle_log = trial_dir / "agent" / "oracle.txt"
    if oracle_log.is_file():
        text = oracle_log.read_text(errors="replace")
        out["listing"] = [
            line
            for line in text.splitlines()
            if line.startswith("==") or "har177-repair oracle" in line
        ][:12]
    return out


def cmd_collect(args: argparse.Namespace) -> int:
    jobs_dir = _jobs_dir(args.jobs_dir)
    rows = []
    for trial_dir in sorted(jobs_dir.glob("*/*/result.json")):
        rows.append(trial_summary(trial_dir.parent))
    out_path = Path(args.out)
    out_path = out_path if out_path.is_absolute() else REPO_ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"collected_at": datetime.now(UTC).isoformat(), "rows": rows}, indent=2
        )
    )
    print(f"collected {len(rows)} trials -> {out_path}")
    for row in rows:
        print(f"- {row['trial_dir']}: reward={row.get('reward')}")
        for line in row.get("listing", []):
            print(f"    {line}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("derive")
    sub.add_parser("stage")
    run = sub.add_parser("run")
    run.add_argument("--task-pkg", required=True)
    run.add_argument("--agent", required=True, choices=["oracle", "nop"])
    run.add_argument("--job-name", required=True)
    run.add_argument("--jobs-dir", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--jobs-dir", required=True)
    collect.add_argument("--out", required=True)
    args = parser.parse_args()
    handlers = {
        "derive": cmd_derive,
        "stage": cmd_stage,
        "run": cmd_run,
        "collect": cmd_collect,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
