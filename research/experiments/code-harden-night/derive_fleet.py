#!/usr/bin/env python3
"""Derive strip-future-history@1 + mtime-normalize@1 for the non-Python fleet.

Unchanged transforms, chained strip -> mtime (the mtime parent is the strip
variant package, matching the validated Python chain). Records land in this
checkout's ``library/task-variants/`` with status ``candidate``; packages go
to the shared variants store. Idempotent: existing records/packages are
skipped, failures are collected into ``derive-report.json``, never fatal.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKTREE = HERE.parents[2]
SNAP = Path(
    "/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/"
    "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)
HF_REPO = "FineEnvs/MiMo-V2.6-RL-harbor-code"
HF_REVISION = "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785"
CREATED_BY = "code-harden-night"

sys.path.insert(0, str(WORKTREE / "src"))

from evallab.mtime_normalize import derive_mtime_normalize  # noqa: E402
from evallab.strip_future_history import derive_strip_future_history  # noqa: E402
from evallab.task_variants import VariantExistsError, VariantInvalid  # noqa: E402


def fleet_tasks(snapshot: Path) -> list[str]:
    out = []
    for task_dir in sorted(p for p in snapshot.iterdir() if p.is_dir()):
        try:
            category = (
                tomllib.loads((task_dir / "task.toml").read_text())
                .get("metadata", {})
                .get("category", "?")
            )
        except OSError:
            continue
        if category != "Python":
            out.append(task_dir.name)
    return out


def strip_record_path(task_id: str) -> Path | None:
    slug = f"mimo-v2.6-rl__{task_id}"
    candidates = sorted((WORKTREE / "library" / "task-variants" / slug).glob("*.json"))
    strips: list[tuple[str, Path]] = []
    for path in candidates:
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("transform") == "strip-future-history@1":
            strips.append((record.get("created_at", ""), path))
    if not strips:
        return None
    # Latest first (mirrors the ledger's strip_pick): a fixed-transform
    # re-derive supersedes an older record.
    return max(strips, key=lambda item: item[0])[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=SNAP)
    parser.add_argument("--report", type=Path, default=HERE / "derive-report.json")
    parser.add_argument("--limit", type=int, default=0, help="0 = whole fleet")
    parser.add_argument("--task", action="append", default=[], help="restrict to task ids")
    args = parser.parse_args()

    tasks = fleet_tasks(args.snapshot)
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t in wanted]
    if args.limit:
        tasks = tasks[: args.limit]

    report: dict[str, list[str]] = {
        "strip_derived": [],
        "strip_exists": [],
        "strip_failed": [],
        "mtime_derived": [],
        "mtime_exists": [],
        "mtime_failed": [],
    }
    failures: dict[str, str] = {}

    for i, task_id in enumerate(tasks, 1):
        parent = args.snapshot / task_id
        hf_source = {
            "kind": "hf",
            "repo": HF_REPO,
            "revision": HF_REVISION,
            "path": f"tasks/{task_id}",
        }
        try:
            record = derive_strip_future_history(
                parent,
                rationale=(
                    "code-harden-night: strip future git history for the non-Python "
                    "code fleet (transform unchanged; language-blind)."
                ),
                created_by=CREATED_BY,
                repo_root=WORKTREE,
                parent_source=hf_source,
            )
            report["strip_derived"].append(task_id)
            print(f"[{i}/{len(tasks)}] {task_id} strip derived {record.digest12}")
        except VariantExistsError:
            report["strip_exists"].append(task_id)
        except VariantInvalid as exc:
            report["strip_failed"].append(task_id)
            failures[f"{task_id}/strip"] = str(exc)[:300]
            print(f"[{i}/{len(tasks)}] {task_id} strip FAILED: {exc}")
            continue

        # The mtime parent is the strip variant package in the shared store.
        record_path = strip_record_path(task_id)
        if record_path is None:
            # Freshly derived above but unreadable (should not happen).
            report["mtime_failed"].append(task_id)
            failures[f"{task_id}/mtime"] = "strip record not found after derive"
            continue
        record_json = json.loads(record_path.read_text())
        digest12 = record_json["variant_digest"].removeprefix("sha256:")[:12]
        slug = f"mimo-v2.6-rl__{task_id}"
        from evallab.task_variants import default_variants_root

        strip_pkg = default_variants_root(WORKTREE) / slug / digest12
        if not strip_pkg.is_dir():
            report["mtime_failed"].append(task_id)
            failures[f"{task_id}/mtime"] = f"strip package missing: {strip_pkg}"
            continue
        try:
            mtime = derive_mtime_normalize(
                strip_pkg,
                rationale=(
                    "code-harden-night: normalize work-tree mtimes on the strip "
                    "variant (chain strip -> mtime; transform unchanged)."
                ),
                created_by=CREATED_BY,
                repo_root=WORKTREE,
                parent_source={
                    "kind": "variant",
                    "record": str(record_path.relative_to(WORKTREE)),
                },
            )
            report["mtime_derived"].append(task_id)
            print(f"[{i}/{len(tasks)}] {task_id} mtime derived {mtime.digest12}")
        except VariantExistsError:
            report["mtime_exists"].append(task_id)
        except VariantInvalid as exc:
            report["mtime_failed"].append(task_id)
            failures[f"{task_id}/mtime"] = str(exc)[:300]
            print(f"[{i}/{len(tasks)}] {task_id} mtime FAILED: {exc}")

    report["failures"] = failures  # type: ignore[assignment]
    args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(
        "strip: derived={} exists={} failed={} | mtime: derived={} exists={} failed={}".format(
            len(report["strip_derived"]),
            len(report["strip_exists"]),
            len(report["strip_failed"]),
            len(report["mtime_derived"]),
            len(report["mtime_exists"]),
            len(report["mtime_failed"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
