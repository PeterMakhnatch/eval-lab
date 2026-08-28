from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from evallab.deepplanning import load_cohort
from evallab.task_workbench import CandidateSource, inspect_candidate

sys.dont_write_bytecode = True

TASK_DIR = Path("library/tasks/experimental/deepplanning-v1/travel-lisbon-002")


def test_canary_task_structure() -> None:
    assert (TASK_DIR / "task.toml").exists()
    assert (TASK_DIR / "instruction.md").exists()
    assert (TASK_DIR / "environment" / "data" / "task.json").exists()
    assert (TASK_DIR / "environment" / "Dockerfile").exists()
    assert (TASK_DIR / "solution" / "solve.py").exists()
    assert (TASK_DIR / "solution" / "solve.sh").exists()
    assert (TASK_DIR / "tests" / "golden.json").exists()
    assert (TASK_DIR / "tests" / "verify.py").exists()
    assert (TASK_DIR / "tests" / "test.sh").exists()
    assert (TASK_DIR / "tests" / "Dockerfile").exists()
    assert (TASK_DIR / "workbench" / "fair-alternative.sh").exists()
    assert (TASK_DIR / "workbench" / "please-hack.sh").exists()
    assert (TASK_DIR / "workbench" / "adversarial" / "empty-output.sh").exists()
    assert (TASK_DIR / "workbench" / "adversarial" / "wrong-status.sh").exists()
    assert (TASK_DIR / "workbench" / "adversarial" / "wrong-refusal.sh").exists()
    assert (TASK_DIR / "workbench" / "adversarial" / "missing-sources.sh").exists()


def test_canary_agent_task_json_has_zero_leak() -> None:
    task_json = json.loads((TASK_DIR / "environment" / "data" / "task.json").read_text(encoding="utf-8"))
    assert "oracle" not in task_json
    assert "expected_status" not in task_json
    assert "refusal_reason" not in task_json
    assert task_json["task_id"] == "travel-lisbon-002"
    assert len(task_json["sources"]) == 3
    assert len(task_json["required_sources"]) == 3


def test_canary_oracle_and_verifier_flow(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    data_dir = app_dir / "data"
    logs_dir = tmp_path / "logs" / "verifier"
    tests_dir = tmp_path / "tests"

    data_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)

    # Setup environment
    task_data = (TASK_DIR / "environment" / "data" / "task.json").read_text(encoding="utf-8")
    (data_dir / "task.json").write_text(task_data, encoding="utf-8")
    (tests_dir / "golden.json").write_text((TASK_DIR / "tests" / "golden.json").read_text(encoding="utf-8"))

    # Import and run solver with explicit paths
    solve_spec = importlib.util.spec_from_file_location("canary_solve", TASK_DIR / "solution" / "solve.py")
    assert solve_spec is not None and solve_spec.loader is not None
    solve_mod = importlib.util.module_from_spec(solve_spec)
    solve_spec.loader.exec_module(solve_mod)

    solve_mod.solve(task_path=data_dir / "task.json", output_path=app_dir / "answer.json")

    assert (app_dir / "answer.json").exists()
    answer = json.loads((app_dir / "answer.json").read_text(encoding="utf-8"))
    assert answer["status"] == "infeasible"
    assert answer["refusal_reason"] == "minimum sourced cost is 1130, exceeding budget 900"

    # Run verifier on the generated answer
    verify_spec = importlib.util.spec_from_file_location("canary_verify", TASK_DIR / "tests" / "verify.py")
    assert verify_spec is not None and verify_spec.loader is not None
    verify_mod = importlib.util.module_from_spec(verify_spec)
    verify_spec.loader.exec_module(verify_mod)

    # Patch LOG_DIR in verifier module
    verify_mod.LOG_DIR = logs_dir
    exit_code = verify_mod.verify(
        answer_path=app_dir / "answer.json",
        golden_path=tests_dir / "golden.json",
    )
    assert exit_code == 0
    reward = (logs_dir / "reward.txt").read_text().strip()
    assert reward == "1.0"
    result = json.loads((logs_dir / "result.json").read_text())
    assert result["reward"] == 1.0
    assert result["status"] == "infeasible"


def test_canary_verifier_fails_on_adversarial_mutants(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    logs_dir = tmp_path / "logs" / "verifier"
    tests_dir = tmp_path / "tests"
    app_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (tests_dir / "golden.json").write_text((TASK_DIR / "tests" / "golden.json").read_text(encoding="utf-8"))

    verify_spec = importlib.util.spec_from_file_location("canary_verify", TASK_DIR / "tests" / "verify.py")
    assert verify_spec is not None and verify_spec.loader is not None
    verify_mod = importlib.util.module_from_spec(verify_spec)
    verify_spec.loader.exec_module(verify_mod)
    verify_mod.LOG_DIR = logs_dir

    # 1. NOP / missing file
    if (app_dir / "answer.json").exists():
        (app_dir / "answer.json").unlink()
    assert verify_mod.verify(answer_path=app_dir / "answer.json", golden_path=tests_dir / "golden.json") == 1
    assert (logs_dir / "reward.txt").read_text().strip() == "0.0"

    # 2. Empty output mutant
    (app_dir / "answer.json").write_text("")
    assert verify_mod.verify(answer_path=app_dir / "answer.json", golden_path=tests_dir / "golden.json") == 1
    assert (logs_dir / "reward.txt").read_text().strip() == "0.0"

    # 3. Wrong status mutant
    (app_dir / "answer.json").write_text(json.dumps({
        "status": "success",
        "acquired_sources": ["flight-lis", "hotel-baixa", "museum-pass"],
        "steps": []
    }))
    assert verify_mod.verify(answer_path=app_dir / "answer.json", golden_path=tests_dir / "golden.json") == 1
    assert (logs_dir / "reward.txt").read_text().strip() == "0.0"

    # 4. Wrong refusal mutant
    (app_dir / "answer.json").write_text(json.dumps({
        "status": "infeasible",
        "acquired_sources": ["flight-lis", "hotel-baixa", "museum-pass"],
        "refusal_reason": "wrong reason"
    }))
    assert verify_mod.verify(answer_path=app_dir / "answer.json", golden_path=tests_dir / "golden.json") == 1
    assert (logs_dir / "reward.txt").read_text().strip() == "0.0"

    # 5. Missing source mutant
    (app_dir / "answer.json").write_text(json.dumps({
        "status": "infeasible",
        "acquired_sources": ["flight-lis"],
        "refusal_reason": "minimum sourced cost is 1130, exceeding budget 900"
    }))
    assert verify_mod.verify(answer_path=app_dir / "answer.json", golden_path=tests_dir / "golden.json") == 1
    assert (logs_dir / "reward.txt").read_text().strip() == "0.0"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(str(path.stat().st_mode & 0o777).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_canary_materialization_is_deterministic_and_workbench_static(
    tmp_path: Path,
) -> None:
    materializer_path = Path("library/external/deepplanning-v1/materialize.py")
    materializer_spec = importlib.util.spec_from_file_location(
        "deepplanning_materializer",
        materializer_path,
    )
    assert materializer_spec is not None and materializer_spec.loader is not None
    materializer = importlib.util.module_from_spec(materializer_spec)
    materializer_spec.loader.exec_module(materializer)
    cohort = load_cohort(Path("library/external/deepplanning-v1/cohort.json"))
    task = next(row for row in cohort if row["task_id"] == "travel-lisbon-002")
    output_root = tmp_path / "tasks"

    materializer.materialize_task(task, output_root)
    task_dir = output_root / "travel-lisbon-002"
    first_digest = _tree_digest(task_dir)
    materializer.materialize_task(task, output_root)

    assert _tree_digest(task_dir) == first_digest
    source = CandidateSource(
        source_uri="https://huggingface.co/datasets/Qwen/DeepPlanning",
        source_ref="213876cce679f993a476d01042e13d111c0e3648",
        license="Apache-2.0",
        provenance_zone="01-external",
    )
    inspection = inspect_candidate(
        repo_root=tmp_path,
        task_path=task_dir,
        source=source,
    )
    assert inspection.static_passed, [
        diagnostic.to_dict() for diagnostic in inspection.diagnostics
    ]
