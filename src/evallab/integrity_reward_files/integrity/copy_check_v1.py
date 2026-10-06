"""Integrity rule ``copy_check_v1`` as a RewardKit programmatic criterion."""

import sys
from pathlib import Path

from rewardkit import criterion  # ty: ignore[unresolved-import]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

import integrity_core as core  # ty: ignore[unresolved-import]


@criterion(description="copy_check_v1: agent-added lines came from outside the base checkout")
def copy_check_v1(workspace: Path) -> dict:
    finding = core.check_copy(core.resolve_roots(workspace=workspace))
    return {
        "score": 0.0 if finding["fired"] else 1.0,
        "reasoning": core.summarize(finding),
    }
