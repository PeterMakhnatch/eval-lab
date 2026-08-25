#!/usr/bin/env python3
"""Fail closed when a Harbor control job's reward differs from its contract."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _job_reward(job: Path) -> float:
    result = job / "result.json"
    if not result.is_file():
        raise RuntimeError(f"missing Harbor result: {result}")
    payload = json.loads(result.read_text(encoding="utf-8"))
    stats = payload.get("stats") or {}
    for report in (stats.get("evals") or {}).values():
        for metric in report.get("metrics") or []:
            if isinstance(metric, dict) and (mean := _number(metric.get("mean"))) is not None:
                return mean
        reward_stats = report.get("reward_stats") or {}
        if isinstance(reward_stats, dict):
            for key in ("mean", "reward"):
                if (mean := _number(reward_stats.get(key))) is not None:
                    return mean
    for trial in sorted(path for path in job.iterdir() if path.is_dir()):
        trial_result = trial / "result.json"
        if not trial_result.is_file():
            continue
        trial_payload = json.loads(trial_result.read_text(encoding="utf-8"))
        rewards = (trial_payload.get("verifier_result") or {}).get("rewards") or {}
        if (reward := _number(rewards.get("reward"))) is not None:
            return reward
    raise RuntimeError(f"no numeric reward in {result}")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: assert_reward.py JOB_DIR EXPECTED")
    job = Path(sys.argv[1])
    expected = float(sys.argv[2])
    observed = _job_reward(job)
    if abs(observed - expected) > 1e-9:
        raise SystemExit(f"{job}: reward {observed} != expected {expected}")
    print(f"{job}: reward={observed}")


if __name__ == "__main__":
    main()
