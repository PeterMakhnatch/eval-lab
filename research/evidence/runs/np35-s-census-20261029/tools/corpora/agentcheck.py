# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
import sys

sys.path.insert(0, "/private/tmp/np35/scripts")
from wc_common import *


def cls(s):
    st = s["labels"]["label_state"]
    return {
        "positive": "positive",
        "negative": "negative_unestablished",
        "excluded": "excluded",
    }.get(st, "ambiguous")


def steps_of(s):
    """ONE mechanical mapping: role->source, LangChain tool_call {name,args} -> {function_name,arguments}."""
    return [
        {
            "source": st["role"],
            "tool_calls": [
                {"function_name": c["name"], "arguments": c["args"]} for c in st["tool_calls"]
            ],
        }
        for st in s["_steps"]
    ]


run(
    "agentcheck",
    "corpus-expansion-pq4-20261028/corpora/aminembarek2__agentcheck",
    cls,
    lambda s: {"task_id": s["identity"]["task_id"]},
    steps_of,
    task_key_text="identity.task_id (native, 3 dependency-migration repo tasks) -> s_shim.task_hash; only 3 values, so a task family in effect.",
    extra_notes=[
        "Mapping: step.role->source (assistant kept; system/user/tool_result/agent final_diff carry no calls), tool_call.name->function_name, tool_call.args->arguments.",
        "Labels are rule output (agentcheck/scorer.py); its detector flags cheats, so S vs positives is partly circular. established_negatives=0.",
    ],
)
