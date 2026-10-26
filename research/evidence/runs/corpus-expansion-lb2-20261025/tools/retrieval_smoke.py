import json, os, sqlite3, sys, time
from pathlib import Path

os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
ROOT = Path(sys.argv[1])
DB = "/Users/petermakhnatch/.local/state/evallab-prod-live/cloud-trace-db.sqlite"
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

registry = CorpusRegistry(ROOT)
ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
split_of = dict(ro.execute("SELECT task_id, split FROM corpus_tasks"))
deepswe_ids = {r[0] for r in ro.execute(
    "SELECT task_id FROM corpus_tasks WHERE task_id LIKE 'goodfire-reward-hack-data/deep_swe/%'")}
print("deepswe task rows", len(deepswe_ids), "splits", sorted({split_of[t] for t in deepswe_ids}))
ro.close()

db = CloudTraceDatabase(DB)
retriever = CloudTraceRetriever(db, registry=registry)
queries = ["cache flags module absolute imports", "feature request implement missing option",
           "reward hacking test special casing", "fabricated verification hidden test",
           "upstream fix recall", "enhancement refactor lint pinning"]
report = []
for query in queries:
    started = time.monotonic()
    examples = retriever.retrieve_similar_examples(query, top_k=5)
    elapsed = time.monotonic() - started
    ids = [e.task_id for e in examples]
    report.append({
        "query": query, "returned": len(ids), "seconds": round(elapsed, 2),
        "deep_swe_hits": sum(t in deepswe_ids for t in ids),
        "non_dev_hits": sum(split_of.get(t) != "DEV" for t in ids),
    })
print(json.dumps(report, indent=1))
admitted = retriever._admitted_ids()
print("retriever admitted tasks", len(admitted[0]), "deep_swe admitted", len(deepswe_ids & set(admitted[0])))
