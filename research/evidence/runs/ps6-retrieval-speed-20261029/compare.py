import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/ps6i2")
BASE = ROOT.parent / "ps6i2-base"
PY = ROOT / ".venv/bin/python"
if len(sys.argv) > 1 and sys.argv[1] == "child":
    from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
    from evallab.production.corpus_queue import CorpusRegistry

    root = Path(sys.argv[2])
    db = CloudTraceDatabase("/private/tmp/ps2/store.sqlite", read_only=True)
    r = CloudTraceRetriever(db, registry=CorpusRegistry(root))
    with db._transaction():
        rows = db._admitted_facets(r)[0]
        clusters = db._rows("SELECT cluster_id, run_ids FROM run_clusters")
    ids = {x["run_id"] for x in rows}
    cluster = next(c for c in clusters if ids.intersection(c["run_ids"]))
    a = rows[0]
    exclude = [a["task_id"]]
    cluster_anchor = next(iter(sorted(ids.intersection(cluster["run_ids"]))))
    modes = {
        "lexical": {},
        "facets": {"facets": {"outcome": "pass"}, "exclude_task_ids": exclude},
        "anchor": {"like_run_id": a["run_id"], "exclude_task_ids": exclude},
        "cluster": {"cluster_id": cluster["cluster_id"], "exclude_task_ids": exclude},
        "cluster+anchor": {
            "cluster_id": cluster["cluster_id"],
            "like_run_id": cluster_anchor,
            "exclude_task_ids": exclude,
        },
    }
    out = {}
    for name, kw in modes.items():
        times = []
        for _ in range(2):
            cpu = time.process_time()
            wall = time.perf_counter()
            r.retrieve_similar_examples("exploit", top_k=3, **kw)
            times.append([time.process_time() - cpu, time.perf_counter() - wall])
        out[name] = times
    print(json.dumps(out))
    sys.exit()
results = {"base": [], "new": []}
for i in range(7):
    for name, root in (
        (("base", BASE), ("new", ROOT)) if i % 2 == 0 else (("new", ROOT), ("base", BASE))
    ):
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
        run = subprocess.run(
            [str(PY), __file__, "child", str(root)],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        results[name].append(json.loads(run.stdout.splitlines()[-1]))
        print(i, name, flush=True)
summary = {}
for mode in results["base"][0]:
    summary[mode] = {}
    for label in results:
        summary[mode][label] = [
            [statistics.median(x[mode][i][j] for x in results[label]) for j in range(2)]
            for i in range(2)
        ]
results["summary"] = summary
Path("/private/tmp/ps5/compare_i2.json").write_text(json.dumps(results, indent=2))
print(json.dumps(summary, indent=2), flush=True)
