# ruff: noqa
# NP35-LB-1 per-corpus census script (scratch style, read-only, aggregates only); see ../../README.md.
"""Shared helpers for NP35 worker A (aggregates only; never prints cells)."""

import json, os, sys

os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, "/private/tmp/np35")
import s_shim  # noqa: E402,F401

OUT = "/private/tmp/np35/w-a.json"
ADAPTERS = "/private/tmp/np35/research/evidence/runs"


def pooled(report, names):
    """Sum S/runs over several groups' report rows (distinct-task union is done by caller via extra Tally)."""
    return {
        k: sum(report.get(n, {}).get(k, 0) for n in names)
        for k in (
            "runs",
            "S",
            "SI",
            "L",
            "L_not_S",
            "runs_without_any_tool_call",
            "unresolved_editor_calls",
        )
    }


def save(corpus_id, entry):
    data = json.load(open(OUT)) if os.path.exists(OUT) else {"corpora": {}}
    data["corpora"][corpus_id] = entry
    json.dump(data, open(OUT, "w"), indent=1, sort_keys=True)
    print(
        corpus_id,
        json.dumps(
            {g: (v["runs"], v["S"], v["distinct_tasks_S"]) for g, v in entry["groups"].items()}
        ),
    )
