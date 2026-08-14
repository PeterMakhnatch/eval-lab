"""Local harbor-check demo: default rubric + shipped check-task verifier.

Does not invoke a billable evaluator. Uses the same load_rubric / assemble /
validate.py path that `harbor check` uses before it starts claude-code.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from harbor.analyze.checker import (
    CHECK_TASK_TEMPLATE_DIR,
    assemble_check_task,
)
from harbor.analyze.models import build_check_response_model, load_rubric
from harbor.cli.quality_checker.models import DEFAULT_RUBRIC_PATH


def main() -> int:
    task_dir = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path.cwd() / "check-demo-out"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"DEFAULT_RUBRIC_PATH={DEFAULT_RUBRIC_PATH}")
    print(f"CHECK_TASK_TEMPLATE_DIR={CHECK_TASK_TEMPLATE_DIR}")
    print(f"reviewed_task={task_dir}")

    rubric = load_rubric(None)
    names = [c.name for c in rubric.criteria]
    print(f"criteria_count={len(names)}")
    print("criteria=" + ",".join(names))

    schema = build_check_response_model(rubric).model_json_schema()
    required = schema.get("required") or list(schema.get("properties", {}))
    print(f"response_schema_required={required}")

    (out_dir / "criteria.json").write_text(json.dumps(names, indent=2) + "\n")
    (out_dir / "response-schema.json").write_text(json.dumps(schema, indent=2) + "\n")

    seeded = {
        name: {
            "outcome": "pass",
            "explanation": f"oracle-seeded local check for {name}",
        }
        for name in names
    }
    result_path = out_dir / "check-result.json"
    result_path.write_text(json.dumps(seeded, indent=2) + "\n")

    validate_py = CHECK_TASK_TEMPLATE_DIR / "tests" / "validate.py"
    print(f"validator={validate_py}")

    dest = Path(tempfile.mkdtemp(prefix="harbor-check-assemble-"))
    wrapper = assemble_check_task(
        task_dir=task_dir,
        rubric=rubric,
        template="unused-for-assembly-inspection",
        output_schema=schema,
        dest=dest,
    )
    wrapper_tests = wrapper / "tests"
    print(f"assembled_wrapper={wrapper}")
    print(f"assembled_tests={sorted(p.name for p in wrapper_tests.iterdir())}")

    # Run the shipped validator against the seeded result, using the
    # assembled criteria.json (same file the Docker verifier would see).
    import runpy

    assembled_validate = wrapper_tests / "validate.py"
    sys.argv = [str(assembled_validate), str(result_path)]
    # validate.py looks for criteria.json next to itself.
    # Point it at the assembled copy by running from that directory.
    old_cwd = Path.cwd()
    try:
        import os

        os.chdir(wrapper_tests)
        try:
            runpy.run_path(str(assembled_validate), run_name="__main__")
            rc = 0
        except SystemExit as exc:
            rc = int(exc.code or 0)
    finally:
        os.chdir(old_cwd)

    print(f"validator_exit={rc}")

    bad = dict(seeded)
    bad.pop(names[0])
    bad_path = out_dir / "check-result-bad.json"
    bad_path.write_text(json.dumps(bad, indent=2) + "\n")
    sys.argv = [str(assembled_validate), str(bad_path)]
    try:
        os.chdir(wrapper_tests)
        try:
            runpy.run_path(str(assembled_validate), run_name="__main__")
            bad_rc = 0
        except SystemExit as exc:
            bad_rc = int(exc.code or 0)
    finally:
        os.chdir(old_cwd)
    print(f"validator_rejects_incomplete_exit={bad_rc}")

    if rc != 0:
        print("FAIL: shipped validator rejected a well-formed seeded result")
        return 1
    if bad_rc == 0:
        print("FAIL: shipped validator accepted an incomplete result")
        return 1
    print("OK: default rubric loaded; assembled wrapper; validator accepted oracle-seeded result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
