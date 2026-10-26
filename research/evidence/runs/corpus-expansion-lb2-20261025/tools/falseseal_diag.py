import json, os, re, sqlite3, sys
from pathlib import Path

os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
from evallab.production.cloud_trace_db import CloudTraceRetriever  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

registry = CorpusRegistry(Path(sys.argv[1]))
sealed = {value.casefold() for value in registry.sealed_identifiers() if value}
bare_numeric = {value for value in sealed if re.fullmatch(r"\d{1,6}", value)}
ro = sqlite3.connect("file:/Users/petermakhnatch/.local/state/evallab-prod-live/cloud-trace-db.sqlite?mode=ro", uri=True)
rows = ro.execute("SELECT task_id, split, dataset_id FROM corpus_tasks WHERE split = 'DEV'").fetchall()
pattern = CloudTraceRetriever._sealed_text
def hit(text):
    return CloudTraceRetriever._sealed_text(text, set(sealed))
numeric_only = set(bare_numeric)
def hit_numeric(text):
    return CloudTraceRetriever._sealed_text(text, numeric_only)
by_dataset = {}
for task_id, split, dataset_id in rows:
    entry = by_dataset.setdefault(dataset_id, {"dev_tasks": 0, "task_id_hits_any_seal": 0, "task_id_hits_only_bare_numeric_seals": 0})
    entry["dev_tasks"] += 1
    any_hit = hit(task_id.casefold())
    numeric_hit = hit_numeric(task_id.casefold())
    entry["task_id_hits_any_seal"] += any_hit
    entry["task_id_hits_only_bare_numeric_seals"] += numeric_hit
print(json.dumps({"sealed_text_identities": len(sealed), "bare_numeric_seals": len(bare_numeric),
                  "by_dataset": by_dataset}, indent=1, sort_keys=True))
