"""Interleaved old(origin/main) vs new real-CLI comparison: byte identity of `data` and CPU times.

usage: cli_compare.py <new-root> <old-root> <out.json> <reps>
"""
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

NEW, OLD, OUT, REPS = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
PY = NEW / ".venv/bin/python"
STORE = "/private/tmp/ps2/store.sqlite"
CODE = "import sys; from evallab.entry import main; sys.exit(main(sys.argv[1:]))"


def run(root: Path, args: list[str]):
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
           "PYTHONWARNINGS": "ignore"}
    proc = subprocess.Popen(
        [str(PY), "-c", CODE, "qa-data", *args, "--cloud-database", STORE],
        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = proc.communicate()
    _, status, usage = os.wait4(proc.pid, 0) if False else (None, proc.returncode, None)
    return proc.returncode, out, err


def run_timed(root: Path, args: list[str]):
    import resource
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
           "PYTHONWARNINGS": "ignore"}
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = subprocess.run(
        [str(PY), "-c", CODE, "qa-data", *args, "--cloud-database", STORE],
        cwd=root, env=env, capture_output=True,
    )
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return proc.returncode, proc.stdout, proc.stderr, cpu


def data(out: bytes):
    doc = json.loads(out)
    doc.pop("meta", None)
    return doc


# discover ids
_, out, _ = run(NEW, ["runs", "--facet", "suspicion=3", "--limit", "6"])
runs = json.loads(out)["data"]
print(list(runs)[:10] if isinstance(runs, dict) else type(runs))
items = runs.get("runs") or runs.get("items") or []
run_ids = [r["run_id"] for r in items]
task_ids = list(dict.fromkeys(r["task_id"] for r in items))
print(run_ids[:3], task_ids[:3])
sealed_run = "retained-trace:b493cd8d3a512fe3bd44d165bfb0f6a4"
calls = [
    ["traces", "--sort", "alarms", "--limit", "100", "--ids-only"],
    ["traces", "--outcome", "fail", "--limit", "100"],
    ["runs", "--like", run_ids[0], "--limit", "10"],
    ["history", task_ids[0]] if task_ids else ["inventory"],
    ["traces", "--limit", "200"],
    ["traces", "--limit", "50", "--budget", "8000"],
    ["traces", "--task", "cad-model"],
    ["task", "terminal-bench/cad-model", "--budget", "8000"],
    ["task", "terminal-bench/layout-config-recreation2", "--budget", "8000"],
    ["clusters"],
    ["runs", "--facet", "answer_material_accessed=true"],
    ["runs", "--limit", "20"],
    ["runs", "--facet", "suspicion=3"],
    ["recount"],
    ["inventory"],
    ["trace-card", run_ids[0]],
    ["trace-card", sealed_run],
    ["traces", "--limit", "5", "--dataset", "terminal-bench"],
]
result = {"identical": {}, "cpu": {}}
for call in calls:
    key = " ".join(call)
    codes = {}
    old_out = new_out = None
    for _ in range(1):
        c_old, o_old, e_old, _t = run_timed(OLD, call)
        c_new, o_new, e_new, _t = run_timed(NEW, call)
    try:
        same = (c_old, c_new) and c_old == c_new and data(o_old) == data(o_new)
    except Exception:
        same = c_old == c_new and o_old == o_new
    result["identical"][key] = {"same_data": bool(same), "exit": [c_old, c_new],
                                "bytes": [len(o_old), len(o_new)]}
    print(key, result["identical"][key], flush=True)

timed = [["traces", "--limit", "200"], ["task", "terminal-bench/cad-model", "--budget", "8000"],
         ["clusters"], ["runs", "--facet", "answer_material_accessed=true"], ["recount"]]
for call in timed:
    key = " ".join(call)
    old, new = [], []
    for _ in range(REPS):
        old.append(run_timed(OLD, call)[3])
        new.append(run_timed(NEW, call)[3])
    result["cpu"][key] = {
        "n": REPS, "old_median_s": statistics.median(old), "new_median_s": statistics.median(new),
        "old": old, "new": new,
    }
    print(key, result["cpu"][key]["old_median_s"], result["cpu"][key]["new_median_s"], flush=True)
OUT.write_text(json.dumps(result, indent=2))
