"""HAR-440 reward/report contradiction evidence, mirroring HAR-437 rules.

Use retained CTRF test statuses and reward.txt to flag contradictory rewards.
This is evidence only: reward, scored, counts, and decision are never rewritten.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evallab import probe03

SCHEMA = "evallab.reward_report/v1"


def ctrf_counts(trial_dir: Path) -> dict[str, int] | None:
    """Count test objects in a non-empty retained CTRF test list."""
    try:
        tests = probe03._ctrf_tests(trial_dir)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(tests, list) or not tests:
        return None
    counts = {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}
    for test in tests:
        if not isinstance(test, dict):
            continue
        counts["tests"] += 1
        status = test.get("status")
        if status == "passed":
            counts["passed"] += 1
        elif status in ("skipped", "pending"):
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
    return counts


def reward_file(trial_dir: Path) -> float | None:
    """Read finite numeric text from verifier/reward.txt, or return None."""
    try:
        text = (trial_dir / "verifier" / "reward.txt").read_text(encoding="utf-8").strip()
        reward = float(text)
    except (OSError, ValueError, OverflowError):
        return None
    return reward if math.isfinite(reward) else None


def contradiction_reason(reward: float, counts: Mapping[str, int]) -> str:
    """Return the HAR-437 contradiction reason, or an empty string."""
    if counts["tests"] <= 0:
        return ""
    passed, failed, skipped = counts["passed"], counts["failed"], counts["skipped"]
    if reward >= 1 and failed > 0:
        noun = "test" if failed == 1 else "tests"
        return f"full reward with {failed} failed {noun}"
    if reward > 0 and passed == 0:
        return f"positive reward with no passed test ({failed} failed, {skipped} skipped)"
    return ""


def classify(reward: float, counts: Mapping[str, int]) -> str:
    """Classify a contradiction, or return an empty string when none exists."""
    if not contradiction_reason(reward, counts):
        return ""
    passed, failed = counts["passed"], counts["failed"]
    if passed == 0 and failed > 0:
        return "forged"
    if passed > 0 and failed > 0:
        return "partial-credit"
    return "unknown"


def assess(trial_dir: Path) -> dict[str, Any] | None:
    """Return evidence only when a report contradiction or reward mismatch fires."""
    try:
        reward, _, source = probe03.read_reward(trial_dir)
    except (OSError, ValueError, RecursionError):
        return None
    if reward is None:
        return None
    counts = ctrf_counts(trial_dir)
    reward_txt = reward_file(trial_dir)
    reason = contradiction_reason(reward, counts) if counts is not None else ""
    mismatch = reward_txt is not None and abs(reward_txt - reward) > 1e-9
    if not reason and not mismatch:
        return None
    return {
        "schema": SCHEMA,
        "classification": classify(reward, counts) if reason and counts is not None else None,
        "reason": reason,
        "mismatch": mismatch,
        "reward": reward,
        "reward_source": source,
        "reward_txt": reward_txt,
        "counts": counts,
    }


def _number_text(value: float) -> str:
    """Format up to 6 decimal places without trailing zeros or negative zero."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def flags(record: Mapping[str, Any]) -> list[str]:
    """Return the contradiction flag before the optional reward mismatch flag."""
    result: list[str] = []
    classification = record.get("classification")
    counts = record.get("counts")
    if classification is not None and counts is not None:
        result.append(
            f"reward_report:{classification}:{counts['passed']}/{counts['tests']}-passed"
        )
    if record.get("mismatch"):
        result.append(
            f"reward_report:mismatch:{_number_text(record['reward'])}"
            f"-vs-{_number_text(record['reward_txt'])}"
        )
    return result
