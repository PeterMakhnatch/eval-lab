# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""NP35 worker-e shared runner. Aggregates only. Primary numbers come from the unchanged s_shim (NP29 S)."""

import sys, os, glob, json, hashlib, collections

sys.dont_write_bytecode = True
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.path.insert(0, "/private/tmp/np35")
import s_shim  # noqa: E402


def load_adapter(corpus_dir):
    d = glob.glob(f"/private/tmp/np35/research/evidence/runs/*/corpora/{corpus_dir}/tools")[0]
    sys.path.insert(0, d)
    import adapter

    return adapter


def map_steps(steps):
    """Mechanical mapping using the corpus's own fields only: source<-source|role, function_name<-function_name|function|name."""
    out = []
    for s in steps:
        tcs = []
        for t in s.get("tool_calls") or []:
            tcs.append(
                {
                    "function_name": t.get("function_name", t.get("function", t.get("name"))),
                    "arguments": t.get("arguments"),
                }
            )
        out.append({"source": s.get("source", s.get("role")), "tool_calls": tcs})
    return out


def run(
    registry_id,
    corpus_dir,
    groups_of,
    task_keys_of,
    steps_of,
    spec_iter,
    out_slug,
    script,
    notes,
    task_key_text,
    extra_counts=None,
):
    reg = json.load(
        open(
            "/Users/petermakhnatch/Developer/research-context/bridge/2026-10-27-run-atlas/cross-test-registry.json"
        )
    )
    ent = [e for e in reg["entries"] if e["id"] == registry_id][0]
    fab = ent["labels"]["false_alarm_basis"]
    t = s_shim.Tally()
    keysets = collections.defaultdict(lambda: collections.defaultdict(set))
    total = collections.Counter()
    for spec in spec_iter():
        gs = groups_of(spec)  # list of group names (first is the primary)
        keys = task_keys_of(spec)  # dict reading-name -> key
        steps = steps_of(spec)
        a = None
        for g in gs:
            if steps is None:
                # no steps analysed (excluded without replay): count only
                t.n[g] += 1
                t.tasks.setdefault(g, set()).add(keys["primary"])
                t.no_agent_calls[g] += 0
            else:
                t.add(g, keys["primary"], steps)
            for name, k in keys.items():
                keysets[g][name].add(k)
        total[gs[0]] += 1
    rep = t.report()
    for g in rep:
        rep[g]["distinct_task_keys_by_reading_all"] = {n: len(v) for n, v in keysets[g].items()}
    out = {
        "registry": {
            "positives": fab["positives"],
            "negatives": fab["negatives"],
            "established": fab["established_negatives"],
            "negative_basis": fab["negative_basis"],
        },
        "groups": rep,
        "task_key": task_key_text,
        "S_not_applicable": False,
        "reconciled_to_registry": None,
        "notes": notes,
        "script": script,
        "primary_group_counts": dict(total),
    }
    if extra_counts:
        out.update(extra_counts(rep, total))
    return out


def save(slug_id, out):
    p = "/private/tmp/np35/w-e.json"
    cur = json.load(open(p)) if os.path.exists(p) else {"corpora": {}}
    cur["corpora"][slug_id] = out
    json.dump(cur, open(p, "w"), indent=1)
    print(json.dumps(out, indent=1))
