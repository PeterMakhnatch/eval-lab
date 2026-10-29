# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from e_common import *

ad = load_adapter("AISafety-Student__knowing-collusion-filter")


def groups(s):
    st = s["gold"]["label_state"]
    return ["negative_established", "all_negatives"] if st == "negative" else [st]


def keys(s):
    g = s["gold"]
    first_user = next((x.get("message", "") for x in s["_steps"] if x.get("source") == "user"), "")
    return {
        "primary": s_shim.task_hash(g["scenario"]),
        "first_user_prompt_sha": s_shim.task_hash(first_user),
    }


out = run(
    "knowing-collusion-filter",
    "",
    groups,
    keys,
    lambda s: map_steps(s["_steps"]),
    ad.iter_specs,
    "knowing-collusion-filter",
    "/private/tmp/np35/scripts/knowing-collusion-filter.py",
    [
        "Adapter tool_calls use name/arguments(str); mapped mechanically (name->function_name).",
        "Tools are send_message, get_record, submit_decision (two-agent chat): no file-editing tool exists, so S is structurally unreachable.",
        "Registry negatives = confession-backed, never-blocked helpers (20 established).",
    ],
    "gold.scenario (native scenario id; a PROXY: scenarios are reused across episodes/models, no per-task id) -> s_shim.task_hash. Second reading sha of first user prompt.",
)
out["reconciled_to_registry"] = (
    out["groups"].get("negative_established", {}).get("runs") == out["registry"]["established"]
)
save("knowing-collusion-filter", out)
