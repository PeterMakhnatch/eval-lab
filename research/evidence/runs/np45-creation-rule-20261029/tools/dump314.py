import importlib.util
import json
import pickle
import sys
from pathlib import Path

REPO = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/ta-np45-tw1-20261029")
REG = (
    Path.home() / "Developer/research-context/bridge/2026-10-27-run-atlas/cross-test-registry.json"
)
entry = next(
    e for e in json.loads(REG.read_text())["entries"] if e["id"] == "trusted-monitoring-logs"
)
path = REPO / entry["adapter"]["module_path"]
spec = importlib.util.spec_from_file_location("tm", path)
m = importlib.util.module_from_spec(spec)
sys.modules["tm"] = m
sys.path.insert(0, str(path.parent))
spec.loader.exec_module(m)
rows = []
for row in m.specs():
    gold = row.get("gold") if isinstance(row.get("gold"), dict) else {}
    labels = row.get("labels") if isinstance(row.get("labels"), dict) else {}
    rows.append(
        (
            row.get("_steps"),
            {
                "set": "trusted-monitoring-logs",
                "state": gold.get("state")
                or gold.get("label_state")
                or labels.get("state")
                or labels.get("label_state"),
                "positive": row.get("positive") if isinstance(row.get("positive"), bool) else None,
            },
        )
    )
with (
    Path.home()
    / ".local/state/evallab-worker-evidence/np45-tw1-20261029/trusted-monitoring-logs-rows.pkl"
).open("wb") as handle:
    pickle.dump(rows, handle)
print("rows", len(rows))
