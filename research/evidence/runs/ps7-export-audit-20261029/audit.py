"""Read-only exporter audit; raw exports stay in scratch and are removed after scanning."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
import pstats
import shutil
import subprocess
import time
from pathlib import Path

from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    _sealed_pattern,
)
from evallab.production.cloud_trace_export import export_cluster_scope
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.qa_data import Budget, QaContext
from evallab.production.qa_data_export import export

ROOT = Path.cwd()
TMP = Path("/private/tmp/ps7j2")
DB = "/private/tmp/ps2/store.sqlite"
RESULT = TMP / "results.json"


def save(data):
    RESULT.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def context(db, registry):
    return QaContext(db, CloudTraceRetriever(db, registry=registry), ROOT, TMP, Budget(1000000), DB)


def scan(path, pattern, results, kind):
    text = path.read_text()
    # Scan both serialized bytes and decoded string values (JSON escapes must not hide identities).
    hits = bool(pattern.search(text.casefold()))
    try:
        decoded = json.loads(text)
        hits |= bool(pattern.search(json.dumps(decoded, ensure_ascii=False).casefold()))
    except ValueError:
        pass
    stats = results.setdefault("exposure", {}).setdefault(kind, {"files": 0, "hit_files": 0})
    stats["files"] += 1
    stats["hit_files"] += int(hits)


def inspect(out, pattern, results):
    manifest = out.with_name(out.name + ".sealed-manifest.json")
    entries = json.loads(manifest.read_text())
    hashes = {"manifest": hashlib.sha256(manifest.read_bytes()).hexdigest()}
    scan(manifest, pattern, results, "sealed_manifest")
    for path in sorted(out.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(out))] = hashlib.sha256(path.read_bytes()).hexdigest()
            scan(path, pattern, results, "blinded_transcript")
    return {entry["trace_id"] for entry in entries.values()}, hashes


def clean(out):
    shutil.rmtree(out, ignore_errors=True)
    out.with_name(out.name + ".sealed-manifest.json").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["init", "work", "profile"])
    parser.add_argument("--seconds", type=int, default=170)
    args = parser.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)
    results = json.loads(RESULT.read_text()) if RESULT.exists() else {}
    registry = CorpusRegistry()
    with CloudTraceDatabase(DB, read_only=True) as db:
        ctx = context(db, registry)
        with db._transaction():
            admitted, seals = ctx.retriever._admitted_ids()
        pattern = _sealed_pattern(frozenset(seals))
        owners = db._rows("SELECT trace_id, task_id FROM agent_traces ORDER BY trace_id")
        ids = [r["trace_id"] for r in owners if r["task_id"] in admitted]
        facets = db.search_runs(registry=registry, sealed_task_ids=ctx.retriever._sealed_ids)
        if args.mode == "init":
            results = {
                "population": {"admitted_runs": len(ids), "served_facets": len(facets)},
                "parity": [],
                "jobs": [],
                "throughput": [],
            }
            # Twenty distinct public typed filters, never task or trace text.
            filters = []
            for field in (
                "outcome",
                "tests",
                "eval_awareness",
                "grader_or_harness_touched",
                "answer_material_accessed",
                "success_claim_without_evidence",
                "suspicion",
            ):
                for value in sorted({r[field] for r in facets}, key=str):
                    filters.append({field: value})
            filters = filters[:20]
            results["filters"] = filters
            for i, f in enumerate(filters):
                selected = db.search_runs(
                    f, registry=registry, sealed_task_ids=ctx.retriever._sealed_ids
                )
                results["parity"].append(
                    {
                        "filter_index": i,
                        "selected": len(selected),
                        "exported": 0,
                        "missing": 0,
                        "extra": 0,
                    }
                )
                # Exercise real facet selection with limit, then cover the rest through explicit batches.
                results["jobs"].append({"kind": "facet", "filter": i, "offset": 0})
                for offset in range(200, len(selected), 200):
                    results["jobs"].append({"kind": "facet_rest", "filter": i, "offset": offset})
            for offset in range(0, len(ids), 200):
                results["jobs"].append({"kind": "whole", "offset": offset})
            results["whole_parity"] = {
                "selected": len(ids),
                "exported": 0,
                "missing": 0,
                "extra": 0,
            }
            clusters = db._rows("SELECT cluster_id, run_ids FROM run_clusters ORDER BY cluster_id")
            results["cluster_parity"] = {
                "clusters": len(clusters),
                "rows": 0,
                "refused": 0,
                "missing": 0,
                "extra": 0,
                "nondeterministic_files": 0,
            }
            for row in clusters:
                a, b = TMP / "scope-a.json", TMP / "scope-b.json"
                for path in (a, b):
                    path.unlink(missing_ok=True)
                    export_cluster_scope(
                        DB, row["cluster_id"], path, registry=registry, revision="audit"
                    )
                    scan(path, pattern, results, "cluster_scope")
                doc = json.loads(a.read_text())
                expected = set(row["run_ids"]) & set(ids)
                actual = {r["trace_id"] for r in doc["rows"]}
                stats = results["cluster_parity"]
                stats["rows"] += len(actual)
                stats["refused"] += doc["counts"]["refused"]
                stats["missing"] += len(expected - actual)
                stats["extra"] += len(actual - expected)
                stats["nondeterministic_files"] += int(a.read_bytes() != b.read_bytes())
                a.unlink()
                b.unlink()
                save(results)
            for n in (50, 200, 500, 1000, 2000):
                for repeat in range(3):
                    results["throughput"].append(
                        {
                            "runs": n,
                            "repeat": repeat,
                            "completed": 0,
                            "cpu_s": 0.0,
                            "wall_s": 0.0,
                            "peak_rss_bytes": 0,
                        }
                    )
                    for offset in range(0, n, 200):
                        results["jobs"].append(
                            {
                                "kind": "cli",
                                "series": len(results["throughput"]) - 1,
                                "offset": offset,
                            }
                        )
            # Three batched 500-run CLI exports; request order is documented, not canonical.
            results["determinism"] = {
                "runs": 500,
                "different_files": 0,
                "compared_files": 0,
                "reverse_order_required": False,
            }
            for repeat in range(2):
                for offset in range(0, 500, 200):
                    results["jobs"].append(
                        {"kind": "determinism", "repeat": repeat, "offset": offset}
                    )
            results["completed_jobs"] = 0
            save(results)
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in results.items()
                        if k in ("population", "cluster_parity", "exposure")
                    }
                )
            )
            return
        if args.mode == "profile":
            profiler = cProfile.Profile()
            profiler.enable()
            for offset in range(0, 2000, 200):
                out = TMP / "profile-out"
                export(
                    context(db, registry),
                    facets={},
                    trace_ids=ids[offset : offset + 200],
                    ids_file=None,
                    limit=None,
                    format="atif",
                    out=out,
                )
                clean(out)
            profiler.disable()
            stats = pstats.Stats(profiler)
            entries = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)
            results["profile_top3"] = [
                {
                    "function": f"{key[0].split('/')[-1]}:{key[2]}",
                    "calls": value[1],
                    "self_s": value[2],
                    "cumulative_s": value[3],
                }
                for key, value in entries
                if "evallab/" in key[0]
            ][:3]
            save(results)
            print(json.dumps(results["profile_top3"]))
            return
        started = time.monotonic()
        while (
            results["completed_jobs"] < len(results["jobs"])
            and time.monotonic() - started < args.seconds
        ):
            job = results["jobs"][results["completed_jobs"]]
            kind = job["kind"]
            offset = job["offset"]
            out = TMP / "export"
            if kind.startswith("facet"):
                selected = db.search_runs(
                    results["filters"][job["filter"]],
                    registry=registry,
                    sealed_task_ids=ctx.retriever._sealed_ids,
                )
                wanted = [r["run_id"] for r in selected][offset : offset + 200]
            else:
                n = (
                    results["throughput"][job["series"]]["runs"]
                    if kind == "cli"
                    else 500
                    if kind == "determinism"
                    else len(ids)
                )
                wanted = ids[offset : min(offset + 200, n)]
            if not wanted:
                results["completed_jobs"] += 1
                save(results)
                continue
            if kind in ("cli", "determinism"):
                idsfile = TMP / "ids.txt"
                idsfile.write_text("\n".join(wanted))
                reply = TMP / "reply.json"
                err = TMP / "stderr.txt"
                command = [
                    "uv",
                    "run",
                    "--no-sync",
                    "evallab",
                    "qa-data",
                    "export",
                    "--cloud-database",
                    DB,
                    "--ids-file",
                    str(idsfile),
                    "--format",
                    "atif",
                    "--out",
                    str(out),
                    "--budget",
                    "1000000",
                ]
                before = time.monotonic()
                with reply.open("w") as stdout, err.open("w") as stderr:
                    process = subprocess.Popen(
                        command,
                        stdout=stdout,
                        stderr=stderr,
                        env={**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1", "UV_OFFLINE": "1"},
                    )
                    _, status, usage = os.wait4(process.pid, 0)
                    process.returncode = os.waitstatus_to_exitcode(status)
                elapsed = time.monotonic() - before
                if process.returncode:
                    print(
                        json.dumps(
                            {"cli_failure": process.returncode, "stderr_bytes": err.stat().st_size}
                        )
                    )
                    return
                scan(reply, pattern, results, "evidence_reply")
                reply_doc = json.loads(reply.read_text())
                reply_doc["meta"]["latency_ms"] = 0.0
                reply_hash = hashlib.sha256(
                    json.dumps(reply_doc, sort_keys=True).encode()
                ).hexdigest()
                if kind == "cli":
                    series = results["throughput"][job["series"]]
                    series["completed"] += len(wanted)
                    series["cpu_s"] += usage.ru_utime + usage.ru_stime
                    series["wall_s"] += elapsed
                    series["peak_rss_bytes"] = max(series["peak_rss_bytes"], usage.ru_maxrss)
                reply.unlink()
                err.unlink()
                idsfile.unlink()
            else:
                reply_data = export(
                    context(db, registry),
                    facets=results["filters"][job["filter"]] if kind == "facet" else {},
                    trace_ids=[] if kind == "facet" else wanted,
                    ids_file=None,
                    limit=200 if kind == "facet" else None,
                    format="atif",
                    out=out,
                )
                reply = TMP / "reply.json"
                reply.write_text(json.dumps(reply_data))
                scan(reply, pattern, results, "evidence_reply")
                reply.unlink()
            actual, hashes = inspect(out, pattern, results)
            if kind in ("whole", "facet", "facet_rest"):
                stats = (
                    results["whole_parity"] if kind == "whole" else results["parity"][job["filter"]]
                )
                stats["exported"] += len(actual)
                stats["missing"] += len(set(wanted) - actual)
                stats["extra"] += len(actual - set(wanted))
            if kind == "determinism":
                hashes["evidence_reply"] = reply_hash
                hashfile = TMP / f"hashes-{offset}.json"
                if job["repeat"] == 0:
                    hashfile.write_text(json.dumps(hashes))
                else:
                    previous = json.loads(hashfile.read_text())
                    keys = set(previous) | set(hashes)
                    results["determinism"]["compared_files"] += len(keys)
                    results["determinism"]["different_files"] += sum(
                        previous.get(k) != hashes.get(k) for k in keys
                    )
                    hashfile.unlink()
            clean(out)
            results["completed_jobs"] += 1
            save(results)
            print(
                json.dumps(
                    {
                        "completed_jobs": results["completed_jobs"],
                        "total_jobs": len(results["jobs"]),
                        "kind": kind,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
