# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys, types, re

sys.path.insert(0, "/private/tmp/np35/scripts")
from common_a import *


# system python3 has no PyYAML. Minimal stand-in: annotation() only reads top-level run_id and verdict (verified in adapter);
# quotes only feed gold_evidence, which this script never calls. Labels are unchanged; _steps do not use yaml.
def _safe_load(text):
    f = lambda k: (
        re.search(rf"(?m)^{k}:\s*['\"]?([^'\"\n#]+?)['\"]?\s*(?:#.*)?$", text).group(1).strip()
    )
    return {"run_id": f("run_id"), "verdict": f("verdict"), "quotes": []}


sys.modules["yaml"] = types.SimpleNamespace(safe_load=_safe_load)
ad = ADAPTERS + "/corpus-expansion-pq1-20261027/corpora/infatoshi__kernelbench-hard-runs/tools"
sys.path.insert(0, ad)
import adapter
from collections import Counter

t = s_shim.Tally()
pool = s_shim.Tally()
harness = {}
for s in adapter.specs(annotated_only=False):
    v = s["verdict"]
    g = {"rubric_leak": "positive", "clean": "negative_established", None: "unlabeled"}[v]
    k = s_shim.task_hash(s["task"])  # native `problem` field (benchmark problem name)
    t.add(g, k, s["_steps"])
    harness.setdefault(g, Counter())[s["harness"]] += 1
    if v == "clean":
        pool.add("all_negatives_pooled", k, s["_steps"])
rep = t.report()
rep.update(pool.report())
for g in rep:
    rep[g]["harness_counts"] = dict(harness.get(g, {}))
entry = {
    "registry": {"positives": 11, "negatives": 19, "established": 19},
    "groups": rep,
    "task_key": "sha256 prefix (s_shim.task_hash) of native `problem` field (benchmark problem name); a real task id, not a proxy. Scoreable subset has 5 problems; all 84 rows reported under 'unlabeled' for the 54 unknowns (NOT negatives).",
    "task_keys_distinct": {
        "problem_all_84_rows": len(set().union(*t.tasks.values())),
        "problem_scoreable_30": len(t.tasks["positive"] | t.tasks["negative_established"]),
    },
    "S_not_applicable": False,
    "reconciled_to_registry": rep["positive"]["runs"] == 11
    and rep["negative_established"]["runs"] == 19
    and rep["unlabeled"]["runs"] == 54,
    "notes": [
        "PyYAML absent in system python3: yaml.safe_load replaced by a regex reader of run_id/verdict only (labels identical to the adapter's; _steps path never uses yaml).",
        "Steps: opencode/claude/kimi events with structured tool_calls (shell names normalised by the adapter to bash_command{keystrokes}); codex runs are opaque plaintext without tool calls (see runs_without_any_tool_call).",
        "Only 5 problems in the scoreable subset: the >=20 distinct-task threshold cannot be met even if S were large.",
    ],
    "script": "/private/tmp/np35/scripts/kernelbench_hard_runs.py",
}
save("kernelbench-hard-runs", entry)
