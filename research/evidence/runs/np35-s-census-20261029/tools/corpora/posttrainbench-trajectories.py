# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from e_common import *

ad = load_adapter("aisa-group__PostTrainBench-Trajectories")
cache = {}


def spec_iter():
    # Replay only non-excluded, non-duplicate rows (as adapter.scan does); finalize() gives the post-replay primary state.
    for sp in ad.specs():
        st, rs = sp["labels"]["state"], sp["labels"]["reason"]
        if st == "excluded" or sp["_trace_path"] is None:
            sp["_final"] = "excluded"
            sp["_steps_"] = None
        else:
            steps, _ = ad.replay(sp["_trace_path"])
            fin, _, _ = ad.finalize(sp, steps)
            sp["_final"] = fin
            sp["_steps_"] = None if fin == "excluded" else steps
        yield sp


def groups(s):
    f = s["_final"]
    return ["negative_unestablished", "all_negatives"] if f == "negative" else [f]


def keys(s):
    i = s["identity"]
    return {
        "primary": s_shim.task_hash(i["task_id"]),
        "benchmark_family": s_shim.task_hash(i["benchmark"]),
        "task_id_hashed_by_adapter": i["task_key"],
    }


out = run(
    "posttrainbench-trajectories",
    "",
    groups,
    keys,
    lambda s: s["_steps_"],
    spec_iter,
    "posttrainbench-trajectories",
    "/private/tmp/np35/scripts/posttrainbench-trajectories.py",
    [
        "Adapter replay() already emits ATIF steps (source=agent, tool_calls[{function_name, arguments}]); no mapping applied.",
        "Registry negative_basis=no_flag_fired, established_negatives=0: ALL negatives are negative_unestablished; negative_established group is empty by design. Registry negatives=1439 are pre-finalize (strict-label) counts; post-replay (finalize) states are reported here and may differ.",
        "Rows with state excluded (duplicates, no trace, empty/verdict-visible) are counted but not analysed for S.",
        "Task key: identity.task_id = '<benchmark>_<base_model>' (benchmark x model); a proxy: tasks are benchmark/model pairs, not per-prompt tasks.",
    ],
    "identity.task_id (native '<benchmark>_<base model>') -> s_shim.task_hash. Readings: benchmark_family (benchmark only). Both are proxies; no per-instance task id exists.",
)
out["registry"]["negatives_note"] = "registry negatives 1439 / established 0"
out["reconciled_to_registry"] = (
    out["groups"].get("negative_unestablished", {}).get("runs") == out["registry"]["negatives"]
)
out["established_group_runs"] = 0
save("posttrainbench-trajectories", out)
