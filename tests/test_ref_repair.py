"""Ref-subset repair: shortcut intake, x3 acceptance, fake-runtime orchestration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.ref_repair import (
    RefRepairCheck,
    RefRepairPlan,
    classify_ref_validation,
    execute_ref_repair,
    load_ref_report,
    load_shortcuts,
    plan_ref_repair,
)
from evallab.verifier_repair import QuotaStatus


def _report(**overrides):
    report = {
        "task_id": "mimo-v2.6-rl/format-code-task-000001",
        "verdict": "fail",
        "package": "/nonexistent",
        "blocks": [
            {"id": "pkg/mod.py#0", "path": "pkg/mod.py", "kind": "code"},
            {"id": "pkg/mod.py#1", "path": "pkg/mod.py", "kind": "code"},
            {"id": "pkg/other.py#0", "path": "pkg/other.py", "kind": "code"},
        ],
        "kept": ["pkg/mod.py#0"],
        "grades": [
            {"label": "full", "kept": 3, "reward": 1.0},
            {"label": "nop", "kept": 0, "reward": 0.0},
            {"label": "drop-block:pkg/mod.py#1", "kept": 2, "reward": 1.0},
            {"label": "drop-file:pkg/other.py", "kept": 2, "reward": 1.0},
            {"label": "drop-block:pkg/other.py#0", "kept": 2, "reward": 0.0},
            {"label": "confirm", "kept": 1, "reward": 1.0},
        ],
    }
    report.update(overrides)
    return report


def test_load_shortcuts_collects_every_passing_proper_subset():
    shortcuts = load_shortcuts(_report())
    by_label = {s.label: s for s in shortcuts}
    assert set(by_label) == {
        "minimal",
        "drop-block:pkg/mod.py#1",
        "drop-file:pkg/other.py",
    }
    assert by_label["minimal"].keep == frozenset({"pkg/mod.py#0"})
    assert by_label["drop-block:pkg/mod.py#1"].keep == frozenset(
        {"pkg/mod.py#0", "pkg/other.py#0"}
    )
    # drop-file removes that file's only block: same set as drop-block#1's complement.
    assert by_label["drop-file:pkg/other.py"].keep == frozenset(
        {"pkg/mod.py#0", "pkg/mod.py#1"}
    )


def test_load_shortcuts_rejects_reports_without_passing_shortcuts():
    with pytest.raises(ValueError, match="no passing shortcut"):
        load_shortcuts(_report(kept=[], grades=[{"label": "full", "reward": 1.0}]))


def test_load_ref_report_rejects_non_fail_verdicts(tmp_path):
    (tmp_path / "report.json").write_text(json.dumps(_report(verdict="pass")))
    with pytest.raises(ValueError, match="not 'fail'"):
        load_ref_report(tmp_path)


def _checks(reference, nop, shortcuts):
    rows = [
        RefRepairCheck(name=f"reference-{i}", role="reference", status="scored", reward=r)
        for i, r in enumerate(reference)
    ]
    rows += [
        RefRepairCheck(name=f"nop-{i}", role="nop", status="scored", reward=r)
        for i, r in enumerate(nop)
    ]
    rows += [
        RefRepairCheck(name=name, role="shortcut", status="scored", reward=r)
        for name, r in shortcuts
    ]
    return rows


def test_classify_accepts_only_x3_controls_and_all_shortcuts_dead():
    verdict, _ = classify_ref_validation(
        _checks([1, 1, 1], [0, 0, 0], [("minimal", 0.0)])
    )
    assert verdict == "repaired"


@pytest.mark.parametrize(
    ("reference", "nop", "shortcuts", "expected"),
    [
        ([1, 1, 0], [0, 0, 0], [("minimal", 0.0)], "rejected"),
        ([1, 1, 1], [0, 0, 1], [("minimal", 0.0)], "rejected"),
        ([1, 1, 1], [0, 0, 0], [("minimal", 1.0)], "rejected"),
        ([1, 1], [0, 0, 0], [("minimal", 0.0)], "blocked"),
    ],
)
def test_classify_rejects_or_blocks_everything_else(reference, nop, shortcuts, expected):
    verdict, _ = classify_ref_validation(_checks(reference, nop, shortcuts))
    assert verdict == expected


def test_classify_blocks_unscored_grades():
    rows = _checks([1, 1, 1], [0, 0, 0], [("minimal", 0.0)])
    rows[0] = RefRepairCheck(name="reference-1", role="reference", status="timeout")
    verdict, reason = classify_ref_validation(rows)
    assert verdict == "blocked"
    assert "reference-1" in reason


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    (package / "tests").mkdir(parents=True)
    (package / "task.toml").write_text('[environment]\nworkdir = "/testbed"\n')
    (package / "instruction.md").write_text("Fix the thing.\n")
    (package / "tests" / "test.patch").write_text("")
    (package / "tests" / "test_command.sh").write_text("python -m pytest tests/ -q\n")
    (package / "tests" / "test.sh").write_text(
        "<<'EOF_RESET_TEST_FILES'\ntest_commands.json\nEOF_RESET_TEST_FILES\n"
    )
    return package


def _plan(tmp_path: Path) -> RefRepairPlan:
    package = _package(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    report = _report(package=str(package))
    (report_dir / "report.json").write_text(json.dumps(report))
    (report_dir / "dropped.patch").write_text("diff --git a/x b/x\n")
    return plan_ref_repair(report_dir)


def test_execute_with_fakes_repairs_and_writes_receipts(tmp_path):
    plan = _plan(tmp_path)
    shortcut_ids = [s.id for s in plan.shortcuts]
    proposal = {
        "schema": "evallab.verifier_repair.proposal/v1",
        "repairable": True,
        "rationale": "cover the omitted behaviour",
        "tests": [
            {
                "id": "t1",
                "repo_path": "tests/test_ref_repair_added.py",
                "code": "def test_added():\n    assert True\n",
            }
        ],
        "command": "python -m pytest tests/test_ref_repair_added.py -q",
        "model": None,
        "mutant_ids": shortcut_ids,
        "unrepairable": [],
    }

    def fake_derive(parent, **kwargs):
        return SimpleNamespace(
            variant_digest="sha256:" + "a" * 64,
            record_relpath=lambda: Path("library/task-variants/rec.json"),
        )

    def fake_grader(**kwargs):
        return _checks([1, 1, 1], [0, 0, 0], [(s, 0.0) for s in shortcut_ids])
    out = tmp_path / "attempt"
    result = execute_ref_repair(
        plan,
        model="gpt-6.1-sol",
        repo_root=tmp_path,
        output_dir=out,
        quota_checker=lambda: QuotaStatus(allowed=True, weekly_used=0.1),
        model_caller=lambda prompt, context: (json.dumps(proposal), {"input_tokens": 1}),
        grader=fake_grader,
        derive_fn=fake_derive,
        materialize_fn=lambda record, parent, repo_root=None: Path(parent),
    )
    assert result.verdict == "repaired"
    assert (out / "repair-result.json").is_file()
    assert (out / "proposal.json").is_file()
    assert json.loads((out / "quota.json").read_text())["weekly_used"] == 0.1


def test_execute_quota_blocked_never_calls_model(tmp_path):
    plan = _plan(tmp_path)
    called = False

    def caller(prompt, context):
        nonlocal called
        called = True
        return "{}", {}

    result = execute_ref_repair(
        plan,
        model="gpt-6.1-sol",
        repo_root=tmp_path,
        output_dir=tmp_path / "attempt",
        quota_checker=lambda: QuotaStatus(allowed=True, weekly_used=0.9),
        model_caller=caller,
        grader=lambda **kwargs: [],
        derive_fn=lambda parent, **kwargs: None,
    )
    assert result.verdict == "blocked"
    assert called is False
