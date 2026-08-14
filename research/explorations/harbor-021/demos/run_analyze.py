"""Local harbor-analyze demo: default rubric + shipped analyze-task verifier.

Does not invoke a billable evaluator. Uses the same load_rubric / assemble /
validate.py path that `harbor analyze` uses before it starts claude-code.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import tempfile
from pathlib import Path

from harbor.analyze.analyzer import (
    ANALYZE_TASK_TEMPLATE_DIR,
    DEFAULT_RUBRIC_PATH,
    assemble_analyze_task,
)
from harbor.analyze.models import build_analyze_response_model, load_rubric


def main() -> int:
    trial_dir = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path.cwd() / "analyze-demo-out"
    task_dir = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"DEFAULT_ANALYZE_RUBRIC_PATH={DEFAULT_RUBRIC_PATH}")
    print(f"ANALYZE_TASK_TEMPLATE_DIR={ANALYZE_TASK_TEMPLATE_DIR}")
    print(f"trial_dir={trial_dir}")
    print(f"task_dir={task_dir}")

    rubric = load_rubric(DEFAULT_RUBRIC_PATH)
    names = [c.name for c in rubric.criteria]
    print(f"criteria_count={len(names)}")
    print("criteria=" + ",".join(names))

    schema = build_analyze_response_model(rubric).model_json_schema()
    print(f"response_schema_keys={list(schema.get('properties', {}))}")

    seeded = {
        "summary": (
            "oracle-seeded local analysis: trajectory shows no reward-hacking; "
            "instruction matches tests."
        ),
        "checks": {
            name: {
                "outcome": "pass",
                "explanation": f"oracle-seeded local analysis for {name}",
            }
            for name in names
        },
    }
    result_path = out_dir / "analysis.json"
    result_path.write_text(json.dumps(seeded, indent=2) + "\n")
    (out_dir / "criteria.json").write_text(json.dumps(names, indent=2) + "\n")

    dest = Path(tempfile.mkdtemp(prefix="harbor-analyze-assemble-"))
    wrapper = assemble_analyze_task(
        trial_dir=trial_dir,
        task_dir=task_dir if task_dir and task_dir.exists() else None,
        rubric=rubric,
        template="Trial at {trial_path}. {task_section}\n{criteria_guidance}",
        output_schema=schema,
        dest=dest,
    )
    wrapper_tests = wrapper / "tests"
    print(f"assembled_wrapper={wrapper}")
    print(f"assembled_tests={sorted(p.name for p in wrapper_tests.iterdir())}")
    print(f"uploaded_trial={(wrapper / 'environment' / 'trial').is_dir()}")

    validate_py = wrapper_tests / "validate.py"
    old_cwd = Path.cwd()
    sys.argv = [str(validate_py), str(result_path)]
    try:
        os.chdir(wrapper_tests)
        try:
            runpy.run_path(str(validate_py), run_name="__main__")
            rc = 0
        except SystemExit as exc:
            rc = int(exc.code or 0)
    finally:
        os.chdir(old_cwd)
    print(f"validator_exit={rc}")

    bad = {"summary": "missing checks"}
    bad_path = out_dir / "analysis-bad.json"
    bad_path.write_text(json.dumps(bad) + "\n")
    sys.argv = [str(validate_py), str(bad_path)]
    try:
        os.chdir(wrapper_tests)
        try:
            runpy.run_path(str(validate_py), run_name="__main__")
            bad_rc = 0
        except SystemExit as exc:
            bad_rc = int(exc.code or 0)
    finally:
        os.chdir(old_cwd)
    print(f"validator_rejects_incomplete_exit={bad_rc}")

    if rc != 0:
        print("FAIL: shipped analyzer validator rejected a well-formed seeded result")
        return 1
    if bad_rc == 0:
        print("FAIL: shipped analyzer validator accepted an incomplete result")
        return 1
    print(
        "OK: default analyze rubric loaded; assembled wrapper; "
        "validator accepted oracle-seeded result"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
