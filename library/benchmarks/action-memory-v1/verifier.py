"""Deterministic verifier for action-memory-v1."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def verify(
    task_dir: Path,
    evidence_dir: Path,
    reward_dir: Path | None = None,
) -> dict[str, Any]:
    if reward_dir is None:
        reward_dir = Path("/logs/verifier")
    reward_dir.mkdir(parents=True, exist_ok=True)

    scenario_file = task_dir / "scenario.json"
    if not scenario_file.exists():
        res = {"reward": 0.0, "reason": "missing_scenario_file"}
        _record(reward_dir, res)
        return res

    scenario = json.loads(scenario_file.read_text(encoding="utf-8"))
    expected_entity = scenario["target_entity"]
    expected_attr = scenario["target_attribute"]
    expected_val = scenario["latest_value"]

    final_state_file = evidence_dir / "final-state.json"
    if not final_state_file.exists():
        res = {"reward": 0.0, "reason": "missing_final_state_evidence"}
        _record(reward_dir, res)
        return res

    try:
        final_state = json.loads(final_state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        res = {"reward": 0.0, "reason": f"corrupt_final_state: {exc}"}
        _record(reward_dir, res)
        return res

    observed_entity = final_state.get("target_entity")
    observed_attr = final_state.get("target_attribute")
    observed_val = final_state.get("bound_value")

    if (
        observed_entity == expected_entity
        and observed_attr == expected_attr
        and observed_val == expected_val
    ):
        res = {
            "reward": 1.0,
            "reason": "exact_latest_value_bound",
            "target_entity": observed_entity,
            "bound_value": observed_val,
        }
    else:
        res = {
            "reward": 0.0,
            "reason": "value_mismatch_or_stale_binding",
            "expected": {"entity": expected_entity, "attr": expected_attr, "value": expected_val},
            "observed": {"entity": observed_entity, "attr": observed_attr, "value": observed_val},
        }

    _record(reward_dir, res)
    return res


def _record(reward_dir: Path, result: dict[str, Any]) -> None:
    reward_str = "1.0\n" if result["reward"] == 1.0 else "0.0\n"
    (reward_dir / "reward.txt").write_text(reward_str, encoding="utf-8")
    (reward_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, default=Path("/app/task_state"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("/app/evidence"))
    parser.add_argument("--reward-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()

    res = verify(args.task_dir, args.evidence_dir, args.reward_dir)
    sys.exit(0 if res["reward"] == 1.0 else 1)
