"""CAS/raw-ATIF-backed redacted hydration API for cited trajectory content.

Re-exports core hydration models and functions from evallab.interpretation.trajectory_hydration.
"""

from __future__ import annotations

from evallab.interpretation.trajectory_hydration import (
    CitationHandle,
    CitationPathJailError,
    CitationTarget,
    HydratedEvidence,
    RedactionPolicy,
    apply_redaction,
    create_citation_handle,
    hydrate_citation,
    hydrate_error_observations,
    hydrate_step_details,
)

__all__ = [
    "CitationHandle",
    "CitationPathJailError",
    "CitationTarget",
    "HydratedEvidence",
    "RedactionPolicy",
    "apply_redaction",
    "create_citation_handle",
    "hydrate_citation",
    "hydrate_error_observations",
    "hydrate_step_details",
]
