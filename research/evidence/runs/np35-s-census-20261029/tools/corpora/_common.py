# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""NP35 worker D common: run one corpus through the frozen S shim. Aggregates only; never prints a cell."""

import sys, os, json, importlib.util

os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, "/private/tmp/np35")
import s_shim  # noqa: E402  (frozen NP29 S, unchanged)

BASE = "/private/tmp/np35/research/evidence/runs/"
OUT = "/private/tmp/np35/w-d.json"


def load(rel, name):
    p = BASE + rel + "/tools/"
    spec = importlib.util.spec_from_file_location(name, p + "adapter.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def map_file_change(steps):
    """BAITBENCH: split each file_change call's ``changes`` list into one call per change with ``path`` set
    (the shim's INPLACE_TOOLS already contains ``file_change`` but reads a ``path`` key). Nothing else changes."""
    out = []
    for st in steps:
        calls = []
        for c in st.get("tool_calls") or []:
            a = c.get("arguments")
            if c.get("function_name") == "file_change" and isinstance(a, dict) and "changes" in a:
                for ch in a["changes"]:
                    calls.append(
                        {"function_name": "file_change", "arguments": {"path": ch["path"]}}
                    )
            else:
                calls.append(c)
        out.append({**st, "tool_calls": calls})
    return out


def save(cid, entry):
    try:
        d = json.load(open(OUT))
    except FileNotFoundError:
        d = {"corpora": {}}
    d["corpora"][cid] = entry
    json.dump(d, open(OUT, "w"), indent=1, sort_keys=True)
    print(json.dumps(entry, indent=1, sort_keys=True))
