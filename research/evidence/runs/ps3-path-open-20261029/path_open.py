"""pS-SELF-3: cost and answers of the seeker's path-mode CloudTraceRetriever on a store copy.

usage: path_open.py STORE OUT.json LABEL
Opens the store through ``CloudTraceRetriever(path)`` (read-only source, private DEV projection),
times the open (wall, CPU), measures the snapshot size, then answers a fixed query set and writes
the hits and receipts (minus the snapshot path/digest, which legitimately differ) for a byte comparison.
No model call, no write to the source.
"""

import dataclasses
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

from evallab.production import cloud_trace_retriever as module

STORE, OUT, LABEL = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
QUERIES = [
    "weak check", "tamperable grader", "answer reachable", "reference validated weak test",
    "grader logic bug", "grader wrong pass", "format code task", "test", "tests", "timeout",
    "reward", "exploit", "pytest", "assert", "cat", "python", "git", "bash",
    "format code task 000079", "format code task 000232", "aiohttp", "hono", "django", "sympy",
    "zzzzunmatchedterm", "weak_check", "Weak Check", "grader", "verifier", "patch",
]
SAME_TASKS = ["format-code-task-000079", "format-code-task-000232", "format-code-task-000085"]


def jsonable(value):
    if dataclasses.is_dataclass(value):
        return {k: jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


source_before = (STORE.stat().st_size, STORE.stat().st_mtime_ns)
cpu0, wall0 = time.process_time(), time.perf_counter()
retriever = module.CloudTraceRetriever(STORE)
open_cpu, open_wall = time.process_time() - cpu0, time.perf_counter() - wall0
snapshot = Path(retriever._db.database_path) if hasattr(retriever._db, "database_path") else None
tempdir = Path(retriever._tempdir.name) if retriever._tempdir else None
snapshot_bytes = sum(p.stat().st_size for p in tempdir.glob("*")) if tempdir else None
answers = {}
cpu1 = time.process_time()
for task in ["format-code-task-999999", *SAME_TASKS]:
    for query in QUERIES:
        try:
            result = retriever.retrieve(task, query)
            payload = jsonable(result)
            receipt = dict(payload["receipt"])
            for key in ("source_path", "source_digest", "snapshot_digest"):
                receipt.pop(key, None)
            payload["receipt"] = receipt
            answers[f"{task}|{query}"] = payload
        except Exception as exc:  # noqa: BLE001 - recorded
            answers[f"{task}|{query}"] = {"error": type(exc).__name__ + ": " + str(exc)[:100]}
query_cpu = time.process_time() - cpu1
digest = retriever.source_digest
retriever.close()
source_after = (STORE.stat().st_size, STORE.stat().st_mtime_ns)
blob = json.dumps(answers, sort_keys=True, separators=(",", ":"))
summary = {
    "label": LABEL, "open_wall_s": round(open_wall, 2), "open_cpu_s": round(open_cpu, 2),
    "snapshot_bytes": snapshot_bytes, "query_cpu_s": round(query_cpu, 2),
    "maxrss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1),
    "source_unchanged": source_before == source_after, "snapshot_digest": digest,
    "queries": len(answers), "with_hits": sum(1 for a in answers.values() if a.get("hits")),
    "total_hits": sum(len(a.get("hits", [])) for a in answers.values()),
    "errors": sorted({a["error"] for a in answers.values() if "error" in a}),
    "answers_sha256": hashlib.sha256(blob.encode()).hexdigest(),
}
OUT.write_text(json.dumps({"summary": summary, "answers": answers}, indent=1, sort_keys=True))
print(json.dumps(summary, indent=1))
