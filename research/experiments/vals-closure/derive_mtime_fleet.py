#!/usr/bin/env python3
"""Derive mtime-normalize@1 across the Python usable fleet (candidate records).

Parent choice per task: the strip-future-history@1 variant package when it
exists in the shared store, else the pinned snapshot package. Tasks with an
existing mtime-normalize@1 record are skipped. All records derive as
``candidate``; the 10-task validation sample is marked ``validated`` with
evidence separately. CodeHarden owns non-Python; this fleet is the Python
ledger's 1,146 usable rows (002361 included: it is in the Python ledger, and
CodeHarden drops their duplicate at integration).

Usage: derive_mtime_fleet.py [--limit N] [--task ID...]
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(WORKTREE / "src"))

PRIMARY = Path("/Users/petermakhnatch/Developer/eval-lab")
SNAP = PRIMARY / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
STORE = PRIMARY / "derived/task-store/variants"
LEDGER = PRIMARY / "research/experiments/python-task-ledger/ledger.csv"

RATIONALE = (
    "Vals-closure fleet: normalize work-tree mtimes to one fixed stamp so "
    "fix-commit file mtimes cannot name the fix. Fail setup if any path "
    "stays newer; grading is unchanged. Candidate pending validation."
)


def existing_mtime(slug: str) -> bool:
    for path in glob.glob(str(WORKTREE / "library/task-variants" / slug / "*.json")):
        try:
            with open(path) as handle:
                if json.load(handle).get("transform") == "mtime-normalize@1":
                    return True
        except (OSError, ValueError):
            continue
    return False


def strip_parent(tid: str) -> tuple[Path, dict] | None:
    slug = f"mimo-v2.6-rl__{tid}"
    for path in sorted(glob.glob(str(WORKTREE / "library/task-variants" / slug / "*.json"))):
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if data.get("transform") != "strip-future-history@1":
            continue
        digest12 = Path(path).stem
        pkg = STORE / slug / digest12
        if pkg.is_dir():
            return pkg, {
                "kind": "variant",
                "record": f"library/task-variants/{slug}/{digest12}.json",
            }
    return None


def main() -> int:
    from evallab.mtime_normalize import derive_mtime_normalize
    from evallab.task_variants import VariantExistsError, VariantInvalid

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--task", action="append", default=[])
    args = parser.parse_args()

    with open(LEDGER, newline="") as handle:
        rows = [r for r in csv.DictReader(handle) if r["status"] == "usable"]
    if args.task:
        wanted = {
            t if t.startswith("format-code-task-") else f"format-code-task-{t}" for t in args.task
        }
        rows = [r for r in rows if r["task_id"] in wanted]
    if args.limit:
        rows = rows[: args.limit]

    derived, skipped, failed = 0, 0, []
    for row in rows:
        tid = row["task_id"]
        slug = f"mimo-v2.6-rl__{tid}"
        if existing_mtime(slug):
            skipped += 1
            continue
        found = strip_parent(tid)
        if found is None:
            snap_pkg = SNAP / tid
            if not snap_pkg.is_dir():
                failed.append((tid, "no strip package and no snapshot"))
                continue
            parent, source = snap_pkg, None
        else:
            parent, source = found
        try:
            derive_mtime_normalize(
                parent,
                rationale=RATIONALE,
                created_by="vals-closure-fleet",
                repo_root=WORKTREE,
                parent_source=source,
            )
            derived += 1
            if derived % 100 == 0:
                print(f"  ...{derived} derived", flush=True)
        except (VariantExistsError, VariantInvalid) as exc:
            failed.append((tid, f"{type(exc).__name__}: {exc}"))
    print(f"derived={derived} skipped_existing={skipped} failed={len(failed)}")
    for tid, reason in failed[:20]:
        print(f"  FAIL {tid}: {reason}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
