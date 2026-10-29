# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from wc_common import *


def cls(s):
    st = s["labels"]["state"]
    return {"positive": "positive", "negative": "negative_established", "excluded": "excluded"}.get(
        st, "ambiguous"
    )


run(
    "researcharena-benchmark-gaming",
    "corpus-expansion-pq2-20261028/corpora/aisa-group__researcharena-trajectories",
    cls,
    lambda s: {
        "task_id": s["identity"]["task_id"],
        "task_config": s["identity"].get("task_config"),
        "task_id+task_config": f"{s['identity']['task_id']}|{s['identity'].get('task_config')}",
    },
    lambda s: s["_steps"],
    task_key_text="identity.task_id (native setting id, proxy: one value per setting family) -> s_shim.task_hash; alternate readings task_config and task_id+task_config reported.",
    extra_notes=[
        "_steps already carry source=agent and tool_calls[{function_name,arguments(dict)}]; used unchanged.",
        "specs() default (include_excluded=False) yields only the 41 staged core runs; the 349 excluded rows the adapter can list carry no steps and are not in the bundle, so excluded=0 here.",
        "Negatives are no-side-task baselines by construction, so all are established per registry; positives are PROMPTED benchmark-gaming attacks.",
    ],
)
