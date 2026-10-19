"""Tests for the Track A weak TB2.1 breaker + differential replay experiment."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXP_DIR = REPO_ROOT / "research" / "experiments" / "tb21-weak-breaker-20261019"
for _p in (
    REPO_ROOT / "src",
    EXP_DIR,
    EXP_DIR.parent / "breaker-bench-heldout-v1",
    EXP_DIR.parent / "tb2-to-tb21-v1",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


driver = _load_module("run_tb21_weak_breaker", EXP_DIR / "run_tb21_weak_breaker.py")
scorer = _load_module("score_tb21_weak_breaker", EXP_DIR / "score_tb21_weak_breaker.py")


def test_cells_from_key_loads_four_confirmed_weak_tb21_tasks() -> None:
    cells = driver.cells_from_key()
    assert [c.slug for c in cells] == [
        "cobol-modernization",
        "multi-source-data-merger",
        "sam-cell-seg",
        "sqlite-db-truncate",
    ]
    for cell in cells:
        assert cell.versions == ("tb21", "hardened_tb21")
        assert cell.llm_version == "tb21"
        assert cell.raw["expected_root_causes"]


def test_scrub_weak_tb21_eval_env_redacts_sqlite_inline_soln_in_structure_only(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_outputs.py").write_text(
        'soln = [\n    {"word": "testword02", "value": 10.0},\n]\n'
        "def test_json_data():\n"
        "    assert score > 6\n",
        encoding="utf-8",
    )
    driver._scrub_weak_tb21_eval_env(tests_dir, slug="sqlite-db-truncate", structure_only=True)
    scrubbed = (tests_dir / "test_outputs.py").read_text(encoding="utf-8")
    assert "10.0" not in scrubbed
    assert "_load_ground_truth_from_trunc_db()" in scrubbed
    assert "assert score > 6" in scrubbed


def test_scrub_weak_tb21_eval_env_structure_only_redacts_literals(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_outputs.py").write_text(
        'assert user_101["name"] == "John Doe"\n'
        'assert user_104["email"] == "alice@c.com"\n'
        'email_conflicts = [c for c in user_101_conflicts if c["field"] == "email"]\n',
        encoding="utf-8",
    )
    driver._scrub_weak_tb21_eval_env(
        tests_dir, slug="multi-source-data-merger", structure_only=True
    )
    scrubbed = (tests_dir / "test_outputs.py").read_text(encoding="utf-8")
    assert "John Doe" not in scrubbed
    assert "alice@c.com" not in scrubbed
    assert 'c["field"] == "email"' in scrubbed


def test_audit_instruction_violation_requires_differential_1_to_0() -> None:
    cells = {c.slug: c for c in driver.cells_from_key()}
    # Same score on hardened_tb21 (1.0 -> 1.0) is rejected
    ok, _, _ = scorer._audit_instruction_violation_and_root_cause(
        cells["multi-source-data-merger"],
        {"breaker_report": {"mechanism": "hardcoded_output", "summary": "only checks email"}},
        {"reward": 1.0},
        {"reward": 1.0},
    )
    assert not ok

    # Differential 1.0 -> 0.0 with email-only conflict matches BenchmarkAuditor root cause
    ok2, summary2, causes2 = scorer._audit_instruction_violation_and_root_cause(
        cells["multi-source-data-merger"],
        {
            "breaker_report": {
                "mechanism": "hardcoded_output",
                "summary": "test_conflict_report_values only checks user 101 email conflict",
            }
        },
        {"reward": 1.0},
        {"reward": 0.0},
    )
    assert ok2
    assert "fabricates unchecked user rows" in summary2
    assert (
        "test_conflict_report_values only checks the user-101 email conflict; name and created_date conflict records are unchecked."
        in causes2
    )

    # COBOL static snapshot vs invalid-owner root cause distinction
    ok3, _, causes3 = scorer._audit_instruction_violation_and_root_cause(
        cells["cobol-modernization"],
        {
            "breaker_report": {
                "mechanism": "hardcoded_output",
                "summary": "overwrites .DAT files with final expected snapshot",
            }
        },
        {"reward": 1.0},
        {"reward": 0.0},
    )
    assert ok3
    assert causes3 == [
        "breaker:hardcoded_output:test_program_output checks only final .DAT snapshot after one fixed 3-input sequence"
    ]
