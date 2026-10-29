# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""NP35 worker c: shared runner (named wc_common to avoid clashing with other workers). Aggregates only. Uses the unchanged s_shim (NP29 S definition)."""

import importlib.util, json, os, sys, hashlib

sys.dont_write_bytecode = True
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.path.insert(0, "/private/tmp/np35")
import s_shim  # noqa: E402

RUNS = "/private/tmp/np35/research/evidence/runs/"
REG = os.path.expanduser(
    "~/Developer/research-context/bridge/2026-10-27-run-atlas/cross-test-registry.json"
)


def load_adapter(rel):
    p = RUNS + rel + "/tools/adapter.py"
    sys.path.insert(0, os.path.dirname(p))
    sp = importlib.util.spec_from_file_location(
        "adapter_" + hashlib.md5(rel.encode()).hexdigest()[:6], p
    )
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


def registry(rid):
    e = next(e for e in json.load(open(REG))["entries"] if e["id"] == rid)
    b = e["labels"]["false_alarm_basis"]
    return {
        "positives": b["positives"],
        "negatives": b["negatives"],
        "established": b["established_negatives"],
        "negative_basis": b["negative_basis"],
        "control_negatives": b["control_negatives"],
    }


def run(
    rid,
    rel,
    classify,
    task_keys,
    steps_of,
    specs_kw=None,
    extra_notes=(),
    task_key_text="",
    s_na=False,
):
    ad = load_adapter(rel)
    t = s_shim.Tally()
    keysets, counts = {}, {}
    for s in ad.specs(**(specs_kw or {})):
        g = classify(s)
        tk = task_keys(s)
        primary = s_shim.task_hash(next(iter(tk.values())))
        steps = steps_of(s)
        t.add(g, primary, steps)
        if g.startswith("negative"):
            t.add("negative_all_pooled", primary, steps)
        for k, v in tk.items():
            keysets.setdefault(k, {}).setdefault(g, set()).add(v)
            keysets[k].setdefault("ALL_runs", set()).add(v)
        counts[g] = counts.get(g, 0) + 1
    rep = t.report()
    reg = registry(rid)
    est = rep.get("negative_established", {}).get("runs", 0)
    unest = rep.get("negative_unestablished", {}).get("runs", 0)
    out = {
        "registry": reg,
        "groups": rep,
        "task_key": task_key_text,
        "task_keys_distinct": {k: {g: len(v) for g, v in d.items()} for k, d in keysets.items()},
        "S_not_applicable": s_na,
        "counts_by_group": counts,
        "reconciled_to_registry": est == reg["established"]
        and counts.get("positive", 0) == reg["positives"]
        and est + unest == reg["negatives"],
        "notes": list(extra_notes),
        "script": os.path.abspath(sys.argv[0]),
    }
    json.dump(out, open(f"/private/tmp/np35/scripts/out-{rid}.json", "w"), indent=1)
    print(
        json.dumps({k: out[k] for k in ("registry", "counts_by_group", "reconciled_to_registry")})
    )
    for g, r in rep.items():
        print(g, r)
    print(out["task_keys_distinct"])
    return out
