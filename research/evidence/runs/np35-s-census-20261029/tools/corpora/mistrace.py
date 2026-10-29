# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from wc_common import *


def cls(s):
    st = s["gold"]["label_state"]
    return {
        "positive_success": "positive",
        "positive_attempt": "positive",
        "negative": "negative_unestablished",
        "excluded": "excluded",
    }.get(st, "ambiguous")


run(
    "mistrace-reward-hacking",
    "corpus-expansion-pq2-20261028/corpora/ameek__mistrace",
    cls,
    lambda s: {"task_id": s["identity"]["task_id"]},
    lambda s: s["_steps"],
    task_key_text="identity.task_id (native ImpossibleBench LiveCodeBench problem id) -> s_shim.task_hash; exact task id, not a proxy.",
    extra_notes=[
        "_steps source in {agent,user}; tool_calls already {function_name,arguments}; editor tool is text_editor (in NP29 EDITOR_TOOLS).",
        "Negatives = both channels false (registry: not certified clean); established_negatives=0 so every negative is negative_unestablished.",
    ],
)
