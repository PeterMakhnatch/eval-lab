"""NP38-CE-1 independent check of the SWE-bench side of the join: the files in each run's submitted diff against the listed test files.

Does not use the step parser, so it cannot share a parsing error with ``np38_join.py``. Counts only.
usage: check_submission.py DB_COPY
"""

import collections
import glob
import json
import re
import sqlite3
import sys
from pathlib import Path

STATE = Path.home() / ".local/state"
CAS = STATE / "evallab-prod-live/cas/sha256"
PAIRS = STATE / "evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"
HF = glob.glob(str(Path.home() / ".cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Verified/snapshots/*/data/*.parquet"))[0]

import pyarrow.parquet as pq  # noqa: E402

pairs = [json.loads(line) for line in PAIRS.read_text().splitlines() if line.strip()]
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro&immutable=1", uri=True)
uri = dict(db.execute("SELECT trace_id, raw_cas_uri FROM agent_traces"))
by_id = {row["instance_id"]: row for row in pq.read_table(HF).to_pylist()}
DIFF = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
TESTLIKE = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*$|_tests?\.py$")
counts: collections.Counter[str] = collections.Counter()
for pair in pairs:
    if "/swebench_verified/" not in pair["task"]:
        continue
    listed = {b for _a, b in DIFF.findall(by_id[pair["task"].split("/")[-1].split("@")[0]]["test_patch"])}
    for cls in ("enacted", "clean"):
        trace = json.loads((CAS / uri[pair[cls]].rsplit("/", 1)[-1]).read_text())["traces"][0]
        submission = trace.get("info", {}).get("swebench", {}).get("submission") or ""
        files = {b for _a, b in DIFF.findall(submission)}
        counts[f"{cls}_runs"] += 1
        counts[f"{cls}_submission_present"] += bool(submission)
        counts[f"{cls}_submission_touches_a_listed_file"] += bool(files & listed)
        counts[f"{cls}_submission_touches_any_test_like_file"] += any(TESTLIKE.search(f) for f in files)
print(json.dumps(dict(counts), sort_keys=True))
