import json, os, sqlite3, sys, time
from pathlib import Path

os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
ROOT = Path(sys.argv[1])
DB = "/Users/petermakhnatch/.local/state/evallab-prod-live/cloud-trace-db.sqlite"
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
split_of = dict(ro.execute("SELECT task_id, split FROM corpus_tasks"))
ro.close()
registry = CorpusRegistry(ROOT)
retriever = CloudTraceRetriever(CloudTraceDatabase(DB), registry=registry)
rows = []
for label, query, kind in [
    ("existing_goodfire_swe_task", "astropy astropy 13236", None),
    ("existing_goodfire_swe_task_reward_seeker", "astropy astropy 13236", "reward_seeker"),
    ("deepswe_task_name", "abs module cache flags", None),
    ("deepswe_task_name_reward_seeker", "abs module cache flags", "reward_seeker"),
]:
    started = time.monotonic()
    examples = retriever.retrieve_similar_examples(query, kind=kind, top_k=3)
    rows.append({
        "case": label, "returned": len(examples), "seconds": round(time.monotonic() - started, 1),
        "splits": sorted({split_of.get(e.task_id) for e in examples}),
        "goodfire_hits": sum(e.task_id.startswith("goodfire-reward-hack-data/") for e in examples),
    })
print(json.dumps(rows, indent=1))
