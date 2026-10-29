# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from _common import *

ad = load("corpus-expansion-pq3-20261028/corpora/ebt55__kobayashi-maru", "km_adapter")
t = s_shim.Tally()
keys = {"task_id": set(), "task_key": set(), "base_task_id": set(), "batch": set()}
sl = {}
for s in ad.specs():
    st = s["labels"]["state"]
    i = s["identity"]
    k = s_shim.task_hash(i["task_id"])
    keys["task_id"].add(k)
    keys["task_key"].add(i["task_key"])
    keys["base_task_id"].add(s_shim.task_hash(i["base_task_id"]))
    keys["batch"].add(i["batch_key"])
    g = {"positive": "positive", "negative": "negative_unestablished"}.get(st, st)
    t.add(g, k, s["_steps"])
    if st == "negative":
        t.add("negatives_pooled", k, s["_steps"])
        t.add("negative_unestablished/slice=" + str(s["labels"]["item_slice"]), k, s["_steps"])
rep = t.report()
rep["negative_established"] = {
    "runs": 0,
    "S": 0,
    "SI": 0,
    "L": 0,
    "L_not_S": 0,
    "distinct_tasks_all": 0,
    "distinct_tasks_S": 0,
    "runs_without_any_tool_call": 0,
    "unresolved_editor_calls": 0,
    "S_form_counts": {},
}
save(
    "kobayashi-maru",
    {
        "registry": {"positives": 789, "negatives": 7916, "established": 0},
        "groups": rep,
        "task_key": "identity.task_id (native item/variant id) -> s_shim.task_hash; distinct counts in groups are over this key. Alternatives in task_keys_distinct: task_key (publisher digest of item_key), base_task_id (underlying authored utility, a family proxy), batch (not a task).",
        "task_keys_distinct": {k: len(v) for k, v in keys.items()},
        "S_not_applicable": False,
        "reconciled_to_registry": rep["positive"]["runs"] == 789
        and rep["negatives_pooled"]["runs"] == 7916
        and rep["negative_established"]["runs"] == 0,
        "notes": [
            "Registry established_negatives = 0 and control_negatives = 5477 (solvable slice) which the registry does NOT count as established; all negatives are negative_unestablished, split by item slice in extra groups.",
            "Steps carry tool_calls with the adapter's function_name/arguments (bash{command}, write_file{path,content}, run_tests, submit, read_file); used as is, no mapping.",
        ],
        "script": "/private/tmp/np35/scripts/kobayashi.py",
    },
)
