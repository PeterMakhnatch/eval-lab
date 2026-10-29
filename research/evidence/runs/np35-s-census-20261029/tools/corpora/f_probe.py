# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""Structure-only probe (worker F): key names and tool-call presence. Prints no content."""

import sys, os, glob
from collections import Counter

sys.dont_write_bytecode = True
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.path.insert(
    0, glob.glob(f"/private/tmp/np35/research/evidence/runs/*/corpora/{sys.argv[1]}/tools")[0]
)
import adapter

n = 0
keys = Counter()
stepkeys = Counter()
src = Counter()
tc = 0
gk = Counter()
lab = Counter()
callt = Counter()
for s in adapter.specs():
    n += 1
    keys.update(s.keys())
    for x in s.get("_steps") or []:
        stepkeys.update(x.keys())
        src[str(x.get("source"))] += 1
        if x.get("tool_calls"):
            tc += 1
    for k in ("gold", "labels", "identity"):
        if isinstance(s.get(k), dict):
            gk.update(f"{k}.{a}" for a in s[k])
    callt[type(s.get("_calls")).__name__] += 1
print("n", n)
print("spec keys", dict(keys))
print("step keys", dict(stepkeys))
print("src", dict(src))
print("steps with tool_calls", tc)
print("gold/labels/identity keys", dict(gk))
print("_calls type", dict(callt))
