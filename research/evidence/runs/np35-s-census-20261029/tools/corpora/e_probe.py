# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys, os, json, glob

os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
d = glob.glob(f"/private/tmp/np35/research/evidence/runs/*/corpora/{sys.argv[1]}/tools")[0]
sys.path.insert(0, d)
import adapter


def shape(v, depth=0):
    if isinstance(v, dict):
        return {
            k: (shape(x, depth + 1) if depth < 2 else type(x).__name__)
            for k, x in list(v.items())[:25]
        }
    if isinstance(v, list):
        return [shape(v[0], depth + 1)] if v else []
    return type(v).__name__


f = getattr(adapter, "specs", None) or adapter.iter_specs
for s in f():
    print({k: shape(v) if k != "_steps" else None for k, v in s.items() if k != "gold" or True})
    st = s["_steps"]
    print(len(st))
    print(shape(st[:3]))
    for x in st:
        if x.get("tool_calls"):
            print(shape(x))
            break
    break
