"""In-process repeated screened-facet reads + Atlas retrieval; prints a JSON result.

usage: measure.py <checkout-root> <out.json>   (run with PYTHONPATH=<root>/src, cwd=<root>)
"""
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

from evallab.production import cloud_trace_db as m
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry

root, out_path = Path(sys.argv[1]), Path(sys.argv[2])
assert Path(m.__file__).resolve().is_relative_to(root.resolve()), m.__file__
db = CloudTraceDatabase("/private/tmp/ps2/store.sqlite", read_only=True)
ret = CloudTraceRetriever(db, registry=CorpusRegistry(root))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def facets(filters=(), allowed=None, limit=None):
    with db._transaction():
        rows, sealed = db._admitted_facets(ret, filters, allowed_task_ids=allowed, limit=limit)
    return rows, sealed


def cpu(fn):
    t = time.process_time()
    fn()
    return time.process_time() - t


result: dict = {}
# identity: screened read under several filter / scope / limit shapes
filters_cases = [
    [], [("answer_material_accessed", [True])], [("outcome", ["pass"])],
    [("suspicion", [0, 1, 2])], [("outcome", ["pass", "fail"]), ("tests", ["modified"])],
]
rows0, sealed0 = facets()
allowed_cases = [None, sorted({r["task_id"] for r in rows0})[:5], [], ["terminal-bench/cad-model"]]
shapes = []
for flt in filters_cases:
    for allowed in allowed_cases:
        for limit in (None, 7):
            try:
                rows, sealed = facets(flt, allowed, limit)
            except ValueError as exc:
                shapes.append(["error", str(exc)])
                continue
            shapes.append([len(rows), digest(rows), digest(sorted(sealed))])
result["facet_shapes"] = len(shapes)
result["facet_digest"] = digest(shapes)

# repeated screened read: first (cold) vs memoised repeats
cold = cpu(facets)
repeats = [cpu(facets) for _ in range(9)]
result["admitted_facets_first_cpu_s"] = cold
result["admitted_facets_repeat_median_cpu_s"] = statistics.median(repeats)

# Atlas retrieval, >= 30 queries
runs = [r["run_id"] for r in rows0[:6]]
tasks = [r["task_id"] for r in rows0]
queries = []
terms = ["exploit", "grader", "test", "reward", "hardcode", "cheat", "verifier", "tests pass"]
for i, term in enumerate(terms):
    t_excl = [tasks[i % len(tasks)]]
    queries.append(dict(query=term, exclude_task_ids=t_excl))
    queries.append(dict(query=term, exclude_task_ids=t_excl, facets={"outcome": ["pass"]}))
    queries.append(dict(query=term, exclude_task_ids=t_excl, facets={"answer_material_accessed": True}, top_k=5))
    queries.append(dict(query="", exclude_task_ids=t_excl, like_run_id=runs[i % len(runs)]))
    queries.append(dict(query=term, exclude_task_ids=t_excl, like_run_id=runs[(i + 1) % len(runs)], top_k=5,
                        include_provisional=True))
answers = []
for q in queries:
    try:
        res = ret.retrieve_similar_examples(**q)
        answers.append(digest([r.__dict__ if hasattr(r, "__dict__") else r for r in res]))
    except ValueError as exc:
        answers.append("error:" + str(exc))
result["retrieval_queries"] = len(queries)
result["retrieval_digest"] = digest(answers)
result["retrieval_errors"] = sum(a.startswith("error:") for a in answers)
# timing: the same Atlas call repeated
q = dict(query="exploit", exclude_task_ids=[tasks[0]], facets={"outcome": ["pass"]})
cold_q = cpu(lambda: ret.retrieve_similar_examples(**q))
rep = [cpu(lambda: ret.retrieve_similar_examples(**q)) for _ in range(9)]
result["retrieve_first_cpu_s"] = cold_q
result["retrieve_repeat_median_cpu_s"] = statistics.median(rep)
out_path.write_text(json.dumps(result, indent=2, sort_keys=True))
print(json.dumps(result, indent=2, sort_keys=True))
