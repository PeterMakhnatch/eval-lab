"""pS-SELF-3: why does cluster + anchor retrieval return nothing on the real store? (read-only)

Opens the store copy read-only. For a sample of anchor runs it varies one thing at a time:
cluster only, anchor only, cluster + anchor, with/without the query terms, with/without a facet filter.
"""

import collections
import sys
import time
from pathlib import Path

from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry

REPO = Path(sys.argv[1])
STORE = Path(sys.argv[2])
db = CloudTraceDatabase(STORE, read_only=True)
registry = CorpusRegistry(REPO)
retriever = CloudTraceRetriever(db, registry=registry)
with db._transaction():
    admitted = set(retriever._admitted_ids()[0])
clusters = db._rows("SELECT cluster_id, level, run_ids, size FROM run_clusters ORDER BY level, cluster_id")
run_task = {r["trace_id"]: r["task_id"] for r in db._rows("SELECT trace_id, task_id FROM agent_traces")}
facet_runs = {r["run_id"] for r in db._rows("SELECT run_id FROM run_facets")}
adm_facets = {r for r in facet_runs if run_task.get(r) in admitted}
print("admitted tasks", len(admitted), "faceted runs", len(facet_runs), "of which admitted", len(adm_facets))
per_level = collections.defaultdict(lambda: [0, 0])
for c in clusters:
    ids = set(c["run_ids"])
    per_level[c["level"]][0] += len(ids)
    per_level[c["level"]][1] += len(ids & adm_facets)
print("cluster members (total, admitted+faceted) per level", dict(per_level))

target = None
results = collections.Counter()
samples = {}
t0 = time.time()
level2 = [c for c in clusters if c["level"] == 2][:8]
for c in level2:
    members = [r for r in c["run_ids"] if r in adm_facets]
    if len(members) < 2:
        continue
    anchor = members[0]
    # the audit excluded a task that does not own the anchor; use one unrelated to this cluster
    other = next(t for t in sorted(admitted) if t not in {run_task[m] for m in c["run_ids"] if m in run_task})
    for label, kwargs, query in (
        ("cluster_only_noquery", {"cluster_id": c["cluster_id"]}, ""),
        ("anchor_only_noquery", {"like_run_id": anchor}, ""),
        ("cluster+anchor_noquery", {"cluster_id": c["cluster_id"], "like_run_id": anchor}, ""),
        ("cluster+anchor_query", {"cluster_id": c["cluster_id"], "like_run_id": anchor}, "test tests grader"),
        ("cluster+anchor+facets{}", {"cluster_id": c["cluster_id"], "like_run_id": anchor, "facets": {}}, ""),
    ):
        try:
            found = retriever.retrieve_similar_examples(query, top_k=5, exclude_task_ids=(other,), **kwargs)
            results[label, "ok", bool(found)] += 1
            samples.setdefault(label, len(found))
        except Exception as exc:  # noqa: BLE001 - recorded
            results[label, type(exc).__name__ + ":" + str(exc)[:60], False] += 1
print({" | ".join(map(str, k)): v for k, v in sorted(results.items())})
print("elapsed", round(time.time() - t0, 1))
