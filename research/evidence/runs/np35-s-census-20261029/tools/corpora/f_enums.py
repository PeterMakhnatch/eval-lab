# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""Worker F: categorical field value counts (short enum-like strings/bools only; no content)."""

import sys, os, glob
from collections import Counter

sys.dont_write_bytecode = True
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.path.insert(
    0, glob.glob(f"/private/tmp/np35/research/evidence/runs/*/corpora/{sys.argv[1]}/tools")[0]
)
import adapter

C = {}


def walk(prefix, d, depth=0):
    for k, v in d.items():
        if k in ("_steps", "auditor_input"):
            continue
        if isinstance(v, dict) and depth < 2:
            walk(f"{prefix}{k}.", v, depth + 1)
        elif (
            isinstance(v, (bool, int, float)) or v is None or (isinstance(v, str) and len(v) <= 40)
        ):
            C.setdefault(prefix + k, Counter())[
                v if not isinstance(v, (int, float)) or isinstance(v, bool) else "num"
            ] += 1
        else:
            C.setdefault(prefix + k, Counter())["<other>"] += 1


for s in adapter.specs():
    walk("", s)
for k, c in C.items():
    print(k, len(c), dict(c.most_common(8)) if len(c) <= 12 else f"({len(c)} distinct)")
