# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from _np35b import *


def classify(s):
    st = s["labels"]["state"]
    if st == "negative":
        return (
            "negative_established"
            if s["labels"]["negative_class"] == "fixed"
            else "negative_unestablished"
        )
    return st


def keys(s):
    i = s["identity"]
    return {
        "primary": "precommit_hook",
        "cluster_arm_x_errors": json.dumps(i["bootstrap_cluster"]),
        "model_arm": i["model_arm"],
        "target_errors": str(i["target_errors"]),
    }


run(
    "noahrossi__sdf-shortcut-taking",
    "sdf-shortcut-taking",
    {"positives": 359, "negatives": 636, "established": 331},
    classify,
    keys,
    "No native task id: the whole corpus is one precommit_hook task, so the key is the constant 'precommit_hook' (1 distinct). Proxy readings also reported: bootstrap cluster (model arm x seeded error count), model arm, seeded error count. The proxies are not tasks.",
    [
        "established negative = labels.negative_class 'fixed' (registry 331); unestablished = 'no_commit' (305); excluded 224 included via adapter specs (all rows yielded).",
        "Steps used as-is: tool messages and assistant messages both source 'agent'; tool calls are execute_command with dict/str arguments from the corpus's own call fields.",
    ],
    "/private/tmp/np35/scripts/sdf-shortcut-taking.py",
    "sdf",
)
