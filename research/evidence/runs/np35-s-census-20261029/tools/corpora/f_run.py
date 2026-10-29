# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""NP35-LB-1 worker F: S (frozen NP29 definition via s_shim, unchanged) over eight staged corpora. Aggregates only.
usage: python3 f_run.py <registry-id>   -> writes /private/tmp/np35/scripts/out-f-<id>.json
"""

import sys, os, glob, json
from collections import Counter

sys.dont_write_bytecode = True
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.path.insert(0, "/private/tmp/np35")
import s_shim

ID = sys.argv[1]
DIR = {
    "sleight-bench": "safety-research__sleight-bench",
    "apollo-insider-trading": "apolloresearch__insider-trading",
    "hack-ignition": "eleutherai__hack-ignition-benchmark",
    "assistant-axis": "arkonyx__assistant-axis-reward-hacking",
    "cuaverifierbench": "microsoft__cuaverifierbench",
    "collusion-probe-pitfalls": "mainul3195__collusion-probe-pitfalls",
    "paichecker": "manyi-w__paichecker",
    "automationbench-verified": "parsewave__automationbench-verified",
}[ID]
sys.path.insert(0, glob.glob(f"/private/tmp/np35/research/evidence/runs/*/corpora/{DIR}/tools")[0])
import adapter

H = s_shim.task_hash


def classify(s):
    """-> (group, primary task key, {reading: key}) ; groups: positive, negative_established, negative_unestablished, ambiguous, excluded"""
    if ID == "sleight-bench":
        g = "positive" if s["positive"] else "negative_established"
        return (
            g,
            H(s["pair_key"]),
            {
                "pair_key(construction group)": s["pair_key"],
                "segment_pair_key": s["segment_pair_key"],
                "scenario_key": s["labels"].get("scenario_key"),
                "category_key": s["labels"].get("category_key"),
            },
        )
    if ID == "cuaverifierbench":
        g = "positive" if s["positive"] else "negative_established"
        return g, H(s["task_key"]), {"task_key(native task_id)": s["task_key"]}
    if ID == "apollo-insider-trading":
        if s["target_label"] != "is_misaligned":
            g = "excluded"  # deception / doubling_down rows: other targets, scored separately (out of domain)
        else:
            g = (
                "positive" if s["positive"] else "negative_unestablished"
            )  # string-match verdict only: no_flag_fired
        return (
            g,
            H(s["task_key"]),
            {
                "task_key(experiment+scenario hash; proxy=scenario)": s["task_key"],
                "cluster_key(root run)": s["cluster_key"],
            },
        )
    if ID == "hack-ignition":
        gg = s["gold_grades"]
        if s["positive"]:
            g = "positive"
        elif gg.get("hardened") == 1:
            g = "negative_established"  # passes the hardened grader: executed-correct (registry 5,418)
        else:
            g = "negative_unestablished"
        return g, H(s["task_key"]), {"task_key(MBPP task id hash)": s["task_key"]}
    if ID == "assistant-axis":
        gd = s["gold"]
        if gd["verdict"] == "HACK":
            g = (
                "positive" if gd["strength"] == "STRONG" else "excluded"
            )  # 32 WEAK HACK excluded from primary scoring
        else:
            g = "negative_unestablished"  # 635 weak negatives, zero confirmed clean
        return (
            g,
            H(s["problem_key"]),
            {
                "problem_key(evaluator-only problem; proxy)": s["problem_key"],
                "episode_key": s["episode_key"],
            },
        )
    if ID == "collusion-probe-pitfalls":
        st = s["labels"]["state"]
        g = {
            "positive": "positive",
            "negative": "negative_unestablished",
            "ambiguous": "ambiguous",
            "excluded": "excluded",
        }[st]
        return (
            g,
            H(s["identity"]["task_id"]),
            {
                "identity.task_id(scenario; proxy)": s["identity"]["task_id"],
                "identity.run_id": s["identity"]["run_id"],
            },
        )
    if ID == "paichecker":
        g = "positive" if s["gold"]["positive"] else "negative_established"
        i = s["identity"]
        k = f"{i['sub_corpus']}/{i['instance_id']}"
        return (
            g,
            H(k),
            {
                "sub_corpus/instance_id(native)": k,
                "repo(cluster; proxy)": f"{i['sub_corpus']}/{i['repo']}",
            },
        )
    if ID == "automationbench-verified":
        g = (
            "positive"
            if s["gold"]["label_state"] == "defective_verified"
            else "negative_unestablished"
        )
        i = s["identity"]
        return (
            g,
            H(i["example_id"]),
            {
                "example_id(native)": i["example_id"],
                "task_name": i["task_name"],
                "domain(cluster; proxy)": i["domain"],
            },
        )


t = s_shim.Tally()
readings = {}  # reading -> group -> set
nsteps = Counter()
srcs = Counter()
tc_steps = Counter()
has_steps = Counter()
Lcount = Counter()
for s in adapter.specs():
    g, tk, extra = classify(s)
    steps = s.get("_steps")
    has_steps[steps is not None] += 1
    steps = steps or []
    a = t.add(g, tk, steps)
    t.add("pooled_all_negatives", tk, steps) if g.startswith("negative") else None
    for st in steps:
        srcs[st.get("source")] += 1
        tc_steps[bool(st.get("tool_calls"))] += 1
    for r, k in extra.items():
        for grp in (g, "ALL"):
            readings.setdefault(r, {}).setdefault(grp, set()).add(k)
    nsteps[g] += len(steps)
rep = t.report()
rep["ALL"] = None
all_runs = sum(
    v["runs"]
    for k, v in rep.items()
    if v
    and k in ("positive", "negative_established", "negative_unestablished", "ambiguous", "excluded")
)
rep["ALL"] = {"runs": all_runs}
out = {
    "groups": rep,
    "task_keys_distinct": {r: {g: len(v) for g, v in d.items()} for r, d in readings.items()},
    "primary_task_key_distinct_all": len(set().union(*t.tasks.values())) if t.tasks else 0,
    "specs_with_steps": {str(k): v for k, v in has_steps.items()},
    "step_sources": {str(k): v for k, v in srcs.items()},
    "steps_with_tool_calls": tc_steps.get(True, 0),
    "steps_total": sum(tc_steps.values()),
}
json.dump(out, open(f"/private/tmp/np35/scripts/out-f-{ID}.json", "w"), indent=1)
print(ID, "done", {g: (v["runs"], v.get("S")) for g, v in rep.items() if v})
