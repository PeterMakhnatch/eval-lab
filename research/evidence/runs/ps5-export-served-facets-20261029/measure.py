"""Counts-only before/after measurement of `qa-data export` and `export-cluster-scope` on a store copy.

usage: measure.py OLD_WORKTREE NEW_WORKTREE STORE SCRATCH RESULTS_JSON [N]

Runs the real CLI from each worktree against a read-only store copy, compares the JSON replies
(and the cluster-scope manifest) with run-specific fields removed, and takes CPU medians of
interleaved old/new runs (user+sys of the child process).
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import sqlite3
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

WITHHELD = "retained-trace:b493cd8d3a512fe3bd44d165bfb0f6a4"
ORDINARY = "retained-trace:227bec15e5cf1617b2d3f56225f8e1a3"
CLUSTER = "L1-07"
VOLATILE = {
    "blind_id", "file", "output_directory", "sealed_manifest", "manifest_sha256", "manifest_path",
    "recorded_at_utc", "elapsed_ms", "path",
}


def cli(worktree: Path, store: str, *args: str) -> tuple[Any, float]:
    env = {**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    done = subprocess.run(
        ["uv", "run", "--no-sync", "evallab", *args, "--cloud-database" if args[0] == "qa-data" else "--source-database", store],
        cwd=worktree, env=env, capture_output=True, text=True, check=False,
    )
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    try:
        return json.loads(done.stdout), cpu
    except ValueError:
        raise SystemExit(f"{args[:3]} failed: {done.stdout[-300:]!r} {done.stderr[-300:]!r}") from None


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if key not in VOLATILE and key != "meta"}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def export(worktree: Path, store: str, scratch: Path, label: str, run_id: str) -> tuple[Any, float]:
    out = scratch / label
    for path in (out, out.with_name(out.name + ".sealed-manifest.json")):
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
    return cli(worktree, store, "qa-data", "export", "--trace-id", run_id, "--out", str(out))


def scope(worktree: Path, store: str, scratch: Path, label: str, cluster: str) -> tuple[Any, float, Any]:
    manifest = scratch / f"{label}.json"
    manifest.unlink(missing_ok=True)
    reply, cpu = cli(
        worktree, store, "prod", "export-cluster-scope", "--cluster-id", cluster, "--manifest", str(manifest),
    )
    return reply, cpu, json.loads(manifest.read_text(encoding="utf-8"))


def containing_clusters(store: str, run_id: str) -> list[str]:
    uri = Path(store).resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return [row[0] for row in connection.execute(
            "SELECT cluster_id FROM run_clusters WHERE instr(run_ids, ?) > 0 ORDER BY cluster_id", (run_id,),
        )]
    finally:
        connection.close()


def main() -> None:
    old, new = Path(sys.argv[1]), Path(sys.argv[2])
    store, scratch, results = sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])
    runs = int(sys.argv[6]) if len(sys.argv) > 6 else 7
    scratch.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"n": runs}

    for name, run_id in (("withheld", WITHHELD), ("ordinary", ORDINARY)):
        before, _ = export(old, store, scratch, f"{name}-old", run_id)
        after, _ = export(new, store, scratch, f"{name}-new", run_id)
        report[f"export_{name}"] = {
            "old_runs": scrub(before["data"]["runs"]), "new_runs": scrub(after["data"]["runs"]),
            "rest_identical": scrub({k: v for k, v in before["data"].items() if k != "runs"})
            == scrub({k: v for k, v in after["data"].items() if k != "runs"}),
        }

    clusters = containing_clusters(store, WITHHELD)
    report["clusters_containing_withheld_run"] = clusters
    for cluster in dict.fromkeys([CLUSTER, *clusters]):
        before_reply, _, before_doc = scope(old, store, scratch, f"scope-old-{cluster}", cluster)
        after_reply, _, after_doc = scope(new, store, scratch, f"scope-new-{cluster}", cluster)
        old_rows = {row["trace_id"]: row for row in before_doc["rows"]}
        new_rows = {row["trace_id"]: row for row in after_doc["rows"]}
        changed = {
            trace: {"old": [old_rows[trace]["facet_producer_revision"], old_rows[trace]["facet_served_model"]],
                    "new": [new_rows[trace]["facet_producer_revision"], new_rows[trace]["facet_served_model"]]}
            for trace in old_rows if old_rows[trace] != new_rows.get(trace)
        }
        report[f"scope_{cluster}"] = {
            "members": before_doc["counts"]["members"], "rows_old": len(old_rows), "rows_new": len(new_rows),
            "same_trace_ids": sorted(old_rows) == sorted(new_rows),
            "counts_identical": before_doc["counts"] == after_doc["counts"],
            "document_identical_except_rows": {k: v for k, v in before_doc.items() if k != "rows"}
            == {k: v for k, v in after_doc.items() if k != "rows"},
            "reply_identical": scrub(before_reply) == scrub(after_reply),
            "rows_changed": changed,
        }

    cpu: dict[str, dict[str, list[float]]] = {
        kind: {"old": [], "new": []} for kind in ("export_withheld", "export_ordinary", f"scope_{CLUSTER}")
    }
    for index in range(runs):
        for side, tree in (("old", old), ("new", new)) if index % 2 == 0 else (("new", new), ("old", old)):
            cpu["export_withheld"][side].append(export(tree, store, scratch, f"cpu-{side}-w", WITHHELD)[1])
            cpu["export_ordinary"][side].append(export(tree, store, scratch, f"cpu-{side}-o", ORDINARY)[1])
            cpu[f"scope_{CLUSTER}"][side].append(scope(tree, store, scratch, f"cpu-{side}-s", CLUSTER)[1])
    report["cpu_seconds"] = {
        kind: {side: {"median": round(statistics.median(values), 3), "samples": [round(v, 3) for v in values]}
               for side, values in sides.items()}
        for kind, sides in cpu.items()
    }
    results.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
