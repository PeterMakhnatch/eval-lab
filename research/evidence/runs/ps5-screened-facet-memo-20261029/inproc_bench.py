"""Interleaved old vs new, one fresh process each: first call vs repeat calls in the same process.

usage: inproc_bench.py <new-root> <old-root> <out.json> <n>
child mode: inproc_bench.py --child <root>
"""
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1] == "--child":
    from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
    from evallab.production.corpus_queue import CorpusRegistry

    root = Path(sys.argv[2])
    db = CloudTraceDatabase("/private/tmp/ps2/store.sqlite", read_only=True)
    ret = CloudTraceRetriever(db, registry=CorpusRegistry(root))
    with db._transaction():
        rows = db._admitted_facets(ret)[0]
    tasks = sorted({r["task_id"] for r in rows})

    def cpu(fn):
        t = time.process_time()
        fn()
        return time.process_time() - t

    def screened():
        with db._transaction():
            db._admitted_facets(ret)
            db._admitted_facets(ret, [("answer_material_accessed", [True])])

    def atlas():
        ret.retrieve_similar_examples(
            "exploit", top_k=3, facets={"outcome": "pass"}, exclude_task_ids=[tasks[0]],
        )

    out = {}
    for name, fn in (("screened_2_reads", screened), ("atlas_retrieve", atlas)):
        times = [cpu(fn) for _ in range(6)]
        out[name] = {"first": times[0], "repeat_median": statistics.median(times[1:])}
    print(json.dumps(out))
    sys.exit(0)

NEW, OLD, OUT, N = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
PY = NEW / ".venv/bin/python"
result: dict = {"n": N, "old": [], "new": []}
for _ in range(N):
    for label, root in (("old", OLD), ("new", NEW)):
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
               "PYTHONWARNINGS": "ignore"}
        proc = subprocess.run([str(PY), __file__, "--child", str(root)], cwd=root, env=env,
                              capture_output=True, text=True, check=True)
        result[label].append(json.loads(proc.stdout.strip().splitlines()[-1]))
summary = {}
for name in ("screened_2_reads", "atlas_retrieve"):
    for key in ("first", "repeat_median"):
        for label in ("old", "new"):
            summary[f"{name}.{key}.{label}_median_s"] = statistics.median(r[name][key] for r in result[label])
result["summary"] = summary
OUT.write_text(json.dumps(result, indent=2))
print(json.dumps(summary, indent=2))
