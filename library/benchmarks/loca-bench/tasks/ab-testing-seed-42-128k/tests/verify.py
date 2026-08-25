"""Deterministic final-state verifier for the official upstream AB task."""
from __future__ import annotations
import csv, json, sys
from pathlib import Path


def _rows(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.reader(fh))


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def verify(
    task_dir: Path | None = None,
    workspace: Path | None = None,
    golden_dir: Path | None = None,
) -> dict:
    """Verify the agent workspace against the verifier-only golden state.

    ``golden_dir`` contains ``expected_record.csv`` and a compact
    ``manifest.json``.  In the Harbor separate-verifier image the defaults
    point to the baked ``/tests/golden`` directory.
    """
    if workspace is None:
        workspace = (task_dir or Path("/app/task_state")) / "agent_workspace"
    workspace = workspace.resolve()

    if golden_dir is None:
        if task_dir is not None:
            candidate = task_dir / "tests" / "golden"
            golden_dir = candidate if (candidate / "expected_record.csv").exists() else task_dir / "golden"
        else:
            golden_dir = Path("/tests/golden")
    golden_dir = golden_dir.resolve()

    expected = _rows(golden_dir / "expected_record.csv")
    actual_path = workspace / "record.csv"
    actual = _rows(actual_path)

    assertions = {
        "record_exists": actual_path.exists(),
        "record_matches_upstream_oracle": actual == expected,
    }

    golden_manifest = _read_json(golden_dir / "manifest.json")
    if golden_manifest is not None:
        row_count = golden_manifest.get("row_count", 0)
        padding_only = golden_manifest.get("padding_only", True)
        state_digest = golden_manifest.get("state_digest")
    elif task_dir is not None:
        manifest = _read_json(task_dir / "state_manifest.json") or {}
        row_count = manifest.get("row_count", 0)
        padding_only = manifest.get("padding_only", True)
        state_digest = manifest.get("state_digest")
    else:
        row_count = 0
        padding_only = True
        state_digest = None

    assertions["state_is_nonempty"] = row_count > 0 and padding_only is False

    if expected and len(expected[-1]) >= 3:
        try:
            a_val = float(expected[-1][1].rstrip("%"))
            b_val = float(expected[-1][2].rstrip("%"))
            b_wins = b_val > a_val
        except ValueError:
            b_wins = False
        marker = workspace / "promo-assets-for-b.marker"
        assertions["required_bucket_marker"] = marker.exists() if b_wins else True
    else:
        assertions["required_bucket_marker"] = False

    assertions["all_assertions"] = all(assertions.values())
    return {
        "reward": 1.0 if assertions["all_assertions"] else 0.0,
        "assertions": assertions,
        "state_digest": state_digest,
    }


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--task-dir", type=Path, default=None)
    p.add_argument("--workspace", type=Path, default=None)
    p.add_argument("--golden-dir", type=Path, default=None)
    args = p.parse_args()
    result = verify(args.task_dir, args.workspace, args.golden_dir)
    print(json.dumps(result, sort_keys=True))
    sys.exit(0 if result["reward"] == 1.0 else 1)
