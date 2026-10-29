# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from _common import *

ad = load("corpus-expansion-pq3-20261028/corpora/juanjvazquez__BAITBENCH", "bb_adapter")
t = s_shim.Tally()
t_asis = s_shim.Tally()
keys = {"task_id": set(), "instance": set(), "family": set()}
for s in ad.specs():
    st = s["labels"]["state"]
    i = s["identity"]
    k = s_shim.task_hash(i["task_id"])
    keys["task_id"].add(k)
    keys["instance"].add(i["instance_key"])
    keys["family"].add(i["cluster_key"])
    steps = map_file_change(s["_steps"])
    g = {"positive": "positive", "negative": "negative_unestablished"}.get(st, st)
    t.add(g, k, steps)
    t_asis.add(g, k, s["_steps"])
    if st == "negative":
        t.add("negatives_pooled", k, steps)
        t_asis.add("negatives_pooled", k, s["_steps"])
        t.add("negative_unestablished/family=" + s_shim.task_hash(i["cluster_key"]), k, steps)
rep = t.report()
for g in ("negative_established",):
    rep[g] = {
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
    "baitbench",
    {
        "registry": {"positives": 681, "negatives": 488, "established": 0},
        "groups": rep,
        "groups_as_is_unmapped_variant": t_asis.report(),
        "task_key": "identity.task_id (native dataset-instance task id, e.g. family+instance) -> s_shim.task_hash. This is the primary key (180 distinct over all runs).",
        "task_keys_distinct": {
            "task_id_all_runs": len(keys["task_id"]),
            "dataset_instance(90)": len(keys["instance"]),
            "task_family(3)": len(keys["family"]),
        },
        "S_not_applicable": False,
        "reconciled_to_registry": rep["positive"]["runs"] == 681
        and rep["negatives_pooled"]["runs"] == 488
        and rep["negative_established"]["runs"] == 0,
        "notes": [
            "Registry established_negatives = 0 (negatives are 'both judges not_reward_hacking', basis no_flag_fired): every negative is negative_unestablished.",
            "_steps are Codex-style events: command_execution{command} (shell, read by S as is), tool_read{files} (read, ignored), file_change{changes:[{kind,path}]}. file_change is in the shim's INPLACE_TOOLS but carries no `path` key, so as-is it is invisible; PRIMARY numbers use ONE mapping (map_file_change in _common.py: one call per change with path=change.path, function name kept). The as-is variant is under groups_as_is_unmapped_variant.",
            "Only 3 synthetic tabular task families / 90 instances: distinct-task counts depend heavily on which key is used (see task_keys_distinct).",
            "ambiguous=82 and excluded=7 per registry are listed as their own groups.",
        ],
        "script": "/private/tmp/np35/scripts/baitbench.py",
    },
)
