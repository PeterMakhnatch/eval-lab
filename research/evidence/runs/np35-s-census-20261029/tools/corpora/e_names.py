# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys, os, glob, collections

d = glob.glob(f"/private/tmp/np35/research/evidence/runs/*/corpora/{sys.argv[1]}/tools")[0]
sys.path.insert(0, d)
import adapter

f = getattr(adapter, "specs", None) or adapter.iter_specs
src = collections.Counter()
fn = collections.Counter()
keys = collections.Counter()
n = 0
for s in f():
    n += 1
    for x in s["_steps"]:
        src[x.get("source", x.get("role"))] += 1
        for t in x.get("tool_calls") or []:
            fn[t.get("function", t.get("name", t.get("function_name")))] += 1
            keys[tuple(sorted(t))] += 1
print(n, dict(src))
print(fn.most_common(30))
print(keys)
