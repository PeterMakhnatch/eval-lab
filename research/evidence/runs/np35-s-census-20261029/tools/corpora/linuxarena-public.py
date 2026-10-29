# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from e_common import *

ad = load_adapter("anonymouslinuxarena__linuxarena-public")


def groups(s):
    st = s["gold"]["state"]
    return ["negative_established", "all_negatives"] if st == "negative" else [st]


def keys(s):
    i = s["identity"]
    return {
        "primary": s_shim.task_hash(i["main_task_id"]),
        "environment_family": s_shim.task_hash(i["environment"]),
        "environment_x_main": s_shim.task_hash(i["environment"] + "/" + i["main_task_id"]),
    }


out = run(
    "linuxarena-public",
    "",
    groups,
    keys,
    lambda s: map_steps(s["_steps"]),
    ad.specs,
    "linuxarena-public",
    "/private/tmp/np35/scripts/linuxarena-public.py",
    [
        "Adapter _steps use role/function keys; mapped mechanically (role->source, function->function_name).",
        "Registry negatives = honest-mode (side_task null) runs, established by run condition (control_negatives=400); no negative_unestablished group.",
        "Adapter yields deduplicated trajectories (566); 6 excluded are not yielded as specs unless gold.state says so.",
    ],
    "identity.main_task_id (native) -> s_shim.task_hash. Readings: environment (family, proxy) and environment x main_task_id. main_task_id may repeat across environments, so the primary key is a possible undercount; environment_x_main is the safer reading.",
)
out["reconciled_to_registry"] = (
    out["groups"].get("negative_established", {}).get("runs") == out["registry"]["established"]
)
save("linuxarena-public", out)
