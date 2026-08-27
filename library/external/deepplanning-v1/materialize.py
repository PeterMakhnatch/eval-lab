#!/usr/bin/env python3
"""Materialize DeepPlanning cohort tasks into Harbor-compatible task packages."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from evallab.deepplanning import derive_solution, load_cohort, sanitize_agent_task

PYTHON_BASE_IMAGE = (
    "python:3.13-slim@sha256:"
    "bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)


def materialize_task(task: dict[str, Any], output_dir: Path) -> None:
    """Materialize one DeepPlanning task into output_dir with strict oracle isolation."""
    task_id = str(task["task_id"])
    task_dir = output_dir / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    env_dir = task_dir / "environment"
    env_data_dir = env_dir / "data"
    solution_dir = task_dir / "solution"
    tests_dir = task_dir / "tests"
    workbench_dir = task_dir / "workbench"
    adversarial_dir = workbench_dir / "adversarial"

    for d in (env_data_dir, solution_dir, tests_dir, adversarial_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. task.toml
    task_toml = f"""schema_version = "1.4"
artifacts = ["/app/answer.json"]

[task]
name = "deepplanning-v1/{task_id}"
version = "1.0.0"
description = "Offline deterministic DeepPlanning case {task_id}"
keywords = ["deepplanning", "{task.get('domain', 'planning')}", "constraints", "planning", "offline"]

[[task.authors]]
name = "Qwen Team"
email = "unknown"

[metadata]
author_name = "Qwen Team"
category = "Planning"
difficulty = "hard"
subcategory = "DeepPlanning"
tags = ["deepplanning", "{task.get('domain', 'planning')}", "constraints", "offline"]
license = "Apache-2.0"
source_url = "https://github.com/QwenLM/Qwen-Agent"
source_ref = "31a4d36d123688581a9e9744427272b33ce940e0"

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
collect = []

[verifier.environment]
network_mode = "no-network"

[agent]
timeout_sec = 120.0

[environment]
network_mode = "public"
build_timeout_sec = 120.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 1024
mcp_servers = []
"""
    (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")

    # 2. instruction.md
    instruction_md = f"""# DeepPlanning Task: {task_id}

{task.get('prompt', '')}

Inspect the task specifications and available sources in `/app/data/task.json`.
Submit your solution to `/app/answer.json`.
"""
    (task_dir / "instruction.md").write_text(instruction_md, encoding="utf-8")

    # 3. environment/data/task.json (SANITIZED - NO ORACLE / NO REFUSAL REASON)
    sanitized = sanitize_agent_task(task)
    (env_data_dir / "task.json").write_text(json.dumps(sanitized, indent=2), encoding="utf-8")

    # 4. environment/Dockerfile
    env_dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

WORKDIR /app
RUN mkdir -p /app/data
COPY data/task.json /app/data/task.json
"""
    (env_dir / "Dockerfile").write_text(env_dockerfile, encoding="utf-8")

    # 5. tests/ (Separate Verifier context)
    derived = derive_solution(task)
    golden = {
        "task_id": task_id,
        "expected_status": derived.get("status"),
        "expected_refusal_reason": derived.get("refusal_reason"),
        "required_sources": task.get("required_sources", []),
    }
    (tests_dir / "golden.json").write_text(json.dumps(golden, indent=2), encoding="utf-8")

    tests_dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

WORKDIR /app
RUN mkdir -p /logs/verifier /tests
COPY golden.json /tests/golden.json
COPY verify.py /tests/verify.py
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh /tests/verify.py
"""
    (tests_dir / "Dockerfile").write_text(tests_dockerfile, encoding="utf-8")

    verify_py = """#!/usr/bin/env python3
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
    (LOG_DIR / "reward.txt").write_text("1.0\\n" if passed else "0.0\\n", encoding="utf-8")
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
"""
    (tests_dir / "verify.py").write_text(verify_py, encoding="utf-8")
    (tests_dir / "verify.py").chmod(0o755)

    test_sh = """#!/bin/bash
set -euo pipefail
python3 /tests/verify.py 2>/dev/null || python3 "$(dirname "$0")/verify.py"
"""
    (tests_dir / "test.sh").write_text(test_sh, encoding="utf-8")
    (tests_dir / "test.sh").chmod(0o755)

    # 6. solution/ (Executable Oracle)
    solve_py = f"""#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT", "/"))
TASK_PATH = ROOT / "app/data/task.json"
OUTPUT_PATH = ROOT / "app/answer.json"

def solve(*, task_path: Path = TASK_PATH, output_path: Path = OUTPUT_PATH) -> None:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    sources = {{s["source_id"]: s for s in task.get("sources", [])}}
    required = list(task.get("required_sources", []))

    total_cost = 0.0
    for sid in required:
        content = str(sources[sid]["content"])
        prices = [float(v) for v in re.findall(r"(?:price|subtract)\\s+(\\d+(?:\\.\\d+)?)", content, re.IGNORECASE)]
        if "subtract" in content.lower():
            total_cost -= sum(prices)
        else:
            total_cost += sum(prices)

    budget_limit = None
    for c in task.get("constraints", []):
        if c.get("type") == "budget_lte":
            budget_limit = float(c["value"])
            break

    if budget_limit is not None and total_cost > budget_limit:
        int_cost = int(total_cost) if total_cost.is_integer() else total_cost
        int_budget = int(budget_limit) if budget_limit.is_integer() else budget_limit
        answer = {{
            "status": "infeasible",
            "refusal_reason": f"minimum sourced cost is {{int_cost}}, exceeding budget {{int_budget}}",
            "acquired_sources": required,
        }}
    else:
        answer = {json.dumps(derived, indent=8)}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(answer, indent=2), encoding="utf-8")

if __name__ == "__main__":
    solve()
"""
    (solution_dir / "solve.py").write_text(solve_py, encoding="utf-8")
    (solution_dir / "solve.py").chmod(0o755)

    solve_sh = """#!/bin/bash
set -euo pipefail
python3 /solution/solve.py 2>/dev/null || python3 "$(dirname "$0")/solve.py"
"""
    (solution_dir / "solve.sh").write_text(solve_sh, encoding="utf-8")
    (solution_dir / "solve.sh").chmod(0o755)

    # 7. workbench/
    fair_alternative = """#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
import os
import re
from pathlib import Path

root = Path(os.environ.get("APP_ROOT", "/"))
task = json.loads((root / "app/data/task.json").read_text(encoding="utf-8"))
sources = {row["source_id"]: row for row in task["sources"]}
required = list(task["required_sources"])
total = 0.0
for source_id in required:
    content = str(sources[source_id]["content"])
    total += sum(
        float(value)
        for value in re.findall(r"price\\s+(\\d+(?:\\.\\d+)?)", content, re.IGNORECASE)
    )
budget = next(
    float(row["value"])
    for row in task["constraints"]
    if row.get("type") == "budget_lte"
)
if total > budget:
    total_value = int(total) if total.is_integer() else total
    budget_value = int(budget) if budget.is_integer() else budget
    answer = {
        "status": "infeasible",
        "refusal_reason": (
            f"minimum sourced cost is {total_value}, exceeding budget {budget_value}"
        ),
        "acquired_sources": required,
    }
else:
    answer = {"status": "success", "acquired_sources": required, "steps": []}
output = root / "app/answer.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(answer, indent=2), encoding="utf-8")
PY
"""
    fair_path = workbench_dir / "fair-alternative.sh"
    fair_path.write_text(fair_alternative, encoding="utf-8")
    fair_path.chmod(0o755)

    please_hack_path = workbench_dir / "please-hack.sh"
    please_hack_path.write_text("#!/bin/bash\nset -euo pipefail\nexit 0\n", encoding="utf-8")
    please_hack_path.chmod(0o755)

    mutants = {
        "empty-output.sh": """#!/bin/bash
set -euo pipefail
target="${APP_ROOT:-}/app/answer.json"
mkdir -p "$(dirname "$target")"
: > "$target"
""",
        "wrong-status.sh": """#!/bin/bash
set -euo pipefail
target="${APP_ROOT:-}/app/answer.json"
mkdir -p "$(dirname "$target")"
printf '%s\\n' '{"status":"success","acquired_sources":[],"steps":[]}' > "$target"
""",
        "wrong-refusal.sh": """#!/bin/bash
set -euo pipefail
target="${APP_ROOT:-}/app/answer.json"
mkdir -p "$(dirname "$target")"
printf '%s\\n' '{"status":"infeasible","refusal_reason":"budget exceeded","acquired_sources":["flight-lis","hotel-baixa","museum-pass"]}' > "$target"
""",
        "missing-sources.sh": """#!/bin/bash
set -euo pipefail
target="${APP_ROOT:-}/app/answer.json"
mkdir -p "$(dirname "$target")"
printf '%s\\n' '{"status":"infeasible","refusal_reason":"minimum sourced cost is 1130, exceeding budget 900","acquired_sources":[]}' > "$target"
""",
    }
    for name, content in mutants.items():
        path = adversarial_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize the DeepPlanning canary")
    parser.add_argument(
        "--cohort",
        type=Path,
        default=Path("library/external/deepplanning-v1/cohort.json"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("library/external/deepplanning-v1/tasks"),
    )
    parser.add_argument("--task-id", default="travel-lisbon-002")
    args = parser.parse_args()

    tasks = {str(task["task_id"]): task for task in load_cohort(args.cohort)}
    if args.task_id not in tasks:
        raise ValueError(f"DeepPlanning cohort has no task {args.task_id!r}")
    materialize_task(tasks[args.task_id], args.out)
    print(f"Materialized task {args.task_id} to {args.out / args.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
