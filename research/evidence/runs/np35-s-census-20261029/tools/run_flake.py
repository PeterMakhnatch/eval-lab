"""NP35-LB-1 reference: Flake Laundering. (a) NP29's own 69-run sample from its pickle (reproduces NP29's S counts);
(b) every run in the staged bundle through its adapter. Aggregates only."""

import hashlib
import json
import os
import pickle
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "/private/tmp/np35")
import s_shim  # noqa: E402

origin = subprocess.run(
    [
        "git",
        "-C",
        os.path.expanduser("~/Developer/eval-lab"),
        "show",
        "origin/main:research/evidence/runs/np29-test-edits-20261029/tools/test_edits.py",
    ],
    capture_output=True,
    check=True,
).stdout
assert hashlib.sha256(origin).hexdigest() == s_shim.TEST_EDITS_SHA256, (
    "NP29 test_edits.py differs from origin/main"
)

out = {"test_edits_sha256": s_shim.TEST_EDITS_SHA256}

# (a) NP29 sample, exactly as NP29 loaded it
state = Path.home() / ".local/state/evallab-np21-cm1"
runs = [r for r in pickle.loads((state / "runs.pkl").read_bytes()) if r["set"] == "flake"]
reasons = json.loads((state / "reasons.json").read_text())
t = s_shim.Tally()
for r in runs:
    t.add(f"np29_sample/{r['stratum']}", r["task"], r["steps"])
    t.add(f"np29_sample_by_reason/{reasons.get(r['key'], 'unknown')}", r["task"], r["steps"])
out["np29_sample"] = t.report()

# (b) whole staged bundle through the adapter
ad = Path(
    "/private/tmp/np35/research/evidence/runs/corpus-expansion-pq3-20261028/corpora/zaidwhy__flake-laundering/tools"
)
sys.path.insert(0, str(ad))
import adapter  # noqa: E402

t2 = s_shim.Tally()
for s in adapter.specs():
    g = s["gold"]
    t2.add(
        f"bundle/{g['state']}/{g['reason']}",
        s_shim.task_hash(s["identity"]["task_key"]),
        s["_steps"],
    )
    t2.add(f"bundle_class/{g['state']}", s_shim.task_hash(s["identity"]["task_key"]), s["_steps"])
out["bundle"] = t2.report()
print(json.dumps(out, indent=1))
