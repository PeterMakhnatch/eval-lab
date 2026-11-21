from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from evallab import probe03

SCHEMA = "evallab.reward_report/v1"


def ctrf_counts(trial_dir: Path) -> dict[str, int] | None:
    counts = probe03.ctrf_counts(trial_dir)
    if counts is None:
        return None
    tests, passed, failed, skipped = counts
    return {"tests": tests, "passed": passed, "failed": failed, "skipped": skipped}


def reward_file(trial_dir: Path) -> float | None:
    try:
        text = (trial_dir / "verifier" / "reward.txt").read_text(encoding="utf-8").strip()
        reward = float(text)
    except (OSError, ValueError, OverflowError):
        return None
    return reward if math.isfinite(reward) else None


def contradiction_reason(reward: float, counts: Mapping[str, int]) -> str:
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
    if not contradiction_reason(reward, counts):
        return ""
    passed, failed = counts["passed"], counts["failed"]
    if passed == 0 and failed > 0:
        return "forged"
    if passed > 0 and failed > 0:
        return "partial-credit"
    return "unknown"


def assess(trial_dir: Path) -> dict[str, Any] | None:
    try:
        reward, scored, source = probe03.read_reward(trial_dir)
    except (OSError, ValueError, RecursionError):
        return None
    if not scored or reward is None:
        if source != "exception_no_captured_reward":
            return None
        reward_txt = reward_file(trial_dir)
        if reward_txt is None:
            return None
        return {
            "schema": SCHEMA,
            "classification": None,
            "reason": "",
            "mismatch": False,
            "reward": None,
            "reward_source": source,
            "reward_txt": reward_txt,
            "counts": ctrf_counts(trial_dir),
            "orphan": True,
        }
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
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def flags(record: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    if record.get("orphan"):
        result.append(f"reward_report:orphan:{_number_text(record['reward_txt'])}")
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


def kind_counts(flag_strings: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for flag in flag_strings:
        parts = flag.split(":", 2)
        if len(parts) == 3 and parts[0] == "reward_report" and parts[1]:
            kind = parts[1]
            counts[kind] = counts.get(kind, 0) + 1
    return {kind: counts[kind] for kind in sorted(counts)}
