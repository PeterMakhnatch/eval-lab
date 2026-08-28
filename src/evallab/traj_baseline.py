"""Mechanical baseline facts, Screening heuristics, and Trace Baseline View (v_trace_baseline).

Re-exports core baseline models and functions from evallab.interpretation.traj_baseline.
"""

from __future__ import annotations

from evallab.interpretation.traj_baseline import (
    TRACE_BASELINE_PROVENANCE,
    BaselineProvenance,
    TraceBaselineRecord,
    compute_cbv_slope,
    compute_exit_code_cascade,
    compute_trace_baseline,
    create_trace_baseline_table,
    get_column_provenance,
)

__all__ = [
    "TRACE_BASELINE_PROVENANCE",
    "BaselineProvenance",
    "TraceBaselineRecord",
    "compute_cbv_slope",
    "compute_exit_code_cascade",
    "compute_trace_baseline",
    "create_trace_baseline_table",
    "get_column_provenance",
]
