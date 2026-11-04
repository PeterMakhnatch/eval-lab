"""Measure the opt-in history signal on frozen DEV views, without model calls.

This uses the same label-free views as cached J3. Missing final diffs abstain.
It does not claim access to omitted retained terminal panes or hidden spills.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

from evallab import hack_drop as hd
from evallab.line.copy_history import history_copy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent.parent / "ov1104-detbench/detbench_lib.py"
    spec = importlib.util.spec_from_file_location("outcome_detbench_views", source)
    assert spec and spec.loader
    views = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(views)
    runs = [json.loads(line) for line in args.runs.read_text().splitlines() if line.strip()]
    dev = [r for r in runs if r["split"] == "dev"]
    if len({r["run_key"] for r in dev}) != len(dev):
        raise ValueError("Duplicate DEV run identity")
    results = []
    for row in dev:
        path = Path(row["view"])
        view = json.loads(path.read_text())
        if view["run_key"] != row["run_key"]:
            raise ValueError("Frozen view identity mismatch")
        diff = view.get("final", {}).get("diff")
        reason = None if isinstance(diff, str) and diff.strip() else "final_diff_unavailable"
        flag = None
        if reason is None:
            rollout = hd.RolloutItem(
                row["run_key"],
                row["cluster"],
                run=hd.atif_run(views.view_to_atif(view), str(path)),
                diff_text=diff,
            )
            flag = history_copy(rollout)
        results.append(
            {
                "run_key": row["run_key"],
                "score": None if reason else float(flag is not None),
                "abstain": reason,
                "evidence": flag,
                "view_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in results))
    print(
        json.dumps(
            {
                "rows": len(results),
                "scored": sum(r["score"] is not None for r in results),
                "positive": sum(r["score"] == 1 for r in results),
                "abstentions": dict(Counter(r["abstain"] for r in results if r["abstain"])),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
