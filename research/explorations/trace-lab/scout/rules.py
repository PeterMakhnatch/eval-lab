"""Deterministic trace rules for Harbor runs (Trace Lab, HAR-109).

This module is the single rule implementation shared by Inspect Scout
scanners (``scanners.py``) and, via the handoff note, Engineering's
``process-job``. It does NOT look up precomputed labels: every function
below calls probe-03's ``capabilities.py`` rule functions on Eval Lab step
records, so Scout and ``process-job`` compute the same values from the
same inputs.

Inputs (Eval Lab records, all read-only, per trial directory):
- ``result.json`` (task name, reward, exception, agent token totals),
- ``config.json`` (trial/job names),
- ``agent/trajectory.json`` + ``agent/trajectory.cont-*.json`` (steps),
- ``verifier/`` (submit-contract verdict, test stdout),
- ``trial.log`` (summarization livelock),
- ``../lab-metadata.json`` (trial-proxy ceilings for ``ceiling_which``).

Outputs: plain dicts with the same ``rule_id`` / ``tag`` / ``attribution``
vocabulary as ``probe-03-capabilities/har81/capabilities.jsonl``. Evidence
is cited as probe-03 step refs (``head#12``,
``trajectory.cont-1.json#30``); Scout scanners map those refs to message
cites.

$0: standard library plus the worktree's ``evallab`` package (for the
normalizer import path only) and probe-03's ``capabilities.py``. No model
calls, no launches, no uploads, no publishing.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACE_LAB = HERE.parent
CAP_DIR = TRACE_LAB / "probe-03-capabilities"
if str(CAP_DIR) not in sys.path:
    sys.path.insert(0, str(CAP_DIR))
import capabilities as cap  # noqa: E402  (probe-03 rule implementation)

# Default Eval Lab sources, in preference order. HAR-81 labels were built
# with the dispatch-531 worktree's normalizer; using the same source keeps
# rejection-cause classification (R-TOOL-01 vs R-TOOL-02) identical. New
# runs should pass their own commit's src explicitly.
_DEFAULT_EVALLAB_SRCS = [
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/src"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/src"),
]
_DEFAULT_NOP_DIRS = [
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/mimo-ops/runs"),
]

_NORM_CACHE: dict[str, tuple[dict, object]] = {}
_RESULT_CACHE: dict[str, dict] = {}


def default_evallab_src() -> str | None:
    """Newest available HAR-81 normalizer source, else None (fallback)."""
    for candidate in _DEFAULT_EVALLAB_SRCS:
        if (candidate / "evallab" / "mimo_tool_calls.py").is_file():
            return str(candidate)
    return None


def default_nop_runs_dir() -> str | None:
    """Nop/qual control tree for the R-ENV-02 cross-check, if present."""
    for candidate in _DEFAULT_NOP_DIRS:
        if candidate.is_dir():
            return str(candidate)
    return None


def get_normalizer(evallab_src: str | None = None) -> tuple[dict, object]:
    """(provenance, normalize_fn), cached per source.

    ``None`` resolves to :func:`default_evallab_src`; an unavailable
    source falls back to probe-03's explicit rules (``mode:
    fallback-explicit-rules``), recorded in provenance.
    """
    key = evallab_src or "__default__"
    cached = _NORM_CACHE.get(key)
    if cached is not None:
        return cached
    src = evallab_src or default_evallab_src()
    provenance, normalize_fn = cap.load_normalizer(src)
    _NORM_CACHE[key] = (provenance, normalize_fn)
    return provenance, normalize_fn


def analyze_trial_rules(
    trial_dir: str | Path,
    *,
    evallab_src: str | None = None,
    nop_runs_dir: str | None = None,
) -> dict:
    """Compute the rule dimensions for one Eval Lab trial directory.

    Calls ``capabilities.analyze_trial`` (the same code that wrote
    ``har81/capabilities.jsonl``) with no HAR-93 joins, and returns the
    rule-relevant subset: task/verifier/stop, first failure, outcome
    failure, completion handshake, loop cost, and wedge. Treatment keys,
    learnability, and reading-sheet fields are intentionally omitted;
    they are not trace rules.

    Results are cached per trial directory within the process.
    """
    trial = Path(trial_dir).resolve()
    cache_key = str(trial)
    cached = _RESULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    provenance, normalize_fn = get_normalizer(evallab_src)
    previous_nop = cap.NOP_RUNS_DIR
    nop = nop_runs_dir if nop_runs_dir is not None else default_nop_runs_dir()
    cap.NOP_RUNS_DIR = nop
    try:
        row = cap.analyze_trial(
            trial,
            dict(provenance),
            normalize_fn,
            None,  # har93_treatment: treatment keys are not trace rules
            None,  # har93_capture: token share is reported, not ruled on
            None,  # har93_source
        )
    finally:
        cap.NOP_RUNS_DIR = previous_nop

    out = {
        "trial": row.get("trial"),
        "trial_dir": row.get("trial_dir"),
        "job_dir": row.get("job_dir"),
        "task": row.get("task"),
        "verifier": row.get("verifier"),
        "stop": row.get("stop"),
        "first_failure": row.get("first_failure"),
        "outcome": row.get("outcome_relevant_failure"),
        "handshake": row.get("completion_handshake"),
        "loops": row.get("loop_cost"),
        "wedge": row.get("wedge"),
        "confirmation_loop": row.get("confirmation_loop"),
        "normalizer": {
            "mode": provenance.get("mode"),
            "sha256": (provenance.get("normalizer_sha256") or "")[:12],
            "evallab_commit": provenance.get("evallab_commit"),
        },
        "nop_runs_dir": nop,
    }
    _RESULT_CACHE[cache_key] = out
    return out


def clear_cache() -> None:
    """Drop cached normalizers and trial results (tests only)."""
    _NORM_CACHE.clear()
    _RESULT_CACHE.clear()
