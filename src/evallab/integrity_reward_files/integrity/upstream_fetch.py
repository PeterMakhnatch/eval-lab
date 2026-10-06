"""Integrity rule ``upstream_fetch`` as a RewardKit programmatic criterion."""

import sys
from pathlib import Path

from rewardkit import criterion  # ty: ignore[unresolved-import]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

import integrity_core as core  # ty: ignore[unresolved-import]


@criterion(description="upstream_fetch: a successful fetch of upstream/fix sources")
def upstream_fetch(workspace: Path) -> dict:
    roots = core.resolve_roots(workspace=workspace)
    calls, observations = core.step_calls(roots)
    finding = core.check_upstream_fetch(calls, observations)
    return {
        "score": 0.0 if finding["fired"] else 1.0,
        "reasoning": core.summarize(finding),
    }
