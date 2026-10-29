"""CLI CPU time, old vs new, strictly alternating which side goes first (removes order bias).

usage: cli_time.py <new-root> <old-root> <out.json> <n>
"""
import json
import os
import resource
import statistics
import subprocess
import sys
from pathlib import Path

NEW, OLD, OUT, N = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
PY = NEW / ".venv/bin/python"
CODE = "import sys; from evallab.entry import main; sys.exit(main(sys.argv[1:]))"


def cpu_of(root: Path, args: list[str]) -> float:
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
           "PYTHONWARNINGS": "ignore"}
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    subprocess.run([str(PY), "-c", CODE, "qa-data", *args, "--cloud-database",
                    "/private/tmp/ps2/store.sqlite"], cwd=root, env=env, capture_output=True, check=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)


calls = [["traces", "--limit", "200"], ["task", "terminal-bench/cad-model", "--budget", "8000"],
         ["clusters"], ["runs", "--facet", "answer_material_accessed=true"]]
result = {}
for call in calls:
    old, new = [], []
    for i in range(N):
        order = (("old", OLD, old), ("new", NEW, new))
        for label, root, bucket in (order if i % 2 == 0 else order[::-1]):
            bucket.append(cpu_of(root, call))
    key = " ".join(call)
    result[key] = {"n": N, "old_median_s": statistics.median(old), "new_median_s": statistics.median(new),
                   "old": old, "new": new}
    print(key, result[key]["old_median_s"], result[key]["new_median_s"], flush=True)
OUT.write_text(json.dumps(result, indent=2))
