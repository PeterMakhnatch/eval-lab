#!/usr/bin/env python3
"""Static census over the 1,519 non-Python MiMo code tasks (no Docker).

Reads the pinned snapshot only. Writes ``census.json`` (one row per task)
and ``census-tables.md`` next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP = Path(
    "/Users/petermakhnatch/Developer/eval-lab/derived/task-store/hf/"
    "FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks"
)

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "src"))

from census_lib import bucket_unknown_runner, classify_downloads, rust_path_status  # noqa: E402

from evallab.task_lint import _hidden_shell_blobs  # noqa: E402


def task_category(task_dir: Path) -> str:
    try:
        return (
            tomllib.loads((task_dir / "task.toml").read_text())
            .get("metadata", {})
            .get("category", "?")
        )
    except OSError:
        return "unreadable"


def census_row(task_dir: Path) -> dict:
    category = task_category(task_dir)
    grading_text = "\n".join(_hidden_shell_blobs(task_dir))
    try:
        setup_text = (task_dir / "environment" / "setup" / "setup.sh").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        setup_text = ""
    row: dict = {"task_id": task_dir.name, "category": category}
    downloads = classify_downloads(grading_text)
    row["downloads"] = downloads
    row["download_tools"] = sorted({d["tool"] for d in downloads})
    row["unconditional_tools"] = sorted(
        {d["tool"] for d in downloads if d["mode"] == "unconditional"}
    )
    if category == "Rust":
        row["rust_path"] = rust_path_status(grading_text, setup_text)
    if category == "Unknown":
        row["runner_bucket"] = bucket_unknown_runner(grading_text)
    return row


def render_tables(rows: list[dict]) -> str:
    nonpy = [r for r in rows if r["category"] != "Python"]
    lines = ["# Code-harden-night static census", ""]
    lines.append(f"Snapshot tasks scanned: {len(rows)}; non-Python fleet: {len(nonpy)}.")
    lines.append("")

    # 1. Language recount.
    lines.append("## 1. Language recount (task.toml [metadata] category)")
    lines.append("")
    lines.append("| Language | Tasks |")
    lines.append("|---|---|")
    for lang, count in Counter(r["category"] for r in rows).most_common():
        lines.append(f"| {lang} | {count} |")
    lines.append("")

    # 2. verify-network-dep extension: unconditional downloaders by tool.
    lines.append("## 2. Grading-time downloads, non-Python fleet (extends mimo-verify-network-dep)")
    lines.append("")
    lines.append(
        "Mode key: **unconditional** = bare fetch in the grading path, breaks under "
        "the egress lock; **conditional** = guarded by a cache check (`if [ ! -d … ]`, "
        "`||`, `command -v`), offline-safe when baked; **offline-flag** = hermetic "
        "flag on the invocation (`GOPROXY=off`, `mvn -o`, `cargo --offline`, "
        "`-mod=vendor`). Counts are tasks with >=1 match of that mode for the tool."
    )
    lines.append("")
    lines.append("| Tool | Unconditional | Conditional | Offline-flag |")
    lines.append("|---|---|---|---|")
    tools = sorted({t for r in nonpy for t in r["download_tools"]})
    for tool in tools:
        u = sum(
            1
            for r in nonpy
            if any(d["tool"] == tool and d["mode"] == "unconditional" for d in r["downloads"])
        )
        c = sum(
            1
            for r in nonpy
            if any(d["tool"] == tool and d["mode"] == "conditional" for d in r["downloads"])
        )
        o = sum(
            1
            for r in nonpy
            if any(d["tool"] == tool and d["mode"] == "offline-flag" for d in r["downloads"])
        )
        lines.append(f"| {tool} | {u} | {c} | {o} |")
    lines.append("")
    flagged = sorted({r["task_id"] for r in nonpy if r["unconditional_tools"]})
    lines.append(
        f"Non-Python tasks with >=1 unconditional grading fetch: **{len(flagged)}** "
        "(prefetch-port candidates; dynamic-C4 truth still needs the locked nop census)."
    )
    lines.append("")
    # Per-category unconditional counts.
    lines.append("| Category | Tasks with unconditional fetch |")
    lines.append("|---|---|")
    for lang, _ in Counter(r["category"] for r in nonpy).most_common():
        n = sum(1 for r in nonpy if r["category"] == lang and r["unconditional_tools"])
        lines.append(f"| {lang} | {n} |")
    lines.append("")

    # 3. Rust PATH cases.
    rust = [r for r in nonpy if r["category"] == "Rust"]
    lines.append("## 3. Rust toolchain-PATH screen (static candidates)")
    lines.append("")
    for status, count in Counter(r.get("rust_path", "?") for r in rust).most_common():
        lines.append(f"- {status}: {count}")
    lines.append("")
    for r in rust:
        if r.get("rust_path") == "path-missing-candidate":
            lines.append(f"- {r['task_id']}: path-missing-candidate")
    lines.append("")

    # 4. Unknown-130 buckets.
    unk = [r for r in nonpy if r["category"] == "Unknown"]
    lines.append("## 4. Unknown-130 triaged by grading runner")
    lines.append("")
    lines.append("| Runner bucket | Tasks |")
    lines.append("|---|---|")
    for bucket, count in Counter(r.get("runner_bucket", "?") for r in unk).most_common():
        lines.append(f"| {bucket} | {count} |")
    lines.append("")
    lines.append("### Task ids per bucket")
    lines.append("")
    buckets: dict[str, list[str]] = {}
    for r in unk:
        buckets.setdefault(r.get("runner_bucket", "?"), []).append(r["task_id"])
    for bucket in sorted(buckets):
        lines.append(f"- {bucket}: {', '.join(sorted(buckets[bucket]))}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=SNAP)
    parser.add_argument("--out", type=Path, default=HERE / "census.json")
    parser.add_argument("--tables", type=Path, default=HERE / "census-tables.md")
    args = parser.parse_args()

    task_dirs = sorted(p for p in args.snapshot.iterdir() if p.is_dir())
    rows = [census_row(task_dir) for task_dir in task_dirs]
    args.out.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    args.tables.write_text(render_tables(rows) + "\n", encoding="utf-8")
    nonpy = sum(1 for r in rows if r["category"] != "Python")
    print(f"scanned {len(rows)} tasks ({nonpy} non-Python) -> {args.out}, {args.tables}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
