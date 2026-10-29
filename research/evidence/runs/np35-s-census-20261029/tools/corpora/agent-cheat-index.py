# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from _np35b import *

FAM = Counter()


def classify(s):
    st = s["labels"]["state"] if "state" in s["labels"] else s["gold"]["label_state"]
    fam = s["identity"]["family"]
    FAM[(st, fam)] += 1
    if st == "negative":
        return "negative_established" if fam == "F4" else "negative_unestablished"
    return st


def keys(s):
    i = s["identity"]
    return {"primary": s_shim.task_hash(i["task_id"]), "family": i["family"]}


run(
    "b0yko__cheat-index",
    "agent-cheat-index",
    {"positives": 62, "negatives": 1086, "established": 270},
    classify,
    keys,
    "Native task id (identity.task_id = row.task.id) -> s_shim.task_hash (40 tasks, 10 per family). Proxy also reported: task family (4).",
    [
        "established negative = valid negative run whose task family is F4 = solvable-control (registry 270, no trap); other negatives unestablished; ambiguous = literal tampered flag without quality-gated detector (14); excluded = errored/rules_not_loaded/unparsed via include_excluded=True.",
        "Steps used as-is from the adapter (Claude/Codex/OpenCode/Copilot streams already mapped to {source, tool_calls:[{function_name,arguments}]}).",
    ],
    "/private/tmp/np35/scripts/agent-cheat-index.py",
    "cheat",
)
import json as _j

print(sorted(FAM.items()))
