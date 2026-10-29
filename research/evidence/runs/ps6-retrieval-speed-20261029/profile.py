import cProfile
import json
import pstats
import statistics
import time
from pathlib import Path

from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry

root = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/ps6i2-base")
registry = CorpusRegistry(root)


def make():
    db = CloudTraceDatabase("/private/tmp/ps2/store.sqlite", read_only=True)
    return db, CloudTraceRetriever(db, registry=registry)


db, r = make()
with db._transaction():
    rows = db._admitted_facets(r)[0]
    clusters = db._rows("SELECT cluster_id, run_ids FROM run_clusters")
ids = {x["run_id"] for x in rows}
cluster = next(c for c in clusters if ids.intersection(c["run_ids"]))
anchor = rows[0]
excluded = [anchor["task_id"]]
cluster_anchor = sorted(ids.intersection(cluster["run_ids"]))[0]
modes = {
    "lexical": {},
    "facets": {"facets": {"outcome": "pass"}, "exclude_task_ids": excluded},
    "anchor": {"like_run_id": anchor["run_id"], "exclude_task_ids": excluded},
    "cluster": {"cluster_id": cluster["cluster_id"], "exclude_task_ids": excluded},
    "cluster+anchor": {
        "cluster_id": cluster["cluster_id"],
        "like_run_id": cluster_anchor,
        "exclude_task_ids": excluded,
    },
}
out = {}
for name, args in modes.items():
    samples = []
    for _ in range(7):
        db, r = make()
        times = []
        for _ in range(2):
            cpu = time.process_time()
            wall = time.perf_counter()
            r.retrieve_similar_examples("exploit", top_k=3, **args)
            times.append([time.process_time() - cpu, time.perf_counter() - wall])
        samples.append(times)
    profiler = cProfile.Profile(timer=time.process_time)
    profiler.enable()
    r.retrieve_similar_examples("exploit", top_k=3, **args)
    profiler.disable()
    stats = pstats.Stats(profiler)
    entries = sorted(stats.stats.items(), key=lambda x: x[1][3], reverse=True)
    total = next(v[3] for k, v in entries if k[2] == "retrieve_similar_examples")
    hotspots = [
        {
            "function": f"{Path(k[0]).name}:{k[1]}:{k[2]}",
            "cumulative_cpu_s": v[3],
            "share": v[3] / total,
        }
        for k, v in entries
        if k[2] != "retrieve_similar_examples"
    ][:5]
    out[name] = {
        "samples": samples,
        "median": [
            [statistics.median(x[i][j] for x in samples) for j in range(2)] for i in range(2)
        ],
        "profile_cpu_s": total,
        "hotspots": hotspots,
    }
    print(name, out[name]["median"], flush=True)
Path("/private/tmp/ps5/profile_full_i2.json").write_text(json.dumps(out, indent=2))
