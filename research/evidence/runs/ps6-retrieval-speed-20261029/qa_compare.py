import json
import os
import subprocess
from pathlib import Path

ROOT = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/ps6i2")
BASE = ROOT.parent / "ps6i2-base"
PY = ROOT / ".venv/bin/python"
CODE = "import sys; from evallab.entry import main; sys.exit(main(sys.argv[1:]))"


def run(tree, args):
    env = {
        **os.environ,
        "PYTHONPATH": str(tree / "src"),
        "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
        "PYTHONWARNINGS": "ignore",
    }
    p = subprocess.run(
        [
            str(PY),
            "-c",
            CODE,
            "qa-data",
            *args,
            "--cloud-database",
            "/private/tmp/ps2/store.sqlite",
        ],
        cwd=tree,
        env=env,
        capture_output=True,
    )
    try:
        value = json.loads(p.stdout)
        value.pop("meta", None)
        out = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    except (ValueError, AttributeError):
        out = p.stdout
    return p.returncode, out


_, out = run(ROOT, ["runs", "--facet", "suspicion=3", "--limit", "6"])
data = json.loads(out)["data"]
items = data.get("runs") or data.get("items") or []
run_id, task = items[0]["run_id"], items[0]["task_id"]
calls = [
    ["traces", "--sort", "alarms", "--limit", "100", "--ids-only"],
    ["traces", "--outcome", "fail", "--limit", "100"],
    ["runs", "--like", run_id, "--limit", "10"],
    ["history", task],
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
    ["trace-card", run_id],
    ["trace-card", "retained-trace:b493cd8d3a512fe3bd44d165bfb0f6a4"],
    ["traces", "--limit", "5", "--dataset", "terminal-bench"],
    ["window", run_id, "--first", "1", "--last", "3"],
    ["findings", "--run", run_id],
    ["split", "task", task],
    ["export", "--out", "/private/tmp/ps5/qa-unused"],
    ["steps", "/private/tmp/ps5/no-manifest", run_id],
    ["scan", "/private/tmp/ps5/no-transcripts"],
]
results = []
for args in calls:
    a, b = run(BASE, args), run(ROOT, args)
    results.append({"call": args[0], "same": a == b, "exits": [a[0], b[0]]})
    print(len(results), results[-1], flush=True)
Path("/private/tmp/ps5/qa_all_i2.json").write_text(json.dumps(results, indent=2))
assert all(x["same"] for x in results)
