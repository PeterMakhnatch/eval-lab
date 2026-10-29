# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""Shared driver for NP35 worker B. Applies the unchanged s_shim to a corpus adapter; aggregates only."""

import json, os, sys, hashlib
from collections import Counter

sys.dont_write_bytecode = True
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.path.insert(0, "/private/tmp/np35")
import s_shim  # noqa: E402

BASE = "/private/tmp/np35/research/evidence/runs/corpus-expansion-pq2-20261028/corpora/"


def run(corpus_dir, reg_id, registry, classify, keys, key_text, notes, script, out_name):
    sys.path.insert(0, BASE + corpus_dir + "/tools")
    import adapter

    t = s_shim.Tally()
    sets = {}
    toolnames = Counter()
    for spec in (
        adapter.specs(include_excluded=True)
        if "include_excluded" in adapter.specs.__code__.co_varnames
        else adapter.specs()
    ):
        g = classify(spec)
        ks = keys(spec)
        steps = s_shim.normalise_steps(spec["_steps"])
        t.add(g, ks["primary"], steps)
        for name, v in ks.items():
            sets.setdefault((g, name), set()).add(v)
        # pooled groups
        if g.startswith("negative"):
            t.add("ALL_negatives_pooled", ks["primary"], steps)
            for name, v in ks.items():
                sets.setdefault(("ALL_negatives_pooled", name), set()).add(v)
        for st in steps:
            if st.get("source") in ("agent", "assistant"):
                for c in st.get("tool_calls") or []:
                    toolnames[c["function_name"]] += 1
    rep = t.report()
    kd = {}
    for (g, name), v in sets.items():
        kd.setdefault(g, {})[name] = len(v)
    out = {
        "registry": registry,
        "groups": rep,
        "task_key": key_text,
        "task_keys_distinct": kd,
        "S_not_applicable": False,
        "tool_name_counts": dict(toolnames.most_common(25)),
        "notes": notes,
        "script": script,
    }
    est = rep.get("negative_established", {}).get("runs", 0)
    out["reconciled_to_registry"] = est == registry["established"]
    out["established_counted"] = est
    p = "/private/tmp/np35/w-b.partial-%s.json" % reg_id
    json.dump(out, open(p, "w"), indent=1)
    print(
        reg_id,
        "established",
        est,
        "registry",
        registry["established"],
        "S_est",
        rep.get("negative_established", {}).get("S"),
        "S_tasks",
        rep.get("negative_established", {}).get("distinct_tasks_S"),
        "S_all_neg",
        rep["ALL_negatives_pooled"]["S"],
    )
