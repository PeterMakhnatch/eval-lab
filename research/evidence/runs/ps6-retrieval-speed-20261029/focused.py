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
if len(sys.argv) > 1:
    from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
    from evallab.production.corpus_queue import CorpusRegistry

    root = Path(sys.argv[1])
    db = CloudTraceDatabase("/private/tmp/ps2/store.sqlite", read_only=True)
    r = CloudTraceRetriever(db, registry=CorpusRegistry(root))
    with db._transaction():
        rows = db._admitted_facets(r)[0]
        clusters = db._rows("SELECT cluster_id, run_ids FROM run_clusters")
    ids = {x["run_id"] for x in rows}
    cluster = next(c for c in clusters if ids.intersection(c["run_ids"]))
    anchor = sorted(ids.intersection(cluster["run_ids"]))[0]
    kw = {
        "cluster_id": cluster["cluster_id"],
        "like_run_id": anchor,
        "exclude_task_ids": [rows[0]["task_id"]],
    }
    times = []
    for _ in range(6):
        start = time.process_time()
        r.retrieve_similar_examples("exploit", top_k=3, **kw)
        times.append(time.process_time() - start)
    print(json.dumps({"first": times[0], "warm": statistics.median(times[1:])}))
    sys.exit()
results = {"base": [], "new": []}
for i in range(7):
    order = (("base", BASE), ("new", ROOT)) if i % 2 == 0 else (("new", ROOT), ("base", BASE))
    for name, root in order:
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
        p = subprocess.run(
            [str(PY), __file__, str(root)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        results[name].append(json.loads(p.stdout.splitlines()[-1]))
        print(i, name, flush=True)
results["median"] = {
    label: {key: statistics.median(x[key] for x in values) for key in ("first", "warm")}
    for label, values in results.items()
}
Path("/private/tmp/ps5/focused_i2.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results["median"], indent=2))
