"""NP22 follow-up: do real CAS URIs in the rendered provenance line carry a dataset name or path?"""
import json
import re
from pathlib import Path

from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry

REPO = Path.cwd()
cohort = json.loads((REPO / "research/experiments/sb1-clean-seeker-set-20261027/cohort.json").read_text())
targets = [task["task_id"] for task in cohort["tasks"]]
db = CloudTraceDatabase(Path("/private/tmp/ps2/store.sqlite"), read_only=True)
retriever = CloudTraceRetriever(db, registry=CorpusRegistry(REPO))
shapes: dict[str, int] = {}
leaks = 0
examples = 0
for target in targets:
    for example in retriever.retrieve_similar_examples(
        "weak_check", facets={"outcome": "pass", "grader_or_harness_touched": True}, exclude_task_ids=(target,),
    ):
        examples += 1
        text = "\n".join(example.prompt_lines(1))
        for key in ("task_cas_uri", "issue_cas_uri", "raw_cas_uri"):
            value = example.provenance.get(key)
            shapes[f"{key}:{re.sub(r'[0-9a-f]{16,}', '<hex>', str(value))}"] = shapes.get(
                f"{key}:{re.sub(r'[0-9a-f]{16,}', '<hex>', str(value))}", 0) + 1
        for value in (example.provenance.get("observation_cas_uris") or []):
            shape = "observation_cas_uris:" + re.sub(r"[0-9a-f]{16,}", "<hex>", str(value))
            shapes[shape] = shapes.get(shape, 0) + 1
        for withheld in (example.dataset_id, example.provenance.get("split")):
            if withheld and any(withheld in str(example.provenance.get(k)) for k in (
                "task_cas_uri", "issue_cas_uri", "raw_cas_uri", "observation_cas_uris",
            )):
                leaks += 1
        del text
print(json.dumps({"examples": examples, "uri_shapes": shapes, "cas_uri_containing_dataset_or_split": leaks}, indent=1))
