"""Incremental cProfile over a 2,000-run export; each invocation handles one CLI-sized batch."""

import cProfile
import json
import pstats

import audit

results = json.loads(audit.RESULT.read_text())
progress = results.setdefault("profile", {"runs": 0, "batches": 0})
registry = audit.CorpusRegistry()
with audit.CloudTraceDatabase(audit.DB, read_only=True) as db:
    ctx = audit.context(db, registry)
    admitted = ctx.admitted_task_ids()
    ids = [
        r["trace_id"]
        for r in db._rows("SELECT trace_id, task_id FROM agent_traces ORDER BY trace_id")
        if r["task_id"] in admitted
    ]
    start = progress["runs"]
    selection = ids[start : min(start + 100, 2000)]
    if selection:
        out = audit.TMP / "profile-out"
        profiler = cProfile.Profile()
        profiler.enable()
        audit.export(
            ctx, facets={}, trace_ids=selection, ids_file=None, limit=None, format="atif", out=out
        )
        profiler.disable()
        audit.clean(out)
        stats = pstats.Stats(profiler)
        filename = audit.TMP / "profile.pstats"
        if filename.exists():
            stats.add(str(filename))
        stats.dump_stats(str(filename))
        progress["runs"] += len(selection)
        progress["batches"] += 1
        entries = sorted(stats.stats.items(), key=lambda item: item[1][2], reverse=True)
        progress["top3_self_cpu"] = [
            {
                "function": f"{key[0].split('/')[-1]}:{key[2]}",
                "calls": value[1],
                "self_s": value[2],
                "cumulative_s": value[3],
            }
            for key, value in entries
        ][:3]
        entries = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)
        progress["top3_evallab_cumulative"] = [
            {
                "function": f"{key[0].split('/')[-1]}:{key[2]}",
                "calls": value[1],
                "self_s": value[2],
                "cumulative_s": value[3],
            }
            for key, value in entries
            if "evallab/" in key[0]
        ][:3]
        audit.save(results)
print(json.dumps(progress))
