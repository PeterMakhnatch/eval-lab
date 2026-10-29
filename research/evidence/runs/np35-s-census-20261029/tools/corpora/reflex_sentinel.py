# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from common_a import *

ad = (
    ADAPTERS
    + "/corpus-expansion-pq1-20261027/corpora/matu79go__reflex-sentinel-agent-trajectories/tools"
)
sys.path.insert(0, ad)
import adapter

t = s_shim.Tally()
pool = s_shim.Tally()
for s in adapter.specs():  # default subset: the 250 primary agent runs
    if s["positive"]:
        g = "positive"
    elif s["split"] == "original":
        g = "negative_established"  # original controls: no conflict by construction
    else:
        g = "negative_unestablished"  # conflicting runs, only "no final test-file diff"
    k = s["task_key"]  # native task_id -> sha256[:16] (adapter); 50 keys
    t.add(g, k, s["_steps"])
    if not s["positive"]:
        pool.add("all_negatives_pooled", k, s["_steps"])
rep = t.report()
rep.update(pool.report())
entry = {
    "registry": {"positives": 45, "negatives": 205, "established": 50},
    "groups": rep,
    "task_key": "spec.task_key = sha256(native task_id)[:16] from the adapter (native task id field); 50 distinct keys over 250 primary runs; not a proxy. Retry subsets (1,170 dependent continuations) not used.",
    "task_keys_distinct": {
        "task_id": len(
            {
                *t.tasks["positive"],
                *t.tasks["negative_established"],
                *t.tasks["negative_unestablished"],
            }
        )
    },
    "S_not_applicable": False,
    "reconciled_to_registry": rep["negative_established"]["runs"] == 50
    and rep["positive"]["runs"] == 45
    and rep["all_negatives_pooled"]["runs"] == 205,
    "notes": [
        "established = 50 split=='original' controls (all test_tampered=false); unestablished = 155 split=='conflicting' negatives (no flag fired). Registry: established 50, control 50.",
        "Steps: agent turns carry structured tool_calls (str_replace_editor, bash_command{keystrokes}, submit); used as is via s_shim.",
    ],
    "script": "/private/tmp/np35/scripts/reflex_sentinel.py",
}
save("reflex-sentinel", entry)
