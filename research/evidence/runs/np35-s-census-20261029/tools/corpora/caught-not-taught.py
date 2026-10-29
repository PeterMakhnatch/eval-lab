# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from e_common import *

ad = load_adapter("amirgroup-codes__caught_not_taught")


def groups(s):
    st = s["gold"]["state"]
    return ["negative_established", "all_negatives"] if st == "negative" else [st]


def keys(s):
    i = s["identity"]
    return {
        "primary": s_shim.task_hash(i["main_name"]),
        "problem_id_proxy": s_shim.task_hash(i["problem_id"]),
    }


out = run(
    "caught-not-taught",
    "",
    groups,
    keys,
    lambda s: map_steps(s["_steps"]),
    ad.specs,
    "caught-not-taught",
    "/private/tmp/np35/scripts/caught-not-taught.py",
    [
        "Adapter _steps use role/function keys; mapped mechanically (role->source, function->function_name).",
        "Registry negatives are all honest-mode episodes, counted established by design (control_negatives=160); no negative_unestablished group exists.",
        "Tool set: bash, text_editor, cdk_ls, submit.",
    ],
    "identity.main_name (native ControlArena main task name) -> s_shim.task_hash. Second reading identity.problem_id (differs across attack cells: more distinct values than main tasks, i.e. main x side-task variant; NOT used as primary). Only 5 main tasks exist.",
)
g = out["groups"]
out["reconciled_to_registry"] = (
    g.get("negative_established", {}).get("runs") == out["registry"]["established"]
)
save("caught-not-taught", out)
