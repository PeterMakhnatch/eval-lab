"""Gated dimension: raw reward times integrity (exact product).

All-pass over ``reward`` and ``integrity`` would binarize a non-binary raw
reward; this criterion computes the product explicitly so ``reward_gated``
stays exactly ``reward x integrity`` for any raw value. Stored MiMo raw
rewards are binary (0/1), where both formulations agree.
"""

import sys
from pathlib import Path

from rewardkit import criterion  # ty: ignore[unresolved-import]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

import integrity_core as core  # ty: ignore[unresolved-import]


@criterion(description="reward_gated: raw reward x integrity (exact product)")
def reward_gated(workspace: Path) -> dict:
    result = core.evaluate(workspace=workspace)
    gated = result["reward_gated"]
    return {
        "score": gated,
        "reasoning": "{} x {} = {}".format(result["reward"], result["integrity"], gated),
    }
