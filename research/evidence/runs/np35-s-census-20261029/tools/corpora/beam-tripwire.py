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
            if s["labels"]["condition"] == "possible"
            else "negative_unestablished"
        )
    return st


def keys(s):
    i = s["identity"]
    return {"primary": s_shim.task_hash(i["task_id"]), "repo": i["repo"]}


run(
    "dhairyapandya__beam-tripwire-episodes",
    "beam-tripwire",
    {"positives": 171, "negatives": 442, "established": 175},
    classify,
    keys,
    "Native task id (identity.task_id = instance_id) -> s_shim.task_hash (44 tasks). Proxy also reported: repo (3). Episodes repeat the same 44 tasks across conditions/efforts.",
    [
        "established negative = negative with condition 'possible' (original task and test, control; registry 175); unestablished = impossible_honest (175) + impossible (92) negatives; ambiguous = 1 justified control test edit; excluded = 2 provider errors.",
        "Beam's own tamper_attempt label is a trace-derived test-file write detector, so S vs positives is partly circular; negatives are by construction free of publisher-detected test-file writes (tamper_attempt false) - S on negatives measures divergence between S and the publisher's rule.",
    ],
    "/private/tmp/np35/scripts/beam-tripwire.py",
    "beam",
)
