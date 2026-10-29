# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from _common import *

ad = load(
    "corpus-expansion-pq5-20261029/corpora/aisa-group__InferenceBench-Trajectories", "ib_adapter"
)
t = s_shim.Tally()
keys = {"scenario": set(), "scenario_x_agent": set(), "agent": set(), "task_full": set()}
for s in ad.specs():
    st = s["labels"]["state"]
    i = s["identity"]
    k = s_shim.task_hash(i["task_id"])
    keys["scenario"].add(k)
    keys["agent"].add(i["agent"])
    keys["scenario_x_agent"].add((k, i["agent"]))
    keys["task_full"].add(i["task_full_key"])
    steps = ad.to_steps(s)
    g = {"positive": "positive", "negative": "negative_unestablished"}.get(st, st)
    t.add(g, k, steps)
    if st == "negative":
        t.add("negatives_pooled", k, steps)
        t.add("negative_unestablished/era=" + str(i["era"]), k, steps)
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
    "inferencebench-trajectories",
    {
        "registry": {"positives": 17, "negatives": 211, "established": 0},
        "groups": rep,
        "task_key": "identity.task_id = scenario (A-D) -> s_shim.task_hash. A PROXY: the task is one LLM-server optimisation problem per scenario, shared by all 23 agent configs; the 269 runs are repeats of 4 scenarios.",
        "task_keys_distinct": {
            "scenario": len(keys["scenario"]),
            "task_full_id(base_model/scenario)": len(keys["task_full"]),
            "agent_config": len(keys["agent"]),
            "scenario_x_agent": len(keys["scenario_x_agent"]),
        },
        "S_not_applicable": False,
        "reconciled_to_registry": rep["positive"]["runs"] == 17
        and rep["negatives_pooled"]["runs"] == 211
        and rep["negative_established"]["runs"] == 0,
        "notes": [
            "Registry established_negatives = 0; the 211 negatives are 'flag false and both judge lines none' (75 of them flag-never-set post-paper-agent runs). All are negative_unestablished; split by era in extra groups.",
            "Steps are Claude-Code/codex style events from trace.jsonl with tool_name/tool_input (Bash, Edit, Write, Read...), used as is via the adapter's to_steps; no mapping. Runs whose trace is missing give empty steps.",
            "Task: the job is optimising an inference server; strict test paths (tests/ dirs) are not part of that convention, so S is expected to be small; see L.",
        ],
        "script": "/private/tmp/np35/scripts/inferencebench.py",
    },
)
