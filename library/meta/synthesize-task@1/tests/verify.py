"""Verifier entrypoint for synthesize-task meta-task."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Add tests/ directory to import completeness_checker
sys.path.insert(0, str(Path(__file__).parent))

try:
    from completeness_checker import check_task_completeness
except ImportError:
    from tests.completeness_checker import check_task_completeness  # type: ignore[no-redef]


def main() -> None:
    target_paths = [
        Path("/app/output/task"),
        Path("output/task"),
    ]
    target = None
    for tp in target_paths:
        if tp.exists():
            target = tp
            break

    if target is None:
        target = Path("/app/output/task")

    log_dir_paths = [
        Path("/logs/verifier"),
        Path("logs/verifier"),
    ]
    log_dir = None
    for lp in log_dir_paths:
        try:
            lp.mkdir(parents=True, exist_ok=True)
            log_dir = lp
            break
        except OSError:
            continue

    if log_dir is None:
        log_dir = Path("logs/verifier")
        log_dir.mkdir(parents=True, exist_ok=True)

    result = check_task_completeness(target)

    # Write CTRF, checks, and reward
    checks = result.get("checks", {})
    passed = bool(result.get("passed", False))
    reward = 1.0 if passed else 0.0

    ctrf_tests = []
    for check_name, check_data in checks.items():
        c_passed = bool(check_data.get("passed", False))
        ctrf_tests.append(
            {
                "name": check_name,
                "status": "passed" if c_passed else "failed",
                "duration": 0.0,
                "message": check_data.get("message", ""),
            }
        )

    ctrf = {
        "report": {
            "summary": {
                "tests": len(ctrf_tests),
                "passed": sum(1 for t in ctrf_tests if t["status"] == "passed"),
                "failed": sum(1 for t in ctrf_tests if t["status"] == "failed"),
            },
            "tests": ctrf_tests,
        }
    }

    (log_dir / "reward.json").write_text(
        json.dumps({"reward": reward}, indent=2) + "\n", encoding="utf-8"
    )
    (log_dir / "ctrf.json").write_text(json.dumps(ctrf, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"passed": passed, "checks": checks, "reward": reward}))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
