"""Read-only, admitted trace payloads for the private trace stores.

Selection shares the blind reader's exact DEV/cross-key gate and native actor
provenance boundary. Verified CAS observations preserve exact actor bytes;
control/event journals are not reconstructed as conversations.
"""

from __future__ import annotations

import re
from uuid import NAMESPACE_URL, uuid5

GOODFIRE_DATASET = "goodfire-reward-hack-data"
ALLOWED_DATASETS = frozenset({
    GOODFIRE_DATASET, "mimo-defect-map-v1", "tb2-to-tb21-v1", "tb4-audit-v1",
})
METADATA_FIELDS = frozenset({
    "run_id", "source", "benchmark", "model", "task_id", "outcome", "split",
})
_OWN_RUNS = {
    "mimo-defect-map-v1": frozenset({"mimo30", "muse-r1", "muse-r2", "muse-w5"}),
    "tb2-to-tb21-v1": frozenset({"tb2-to-tb21"}),
    "tb4-audit-v1": frozenset({"kimi-tb4", "muse-r1"}),
}
_CAS_URI = re.compile(r"cas://sha256/([0-9a-f]{64})\Z")


def trace_uuid(trace_id: str) -> str:
    """The same deterministic remote run identity for every target."""
    return str(uuid5(NAMESPACE_URL, "evallab-trace-store/v1:" + trace_id))


