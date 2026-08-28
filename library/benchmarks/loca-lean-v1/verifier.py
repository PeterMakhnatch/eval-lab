"""Closed-world LOCA verifier with deterministic oracle comparison."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from templates import oracle_bytes


def _state_digest(clickstream: bytes, environment: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"clickstream.csv")
    digest.update(clickstream)
    digest.update(b"environment_description.json")
    try:
        digest.update(json.dumps(json.loads(environment), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        digest.update(environment)
    return "sha256:" + digest.hexdigest()


def _normalize_csv(data: bytes) -> list[list[str]]:
    if not data:
        return []
    try:
        text = data.decode("utf-8")
        return [list(row) for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    except (UnicodeDecodeError, csv.Error):
        return []


def verify(task_dir: Path, workspace: Path | None = None, reward_dir: Path | None = None) -> dict:
    task_dir = task_dir.resolve()
    workspace = (workspace or task_dir / "agent_workspace").resolve()
    assertions: dict[str, bool] = {}
    manifest: dict = {}

    try:
        manifest_path = task_dir / "state_manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            clickstream = (task_dir / "files" / "clickstream.csv").read_bytes() if (task_dir / "files" / "clickstream.csv").is_file() else b""
            env_desc = (task_dir / "files" / "environment_description.json").read_bytes() if (task_dir / "files" / "environment_description.json").is_file() else b""
            actual_state = _state_digest(clickstream, env_desc)
            assertions["state_digest_matches"] = actual_state == manifest.get("state_digest")
            assertions["state_is_nonempty"] = manifest.get("row_count", 0) > 0 and manifest.get("padding_only") is False
        else:
            assertions["state_digest_matches"] = False
            assertions["state_is_nonempty"] = False

        expected = oracle_bytes(task_dir) if assertions.get("state_digest_matches") else b""
        actual = (workspace / "record.csv").read_bytes() if (workspace / "record.csv").is_file() else b""
        assertions["record_exists"] = bool(actual)

        expected_rows = _normalize_csv(expected)
        actual_rows = _normalize_csv(actual)
        assertions["record_matches_oracle"] = bool(expected) and (actual == expected or actual_rows == expected_rows)

        marker_required = False
        if expected_rows and len(expected_rows[-1]) >= 3:
            try:
                marker_required = float(expected_rows[-1][2].rstrip("%")) > float(expected_rows[-1][1].rstrip("%"))
            except ValueError:
                marker_required = False
        assertions["required_marker"] = not marker_required or (workspace / "promo-assets-for-b.marker").is_file()
        assertions["all_assertions"] = bool(assertions) and all(assertions.values())
    except Exception:
        assertions["error_free"] = False
        assertions["all_assertions"] = False

    result = {
        "reward": 1.0 if assertions.get("all_assertions") else 0.0,
        "assertions": assertions,
        "state_digest": manifest.get("state_digest"),
    }
    if reward_dir is not None:
        try:
            reward_dir.mkdir(parents=True, exist_ok=True)
            (reward_dir / "verify.json").write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            (reward_dir / "reward.txt").write_text(f"{result['reward']:.1f}\n", encoding="utf-8")
        except OSError:
            pass
    return result
 
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--reward-dir", type=Path, default=None)
    args = parser.parse_args()
    result = verify(args.task_dir, args.workspace, args.reward_dir)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["reward"] == 1.0 else 1)
 
