"""Run-level lists behind each Trace Tools row: which runs a tool hit, read clean, or did not read.

Trace ids and the task each belongs to, for *admitted DEV* runs only. There are no labels,
rewards or outcomes here: whether a tool hit a run is the tool's own recorded flag, and
"read clean" means the tool read the run and flagged nothing, which is not a verdict that
the run is clean. A run a tool has no result for (or only abstained on) is "not read": that
is neither a miss nor a clean pass. Whether a hit was *right* needs labels, which stay local.

``facets`` is the Run Atlas facet reader: a hit is a stored facet with suspicion of at
least 2 (the scorecard's ``suspicion>=2`` headline), read clean is a stored facet below 2,
and not read is an admitted run with no stored facet.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from evallab.production.task_evidence import ToolFindingIndex

SCHEMA = "evallab.tool_runs/v1"
KINDS = ("hit", "clean", "unread")
MAX_PAGE = 200
FACET_TOOL = "facets"
FACET_HIT_DETECTOR = "suspicion>=2"
FACET_HIT_MIN = 2
# Tools whose runs are retained per run. The rule cascade and `harbor analyze` keep counts only.
RESULT_TOOLS = ("laminar", "docent", "scout", "scout_metr", "omp")
RUN_LIST_TOOLS = (FACET_TOOL, *RESULT_TOOLS)


def _facet_partition(
    admitted: Collection[str], shown: Mapping[str, Mapping[str, Any]], facets_n: int
) -> dict[str, Any]:
    hit: dict[str, list[dict[str, Any]]] = {}
    clean: list[str] = []
    unread: list[str] = []
    for run in sorted(admitted):
        facet = shown.get(run)
        if facet is None:
            unread.append(run)
        elif facet["suspicion"] >= FACET_HIT_MIN:
            hit[run] = [
                {
                    "detector": FACET_HIT_DETECTOR,
                    "evidence_steps": list(facet["evidence_steps"])[:5],
                }
            ]
        else:
            clean.append(run)
    return {"hit": hit, "clean": clean, "unread": unread, "outside_n": facets_n - len(shown)}


def tool_run_page(
    tool: str,
    kind: str,
    *,
    traces: Mapping[str, Mapping[str, Any]],
    shown: Mapping[str, Mapping[str, Any]],
    facets_n: int,
    findings: ToolFindingIndex,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """One page of one kind of run for one tool, with the counts of all three kinds."""
    if tool not in RUN_LIST_TOOLS:
        raise ValueError(f"no per-run list is retained for {tool!r}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    if not 1 <= limit <= MAX_PAGE or offset < 0:
        raise ValueError(f"limit must be 1..{MAX_PAGE} and offset non-negative")
    split = (
        _facet_partition(traces, shown, facets_n)
        if tool == FACET_TOOL
        else findings.partition(tool, traces)
    )
    hits = split["hit"]
    ordered = sorted(hits, key=lambda run: (-len(hits[run]), run)) if kind == "hit" else split[kind]
    page = ordered[offset : offset + limit]
    return {
        "schema": SCHEMA,
        "tool": tool,
        "kind": kind,
        "counts": {
            "admitted_n": len(traces),
            "hit_n": len(hits),
            "clean_n": len(split["clean"]),
            "unread_n": len(split["unread"]),
            "outside_n": split["outside_n"],
        },
        "total": len(ordered),
        "limit": limit,
        "offset": offset,
        "runs": [
            {
                "run_id": run,
                "task_id": traces[run]["task_id"],
                "faceted": run in shown,
                "detectors": hits.get(run, []) if kind == "hit" else [],
            }
            for run in page
        ],
        "results_error": None if tool == FACET_TOOL else findings.error,
        "notice": (
            "Admitted DEV runs only, by trace id. Hit is the tool's own recorded flag; read clean "
            "means it read the run and flagged nothing, which is not a verdict; not read means "
            "no result or only abstentions, which is neither a miss nor a clean pass. Whether a "
            "hit was right needs labels, which are never sent to the browser."
        ),
    }
