# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from common_a import *

ad = (
    ADAPTERS
    + "/corpus-expansion-pq2-20261028/corpora/wiserhumanexperimental__evilgenie-escalation/tools"
)
sys.path.insert(0, ad)
import adapter

t = s_shim.Tally()
sc = s_shim.Tally()
pool = s_shim.Tally()
for s in adapter.specs():
    st, cohort = s["labels"]["state"], s["identity"]["cohort"]
    if st == "positive":
        g = "positive"
    elif st == "negative":
        g = (
            "negative_established"
            if cohort == "extra_negative_control"
            else "negative_unestablished"
        )
    elif st == "ambiguous":
        g = "ambiguous"
    else:
        g = "excluded"
    k, k2 = (
        s_shim.task_hash(s["identity"]["problem_id"]),
        s_shim.task_hash(s["identity"]["scoped_task_id"]),
    )
    t.add(g, k, s["_steps"])
    sc.add(g, k2, s["_steps"])
    if st == "negative":
        pool.add("all_negatives_pooled", k, s["_steps"])
rep = t.report()
rep.update(pool.report())
for g, v in sc.report().items():
    rep[g]["distinct_tasks_all_scoped"] = v["distinct_tasks_all"]
    rep[g]["distinct_tasks_S_scoped"] = v["distinct_tasks_S"]
allk = lambda tl: len(set().union(*tl.tasks.values()))
entry = {
    "registry": {"positives": 124, "negatives": 1990, "established": 177},
    "groups": rep,
    "task_key": "sha256 prefix (s_shim.task_hash) of native identity.problem_id (15 underlying problems; primary reading, matches registry bootstrap unit). Alternative: problem_set/problem_id scoped id = 18 keys (3 cross-set collisions). Both reported as distinct_tasks_* and distinct_tasks_*_scoped.",
    "task_keys_distinct": {"problem_id": allk(t), "scoped_problem_set_and_id": allk(sc)},
    "S_not_applicable": False,
    "reconciled_to_registry": rep["negative_established"]["runs"] == 177
    and rep["positive"]["runs"] == 124
    and rep["all_negatives_pooled"]["runs"] == 1990,
    "notes": [
        "established = state negative AND cohort extra_negative_control (registry: 177 control negatives); unestablished = state negative in indexed_main cohort (1,813); ambiguous = 174 (heuristic/channel conflict); excluded = 7 native error. Note the 3 heuristic controls fall into ambiguous.",
        "Steps carry structured tool_calls (native function_name + arguments) used as is via s_shim.",
    ],
    "script": "/private/tmp/np35/scripts/evilgenie_escalation.py",
}
save("evilgenie-escalation", entry)
