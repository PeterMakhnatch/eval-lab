"""Deterministic reference generator for synthesize-task meta-task."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from pathlib import Path


def find_input_dir(name: str) -> Path:
    candidates = [
        Path(f"/app/{name}"),
        Path(f"environment/{name}"),
        Path(name),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(f"/app/{name}")


def load_spec() -> dict[str, object]:
    spec_paths = [
        Path("/app/spec.json"),
        Path("environment/spec.json"),
        Path("spec.json"),
    ]
    for p in spec_paths:
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {
        "name": "log-aggregation-summary",
        "category": "data-processing",
        "scenario": "structured-pipeline",
        "difficulty": "medium",
        "summary": "Process structured input logs and generate aggregated summary reports",
        "seed_class": "craft-gap",
    }


def main() -> int:
    spec = load_spec()
    skeleton_dir = find_input_dir("skeleton")
    exemplar_dir = find_input_dir("exemplar")
    _ = exemplar_dir

    out_dirs = [
        Path("/app/output/task"),
        Path("output/task"),
    ]
    out_dir = None
    for d in out_dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            out_dir = d
            break
        except OSError:
            continue

    if out_dir is None:
        out_dir = Path("output/task")
        out_dir.mkdir(parents=True, exist_ok=True)

    task_name = str(spec.get("name", "synthesized-task")).lower().replace(" ", "-")
    category = str(spec.get("category", "data-processing"))
    difficulty = str(spec.get("difficulty", "medium"))
    summary = str(spec.get("summary", "Process input data and generate summary report"))

    # Copy skeleton as base
    if skeleton_dir.is_dir():
        for item in skeleton_dir.iterdir():
            dest = out_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    # Customize task.toml
    task_toml_text = f"""schema_version = "1.4"
artifacts = [
    "/app/output/summary.json",
]

[task]
name = "local-lab/{task_name}"
version = "0.1.0"
description = "{summary}"
keywords = ["python", "{category}", "separate-verifier"]

[[task.authors]]
name = "Eval Lab Synthesizer"
email = "p.makhnatch@gmail.com"

[metadata]
difficulty = "{difficulty}"
category = "{category}"
tags = ["synthetic", "authoring"]

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
collect = []

[agent]
timeout_sec = 120.0

[environment]
network_mode = "public"
build_timeout_sec = 300.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 2048
mcp_servers = []
"""
    (out_dir / "task.toml").write_text(task_toml_text, encoding="utf-8")

    # Customize instruction.md
    instruction_text = f"""# {task_name.replace('-', ' ').title()}

Read the records in `/app/input/data.json` and create `/app/output/summary.json`.

The output must be a valid JSON object with the following fields:
- `schema_version`: integer `1`
- `total_records`: total count of records processed
- `status`: string `"ok"`

Write valid UTF-8 JSON with a trailing newline. Do not modify or delete the input file.
"""
    (out_dir / "instruction.md").write_text(instruction_text, encoding="utf-8")

    # Ensure environment/Dockerfile and data exist
    env_dir = out_dir / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_dockerfile = """FROM python:3.13-slim-bookworm

WORKDIR /app

COPY data.json /app/input/data.json

RUN mkdir -p /app/output
"""
    (env_dir / "Dockerfile").write_text(env_dockerfile, encoding="utf-8")
    sample_data = [
        {"id": 1, "type": "event_a", "val": 10},
        {"id": 2, "type": "event_b", "val": 20},
        {"id": 3, "type": "event_a", "val": 30},
    ]
    (env_dir / "data.json").write_text(json.dumps(sample_data, indent=2) + "\n", encoding="utf-8")

    # Ensure solution exists and is executable
    sol_dir = out_dir / "solution"
    sol_dir.mkdir(parents=True, exist_ok=True)
    sol_sh = """#!/bin/sh
set -eu

if [ -f /solution/solve.py ]; then
    exec python /solution/solve.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/solve.py"
fi
"""
    (sol_dir / "solve.sh").write_text(sol_sh, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(sol_dir / "solve.sh", 0o755)

    sol_py = """import json
from pathlib import Path

input_file = Path("/app/input/data.json")
if not input_file.is_file():
    input_file = Path("environment/data.json")
if not input_file.is_file():
    input_file = Path("input/data.json")

data = json.loads(input_file.read_text(encoding="utf-8"))

summary = {
    "schema_version": 1,
    "total_records": len(data),
    "status": "ok",
}

output_dir = Path("/app/output")
if not output_dir.exists():
    output_dir = Path("output")
output_dir.mkdir(parents=True, exist_ok=True)

output_file = output_dir / "summary.json"
output_file.write_text(json.dumps(summary, indent=2) + "\\n", encoding="utf-8")
"""
    (sol_dir / "solve.py").write_text(sol_py, encoding="utf-8")

    # Ensure tests exist and are executable
    tests_dir = out_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    tests_dockerfile = """FROM python:3.13-slim-bookworm

WORKDIR /app

COPY test.sh /tests/test.sh
COPY verify.py /tests/verify.py

RUN chmod +x /tests/test.sh
"""
    (tests_dir / "Dockerfile").write_text(tests_dockerfile, encoding="utf-8")

    test_sh = """#!/bin/sh
set -eu

if [ -f /tests/verify.py ]; then
    exec python /tests/verify.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/verify.py"
fi
"""
    (tests_dir / "test.sh").write_text(test_sh, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tests_dir / "test.sh", 0o755)

    verify_py = """import json
from pathlib import Path

AGENT_OUTPUT = Path("/app/output/summary.json")
if not AGENT_OUTPUT.is_file():
    AGENT_OUTPUT = Path("output/summary.json")

LOG_DIR = Path("/logs/verifier")
if not LOG_DIR.exists():
    LOG_DIR = Path("logs/verifier")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not AGENT_OUTPUT.is_file():
        passed = False
        message = "summary.json is missing"
    else:
        try:
            data = json.loads(AGENT_OUTPUT.read_text(encoding="utf-8"))
            passed = (
                isinstance(data, dict)
                and data.get("schema_version") == 1
                and data.get("total_records") == 3
                and data.get("status") == "ok"
            )
            message = "summary.json is valid" if passed else "summary.json content mismatch"
        except Exception as exc:
            passed = False
            message = f"error parsing json: {exc}"

    checks = {"correctness": {"passed": passed, "message": message}}
    rewards = {"reward": 1.0 if passed else 0.0}
    ctrf = {
        "report": {
            "summary": {
                "tests": 1,
                "passed": 1 if passed else 0,
                "failed": 0 if passed else 1,
            }
        }
    }

    (LOG_DIR / "checks.json").write_text(json.dumps(checks, indent=2) + "\\n", encoding="utf-8")
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards, indent=2) + "\\n", encoding="utf-8")
    (LOG_DIR / "ctrf.json").write_text(json.dumps(ctrf, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "checks": checks}))


if __name__ == "__main__":
    main()
"""
    (tests_dir / "verify.py").write_text(verify_py, encoding="utf-8")

    print(f"Synthesized task generated at {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
