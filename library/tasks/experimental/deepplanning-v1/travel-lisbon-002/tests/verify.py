#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("APP_ROOT", "/"))
ANSWER_PATH = ROOT / "app/answer.json"
LOG_DIR = ROOT / "logs/verifier"
GOLDEN_PATH = ROOT / "tests/golden.json"

def _record(passed: bool, reason: str, **extra: Any) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "reward.txt").write_text("1.0\n" if passed else "0.0\n", encoding="utf-8")
    payload = {"status": "passed" if passed else "failed", "reason": reason, "reward": 1.0 if passed else 0.0, **extra}
    (LOG_DIR / "result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if passed:
        print(f"[PASS] {reason}")
        return 0
    print(f"[FAIL] {reason}", file=sys.stderr)
    return 1

def verify(
    *,
    answer_path: Path = ANSWER_PATH,
    golden_path: Path = GOLDEN_PATH,
) -> int:
    if not answer_path.exists():
        return _record(False, f"answer file not found at {answer_path}")
    try:
        content = answer_path.read_text(encoding="utf-8").strip()
        if not content:
            return _record(False, "answer file is empty")
        answer = json.loads(content)
    except Exception as exc:
        return _record(False, f"failed to parse answer JSON: {exc}")
    if not isinstance(answer, dict):
        return _record(False, "answer is not a JSON object")

    golden_file = golden_path if golden_path.exists() else Path(__file__).parent / "golden.json"
    if not golden_file.exists():
        return _record(False, "golden specification missing from verifier context")
    golden = json.loads(golden_file.read_text(encoding="utf-8"))

    acquired = answer.get("acquired_sources", [])
    if not isinstance(acquired, list):
        return _record(False, "acquired_sources must be a list")
    missing = set(golden.get("required_sources", [])) - set(str(s) for s in acquired)
    if missing:
        return _record(False, f"missing required sources: {sorted(missing)}")

    status = answer.get("status")
    expected_status = golden.get("expected_status")
    if status != expected_status:
        return _record(False, f"status mismatch: expected '{expected_status}', got '{status}'")

    if expected_status == "infeasible":
        expected_reason = golden.get("expected_refusal_reason", "").strip()
        observed_reason = str(answer.get("refusal_reason", "")).strip()
        if observed_reason != expected_reason:
            return _record(False, f"refusal_reason mismatch: expected '{expected_reason}', got '{observed_reason}'")

    return _record(True, "DeepPlanning task verified successfully", status=status)

if __name__ == "__main__":
    sys.exit(verify())
