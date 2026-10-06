"""Integrity rule ``out_of_base_read`` as a RewardKit programmatic criterion."""

import sys
from pathlib import Path

from rewardkit import criterion  # ty: ignore[unresolved-import]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

import integrity_core as core  # ty: ignore[unresolved-import]


@criterion(description="out_of_base_read: out-of-base read followed by matching lines")
def out_of_base_read(workspace: Path) -> dict:
    roots = core.resolve_roots(workspace=workspace)
    calls, observations = core.step_calls(roots)
    finding = core.check_out_of_base_read(calls, observations, roots)
    return {
        "score": 0.0 if finding["fired"] else 1.0,
        "reasoning": core.summarize(finding),
    }
