"""Clustered / repeated-measure design-effect sizing contract.

Public surface for the fail-closed clustered power-planning contract implemented
in ``evallab.cohort``. Campaign sizing refuses to quote a clustered ``n`` without
an explicit ICC (rho) and cluster-size declaration, and never presents repeated
measures within a cluster as independent trials. ``rho == 0`` keeps the
independent (unclustered) plan unchanged.
"""

from __future__ import annotations

from evallab.analysis_statistics import (
    AnalysisStatus,
    RefusalCode,
    RepeatCellInput,
    RepeatHeterogeneityReport,
    analyze_repeat_heterogeneity,
    compute_design_effect,
)
from evallab.cohort import (
    clustered_minimum_detectable_effect,
    clustered_power_requirements,
    clustered_required_tasks_for_effect,
    design_effect,
    effective_sample_size,
)

__all__ = [
    "AnalysisStatus",
    "RefusalCode",
    "RepeatCellInput",
    "RepeatHeterogeneityReport",
    "analyze_repeat_heterogeneity",
    "clustered_minimum_detectable_effect",
    "clustered_power_requirements",
    "clustered_required_tasks_for_effect",
    "compute_design_effect",
    "design_effect",
    "effective_sample_size",
]
