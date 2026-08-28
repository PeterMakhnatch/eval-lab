#!/usr/bin/env python3
"""Exact dependency-trace verifier for Function-DAG task."""
import json
import sys
from pathlib import Path
from typing import Any

GOLDEN = {"dependency_trace": [{"inputs": [{"id": "in_1", "value": 3}], "node": "n_2_0", "value": 3}], "target": "n_2_0", "value": 3}
RESULT_PATH = Path("/app/output/result.json")
LOG_DIR = Path("/logs/verifier")


def _record(passed: bool, reason: str, **extra: Any) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "reward.txt").write_text("1\n" if passed else "0\n", encoding="utf-8")
    payload: dict[str, Any] = {
        "passed": passed,
        "reason": reason,
        **extra,
    }
    (LOG_DIR / "result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if passed:
        print(f"[PASS] {reason}")
        return 0
    print(f"[FAIL] {reason}", file=sys.stderr)
    return 1


def _exact_json_equal(observed: Any, expected: Any) -> bool:
    if type(observed) is not type(expected):
        return False
    if isinstance(expected, dict):
        return observed.keys() == expected.keys() and all(
            _exact_json_equal(observed[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(observed) == len(expected) and all(
            _exact_json_equal(observed_item, expected_item)
            for observed_item, expected_item in zip(observed, expected, strict=True)
        )
    return observed == expected


def verify() -> int:
    if not RESULT_PATH.exists():
        return _record(
            False,
            f"Result file does not exist: {RESULT_PATH}",
            expected_contract="dependency-trace/v1",
        )

    try:
        content = RESULT_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return _record(
                False,
                "Result file is empty",
                expected_contract="dependency-trace/v1",
            )
        data = json.loads(content)
    except Exception as exc:
        return _record(
            False,
            f"Invalid JSON format: {exc}",
            expected_contract="dependency-trace/v1",
        )

    if not isinstance(data, dict):
        return _record(
            False,
            f"Expected JSON object, got {type(data).__name__}",
            expected_contract="dependency-trace/v1",
        )

    if data.keys() != GOLDEN.keys():
        return _record(
            False,
            "Result must contain exactly target, value, and dependency_trace",
            expected_keys=list(GOLDEN),
            observed_keys=list(data),
        )

    if not _exact_json_equal(data, GOLDEN):
        return _record(
            False,
            "Result does not exactly match the dependency-trace contract",
            expected=GOLDEN,
            observed=data,
        )

    return _record(
        True,
        f"Verified exact dependency trace for {GOLDEN['target']} = {GOLDEN['value']}",
        observed_target=data["target"],
        observed_value=data["value"],
        trace_length=len(data["dependency_trace"]),
    )


if __name__ == "__main__":
    sys.exit(verify())
