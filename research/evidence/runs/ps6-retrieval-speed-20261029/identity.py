import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/ps6i2")
BASE = ROOT.parent / "ps6i2-base"
PY = ROOT / ".venv/bin/python"
if len(sys.argv) > 1:
    from evallab.production.cloud_trace_db import (
        CloudTraceDatabase,
        CloudTraceRetriever,
        _sealed_pattern,
    )
    from evallab.production.corpus_queue import CorpusRegistry

    root = Path(sys.argv[1])
    db = CloudTraceDatabase("/private/tmp/ps2/store.sqlite", read_only=True)
    ret = CloudTraceRetriever(db, registry=CorpusRegistry(root))
    with db._transaction():
        admitted, seals = ret._admitted_ids()
        served = db._admitted_facets(ret)[0]
        served_ids = {r["run_id"] for r in served}
        p = []
        scope = db._task_id_scope("a.task_id", admitted, p)
        traces = db._rows(
            "SELECT a.trace_id, a.task_id, f.run_id FROM agent_traces a LEFT JOIN run_facets f ON f.run_id=a.trace_id WHERE "
            + scope
            + " ORDER BY a.trace_id",
            p,
        )
        clusters = db._rows("SELECT cluster_id, run_ids FROM run_clusters")
    withheld = [x for x in traces if x["run_id"] is not None and x["trace_id"] not in served_ids]
    missing = [x for x in traces if x["run_id"] is None]
    assert withheld and len(missing) >= 5
    cluster = next(c for c in clusters if served_ids.intersection(c["run_ids"]))
    anchor = next(iter(sorted(served_ids.intersection(cluster["run_ids"]))))
    pattern = _sealed_pattern(frozenset(seals))
    valid = []
    seen = set()
    for x in served:
        key = (x["goal"][:50], x["task_id"])
        if key not in seen and key[0] and not ret._sealed_text(key[0], pattern):
            seen.add(key)
            valid.append(x)
        if len(valid) == 10:
            break
    assert len(valid) == 10
    answers = []
    for i, goal in enumerate(valid):
        q = goal["goal"][:50]
        exclude = [goal["task_id"]]
        candidate = (withheld * 5 + missing[:5])[i]
        options = [
            {},
            {"facets": {"outcome": "pass"}, "exclude_task_ids": exclude},
            {"cluster_id": cluster["cluster_id"], "exclude_task_ids": exclude},
            {"like_run_id": candidate["trace_id"], "exclude_task_ids": exclude},
            {
                "cluster_id": cluster["cluster_id"],
                "like_run_id": anchor,
                "exclude_task_ids": exclude,
            },
        ]
        for opts in options:
            try:
                value = [x.to_dict() for x in ret.retrieve_similar_examples(q, top_k=3, **opts)]
            except ValueError as e:
                value = {"error": str(e)}
            answers.append(value)
    payload = json.dumps(
        answers, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    Path("/private/tmp/ps5/identity-" + root.name + ".json").write_bytes(payload)
    print(
        json.dumps(
            {
                "queries": len(answers),
                "withheld_available": len(withheld),
                "missing_available": len(missing),
                "withheld_anchor_queries": 5,
                "missing_anchor_queries": 5,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    )
    sys.exit()
for root in (BASE, ROOT):
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "EVALLAB_DISABLE_LOCAL_DOCKER": "1"}
    subprocess.run([str(PY), __file__, str(root)], cwd=root, env=env, check=True)
assert (
    Path("/private/tmp/ps5/identity-" + BASE.name + ".json").read_bytes()
    == Path("/private/tmp/ps5/identity-" + ROOT.name + ".json").read_bytes()
)
print("byte identical")
