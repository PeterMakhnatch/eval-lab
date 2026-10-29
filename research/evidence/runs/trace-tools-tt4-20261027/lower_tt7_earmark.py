"""One-off: lower TT7's immutable cap/earmark in the live shared budget state (35.00 -> 33.50).

TT-7 is parked; owner Trace Harness w1C:p0 (Parent decision, 2026-10-28). The live state is
validated against ``openrouter_budget.CAPS``, so the cap, the stored arm cap and the integrity
digest change together under the state lock; a byte-identical backup is kept beside it.
Refuses unless TT7 has no attempts and the stored caps are exactly the expected old ones.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import openrouter_budget as budget

OLD, NEW = "35.00", "33.50"


def main(state_dir: Path = budget.DEFAULT_STATE_DIR) -> None:
    manager = budget.Budget(state_dir, "tt4")
    with manager._locked() as descriptor:
        path = state_dir / budget.STATE_FILE
        state = json.loads(path.read_text())
        if state["caps"]["tt7"] == NEW and state["arms"]["tt7"]["cap_usd"] == NEW:
            print("already lowered")
            return
        if state["caps"] != {"tt4": "14.85", "tt7": OLD} or state["arms"]["tt7"]["cap_usd"] != OLD:
            sys.exit("unexpected stored caps; refusing")
        if any(record["arm"] == "tt7" for record in state["attempts"].values()):
            sys.exit("TT7 has attempts; refusing")
        if state["integrity"]["sha256"] != budget.state_digest(state):
            sys.exit("state integrity digest does not verify; refusing")
        shutil.copy2(path, path.with_name(f"{budget.STATE_FILE}.before-tt7-earmark-{NEW}"))
        state["caps"]["tt7"] = state["arms"]["tt7"]["cap_usd"] = NEW
        state["seq"] += 1
        manager._write(state, descriptor)
    print(f"TT7 cap {OLD} -> {NEW}")


if __name__ == "__main__":
    main()
