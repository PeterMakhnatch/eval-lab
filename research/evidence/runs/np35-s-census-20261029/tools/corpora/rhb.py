# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from wc_common import *


def cls(s):
    st = s["labels"]["state"]
    return {
        "positive": "positive",
        "negative": "negative_unestablished",
        "excluded": "excluded",
    }.get(st, "ambiguous")


run(
    "rhb-delivery-queue",
    "corpus-expansion-pq2-20261028/corpora/joesbobclaw__reward-hacking-benchmark",
    cls,
    lambda s: {
        "scenario_sha256(identity.task_id)": s["identity"]["task_id"],
        "cluster_key(study_family/scenario)": s["identity"]["cluster_key"],
        "study": s["identity"]["study"],
    },
    lambda s: s["_steps"],
    task_key_text="identity.task_id = scenario sha256 prefix (native scenario id, one per simulated delivery scenario) -> s_shim.task_hash; proxies reported: cluster_key, study.",
    extra_notes=[
        "Environment tools are deliver/finish etc. in a simulated delivery queue: no file/shell editor, so S is structurally unable to fire; zero S is a property of the environment, not evidence about clean test editors.",
        "established_negatives=0 in registry; all negatives unestablished.",
    ],
)
