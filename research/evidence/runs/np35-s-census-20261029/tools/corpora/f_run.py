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
    "paichecker": "manyi-w__paichecker",
    "automationbench-verified": "parsewave__automationbench-verified",
}[ID]
sys.path.insert(0, glob.glob(f"/private/tmp/np35/research/evidence/runs/*/corpora/{DIR}/tools")[0])
import adapter

H = s_shim.task_hash


def classify(s):
    """-> (group, primary task key, {reading: key}) ; groups: positive, negative_established, negative_unestablished, ambiguous, excluded"""
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
