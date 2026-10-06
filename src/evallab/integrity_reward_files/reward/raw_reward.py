"""Raw-reward dimension: the task's own grading result, unchanged."""

import sys
from pathlib import Path

from rewardkit import criterion  # ty: ignore[unresolved-import]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

import integrity_core as core  # ty: ignore[unresolved-import]


@criterion(description="raw hidden-test result from verifier/reward.txt")
def raw_reward(workspace: Path) -> dict:
    roots = core.resolve_roots(workspace=workspace)
    raw = core.read_raw_reward(roots)
    if raw is None:
        return {
            "score": 0.0,
            "reasoning": "unscored by the grader; gated to 0.0, not a measured fail",
        }
    return {"score": raw, "reasoning": f"reward.txt={raw}"}
