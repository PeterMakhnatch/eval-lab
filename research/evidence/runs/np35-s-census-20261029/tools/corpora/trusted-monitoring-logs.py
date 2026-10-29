# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from e_common import *

ad = load_adapter("dang-w__trusted-monitoring-logs")


def groups(s):
    st = s["labels"]["label_state"]
    return ["negative_established", "all_negatives"] if st == "negative" else [st]


def keys(s):
    return {
        "primary": s_shim.task_hash(s["identity"]["task_id"]),
        "task_text_sha": s_shim.task_hash(s["identity"]["task_text"]),
    }


out = run(
    "trusted-monitoring-logs",
    "",
    groups,
    keys,
    lambda s: map_steps(s["_steps"]),
    ad.specs,
    "trusted-monitoring-logs",
    "/private/tmp/np35/scripts/trusted-monitoring-logs.py",
    [
        "_steps (not _steps_code) used. Source values user/agent; tool calls use function/arguments, mapped mechanically.",
        "Tool set is submit / read_tool_output / grep_tool_output only: APPS one-shot code, no file-edit tools, so S is structurally unreachable (S not 'not applicable': structured calls exist but none can be a file write).",
        "Registry negatives = honest-condition runs in the 12288-budget honest set, established by design (control_negatives=200).",
    ],
    "identity.task_id (native APPS problem id) -> s_shim.task_hash; second reading sha of task_text.",
)
out["reconciled_to_registry"] = (
    out["groups"].get("negative_established", {}).get("runs") == out["registry"]["established"]
)
save("trusted-monitoring-logs", out)
