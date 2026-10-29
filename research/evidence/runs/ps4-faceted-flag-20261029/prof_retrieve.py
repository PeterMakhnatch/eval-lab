"""pS-SELF-4: where does one Atlas retrieve_similar_examples call spend CPU on the store copy? (read-only)

usage: prof_retrieve.py REPO STORE [--profile]
Warm-opens once, then times N calls per mode (CPU s, in-process), optionally cProfile of the cluster+anchor mode.
"""

import cProfile
import hashlib
import json
import pstats
import statistics
import sys
import time
from pathlib import Path

from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry

REPO, STORE = Path(sys.argv[1]), Path(sys.argv[2])
db = CloudTraceDatabase(STORE, read_only=True)
retriever = CloudTraceRetriever(db, registry=CorpusRegistry(REPO))
clusters = db._rows("SELECT cluster_id, run_ids FROM run_clusters WHERE level = 2 ORDER BY cluster_id")
run_task = {r["trace_id"]: r["task_id"] for r in db._rows("SELECT trace_id, task_id FROM agent_traces")}
facet_runs = {r["run_id"] for r in db._rows("SELECT run_id FROM run_facets")}
c = next(c for c in clusters if len([r for r in c["run_ids"] if r in facet_runs]) > 3)
anchor = next(r for r in c["run_ids"] if r in facet_runs)
other = "format-code-task-999999"
MODES = {
    "lexical": ("checksum grader tests", {}),
    "facets_any": ("grader tests", {"facets": {}}),
    "cluster": ("", {"cluster_id": c["cluster_id"]}),
    "anchor": ("", {"like_run_id": anchor}),
    "cluster_anchor_query": ("tests grader", {"cluster_id": c["cluster_id"], "like_run_id": anchor}),
}


def call(mode):
    query, kwargs = MODES[mode]
    found = retriever.retrieve_similar_examples(query, top_k=5, exclude_task_ids=(other,), **kwargs)
    return [(e.task_id, e.trace_id, e.issue_id) for e in found]


result = {}
for mode in MODES:
    call(mode)  # warm page cache and memo
    samples, answers = [], None
    for _ in range(5):
        t = time.process_time()
        answers = call(mode)
        samples.append(time.process_time() - t)
    result[mode] = {
        "cpu_median_s": round(statistics.median(samples), 3), "n": len(answers),
        "answers_sha256": hashlib.sha256(json.dumps(answers).encode()).hexdigest()[:16],
    }
print(json.dumps(result, indent=1))
if "--profile" in sys.argv:
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(5):
        call("cluster_anchor_query")
        call("facets_any")
    profiler.disable()
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(22)
