"""Deterministic final-state verifier for the official upstream AB task."""
from __future__ import annotations
import csv, json
from pathlib import Path

def _rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as fh: return list(csv.reader(fh))

def verify(task_dir: Path, workspace: Path | None = None) -> dict:
    task_dir = task_dir.resolve(); workspace = (workspace or task_dir / "agent_workspace").resolve()
    expected = _rows(task_dir / "files" / "expected_record.csv")
    actual_path = workspace / "record.csv"
    actual = _rows(actual_path) if actual_path.exists() else []
    assertions = {"record_exists": actual_path.exists(), "record_matches_upstream_oracle": actual == expected}
    manifest = json.loads((task_dir / "state_manifest.json").read_text(encoding="utf-8"))
    assertions["state_is_nonempty"] = manifest["row_count"] > 0 and manifest["padding_only"] is False
    if expected and len(expected[-1]) >= 3:
        b_wins = float(expected[-1][2].rstrip("%")) > float(expected[-1][1].rstrip("%"))
        marker = workspace / "promo-assets-for-b.marker"
        assertions["required_bucket_marker"] = marker.exists() if b_wins else True
    else: assertions["required_bucket_marker"] = False
    assertions["all_assertions"] = all(assertions.values())
    return {"reward": 1.0 if assertions["all_assertions"] else 0.0, "assertions": assertions, "state_digest": manifest["state_digest"]}

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--task-dir", type=Path, required=True); p.add_argument("--workspace", type=Path); args = p.parse_args()
    result = verify(args.task_dir, args.workspace); print(json.dumps(result, sort_keys=True)); raise SystemExit(0 if result["reward"] == 1 else 1)
