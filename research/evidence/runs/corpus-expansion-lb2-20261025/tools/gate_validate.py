"""Validate that the production admission gate reproduces the existing Goodfire DEV/held partition.

Prints aggregates only: never a sealed identity, task name, or answer-key content.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
ROOT = Path(sys.argv[1])
DB = "/Users/petermakhnatch/.local/state/evallab-prod-live/cloud-trace-db.sqlite"

from evallab.production.cloud_trace_db import admitted_cloud_task_ids  # noqa: E402
from evallab.production.corpus_queue import CorpusRegistry  # noqa: E402

registry = CorpusRegistry(ROOT)
assert registry.has_assignment_authority()
connection = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
rows = connection.execute(
    "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
    " json_extract(metadata_json, '$.item_id') AS item_id,"
    " CASE WHEN json_valid(metadata_json) THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
    " FROM corpus_tasks WHERE dataset_id = 'goodfire-reward-hack-data'"
).fetchall()
actual_dev = {row["task_id"] for row in rows if row["split"] == "DEV"}
actual_held = {row["task_id"] for row in rows if row["split"] != "DEV"}
candidates = [{**dict(row), "split": "DEV"} for row in rows]
admitted = admitted_cloud_task_ids(candidates, registry=registry)
print(json.dumps({
    "goodfire_rows": len(rows),
    "actual_dev": len(actual_dev),
    "actual_held": len(actual_held),
    "gate_admits_when_all_candidates_DEV": len(admitted),
    "gate_denies": len(rows) - len(admitted),
    "admitted_equals_actual_dev": admitted == actual_dev,
    "dev_not_admitted": len(actual_dev - admitted),
    "held_admitted": len(actual_held & admitted),
    "registry_assignment_digest": registry.split_assignments_digest,
}, sort_keys=True))
