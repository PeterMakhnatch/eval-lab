"""Verify unchanged cluster policy, format samples, and CLI facet selection."""

import json
import os
import subprocess

import audit

results = json.loads(audit.RESULT.read_text())
results["rejected_policy_option"] = {
    "refused_rows": 3,
    "baseline_rows": 3435,
    "hit_files": 6,
    "files": 136,
    "applied": False,
}
registry = audit.CorpusRegistry()
with audit.CloudTraceDatabase(audit.DB, read_only=True) as db:
    ctx = audit.context(db, registry)
    with db._transaction():
        admitted, seals = ctx.retriever._admitted_ids()
    pattern = audit._sealed_pattern(frozenset(seals))
    owners = db._rows("SELECT trace_id, task_id FROM agent_traces ORDER BY trace_id")
    ids = {r["trace_id"] for r in owners if r["task_id"] in admitted}
    stats = {
        "clusters": 0,
        "rows": 0,
        "refused": 0,
        "missing": 0,
        "extra": 0,
        "nondeterministic_files": 0,
        "class_b_hit_files": 0,
        "class_c_hit_files": 0,
    }
    results["exposure"]["cluster_scope"] = {"files": 0, "hit_files": 0}
    identity_keys = {"task_id", "native_task_id", "canonical_task_id", "trace_id"}
    for row in db._rows("SELECT cluster_id, run_ids FROM run_clusters ORDER BY cluster_id"):
        paths = [audit.TMP / "scope-a.json", audit.TMP / "scope-b.json"]
        for p in paths:
            audit.export_cluster_scope(
                audit.DB, row["cluster_id"], p, registry=registry, revision="audit"
            )
            audit.scan(p, pattern, results, "cluster_scope")
            doc = json.loads(p.read_text())
            identities = [{k: v for k, v in r.items() if k in identity_keys} for r in doc["rows"]]
            remainder = {
                **doc,
                "rows": [
                    {k: v for k, v in r.items() if k not in identity_keys} for r in doc["rows"]
                ],
            }
            stats["class_b_hit_files"] += int(
                bool(pattern.search(json.dumps(identities, ensure_ascii=False).casefold()))
            )
            stats["class_c_hit_files"] += int(
                bool(pattern.search(json.dumps(remainder, ensure_ascii=False).casefold()))
            )
        expected = set(row["run_ids"]) & ids
        actual = {r["trace_id"] for r in doc["rows"]}
        stats["clusters"] += 1
        stats["rows"] += len(actual)
        stats["refused"] += doc["counts"]["refused"]
        stats["missing"] += len(expected - actual)
        stats["extra"] += len(actual - expected)
        stats["nondeterministic_files"] += int(paths[0].read_bytes() != paths[1].read_bytes())
        for p in paths:
            p.unlink()
    results["cluster_parity"] = stats
    results["format_samples"] = []
    for format in ["harbor", "scout"]:
        out = audit.TMP / "format-out"
        reply = audit.export(
            audit.context(db, registry),
            facets={},
            trace_ids=sorted(ids)[:50],
            ids_file=None,
            limit=None,
            format=format,
            out=out,
        )
        actual, hashes = audit.inspect(out, pattern, results)
        results["format_samples"].append(
            {
                "format": format,
                "exported": len(actual),
                "missing": len(set(sorted(ids)[:50]) - actual),
                "files": len(hashes),
            }
        )
        audit.clean(out)
    out = audit.TMP / "facet-cli"
    reply = audit.TMP / "facet-reply.json"
    err = audit.TMP / "facet-stderr.txt"
    with reply.open("w") as stream, err.open("w") as errors:
        process = subprocess.run(
            [
                "uv",
                "run",
                "--no-sync",
                "evallab",
                "qa-data",
                "export",
                "--cloud-database",
                audit.DB,
                "--facet",
                "outcome=pass",
                "--limit",
                "50",
                "--out",
                str(out),
                "--format",
                "atif",
                "--budget",
                "1000000",
            ],
            stdout=stream,
            stderr=errors,
            env={**os.environ, "EVALLAB_DISABLE_LOCAL_DOCKER": "1", "UV_OFFLINE": "1"},
        )
    assert process.returncode == 0, {
        "returncode": process.returncode,
        "stderr_bytes": err.stat().st_size,
    }
    audit.scan(reply, pattern, results, "evidence_reply")
    actual, _ = audit.inspect(out, pattern, results)
    expected = {
        r["run_id"]
        for r in db.search_runs(
            {"outcome": "pass"},
            registry=registry,
            sealed_task_ids=ctx.retriever._sealed_ids,
            limit=50,
        )
    }
    results["cli_facet_parity"] = {
        "selected": len(expected),
        "exported": len(actual),
        "missing": len(expected - actual),
        "extra": len(actual - expected),
    }
    audit.clean(out)
    reply.unlink()
    err.unlink()
audit.save(results)
print(json.dumps({k: results[k] for k in ("cluster_parity", "format_samples", "cli_facet_parity")}))
