"""Ref-subset repair: shortcut intake, x3 acceptance, fake-runtime orchestration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.ref_repair import (
    RefRepairCheck,
    RefRepairPlan,
    classify_ref_validation,
    execute_ref_repair,
    load_shortcuts,
    plan_ref_repair,
)
from evallab.registry import compute_task_digests
from evallab.verifier_repair import QuotaStatus


def _report(**overrides):
    report = {
        "schema": "evallab.ref_subset.report/v1",
        "task_id": "format-code-task-000001",
        "verdict": "fail",
        "package": "/nonexistent",
        "blocks": [
            {"id": "pkg/mod.py#0", "path": "pkg/mod.py", "kind": "code"},
            {"id": "pkg/mod.py#1", "path": "pkg/mod.py", "kind": "code"},
            {"id": "pkg/other.py#0", "path": "pkg/other.py", "kind": "code"},
            {"id": "README.md#0", "path": "README.md", "kind": "noncode"},
        ],
        "kept": ["pkg/mod.py#0"],
        "grades": [
            {"label": "full", "kept": 4, "reward": 1.0, "status": "scored"},
            {"label": "nop", "kept": 0, "reward": 0.0, "status": "scored"},
            {"label": "drop-docs-comments", "kept": 3, "reward": 1.0, "status": "scored"},
            {"label": "drop-file:pkg/other.py", "kept": 2, "reward": 1.0, "status": "scored"},
            {"label": "drop-block:pkg/mod.py#1", "kept": 1, "reward": 1.0, "status": "scored"},
            {"label": "confirm", "kept": 1, "reward": 1.0, "status": "scored"},
        ],
    }
    report.update(overrides)
    return report


def test_load_shortcuts_replays_cumulative_passing_removals():
    by_label = {s.label: s for s in load_shortcuts(_report())}
    assert set(by_label) == {"minimal", "drop-file:pkg/other.py"}
    # A later success removes from an already reduced submission.
    assert by_label["minimal"].keep == frozenset({"pkg/mod.py#0"})
    assert by_label["drop-file:pkg/other.py"].keep == frozenset({"pkg/mod.py#0", "pkg/mod.py#1"})


def test_load_shortcuts_has_none_when_nothing_passed_or_probe_found_every_block_needed():
    report = _report()
    report["grades"] = report["grades"][:2]
    report["kept"] = [b["id"] for b in report["blocks"]]
    assert load_shortcuts(report) == []
    assert load_shortcuts(_report(verdict="pass")) == []


def test_plan_refuses_a_pass_verdict_without_real_partial_fixes(tmp_path):
    package = _package(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    (report_dir / "report.json").write_text(json.dumps(_bound_report(package, verdict="pass")))
    with pytest.raises(ValueError, match="nothing to repair"):
        plan_ref_repair(report_dir)


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
        _checks([1, 1, 1], [0, 0, 0], [("minimal", 0.0)]), shortcut_ids=["minimal"]
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
    verdict, _ = classify_ref_validation(
        _checks(reference, nop, shortcuts), shortcut_ids=["minimal"]
    )
    assert verdict == expected


def test_classify_blocks_unscored_grades():
    rows = _checks([1, 1, 1], [0, 0, 0], [("minimal", 0.0)])
    rows[0] = RefRepairCheck(name="reference-0", role="reference", status="timeout")
    verdict, _ = classify_ref_validation(rows, shortcut_ids=["minimal"])
    assert verdict == "blocked"


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    (package / "tests").mkdir(parents=True)
    (package / "environment" / "setup").mkdir(parents=True)
    (package / "environment" / "setup" / "setup.sh").write_text("#!/bin/sh\n")
    (package / "task.toml").write_text(
        '[metadata]\nsource_id = "format-code-task-000001"\n'
        '[environment]\nworkdir = "/testbed"\ndocker_image = "local-fixture:fixed"\n'
    )
    (package / "instruction.md").write_text("Fix the thing.\n")
    (package / "tests" / "test.patch").write_text("")
    (package / "tests" / "test_command.sh").write_text("python -m pytest tests/ -q\n")
    (package / "tests" / "test.sh").write_text(
        "<<'EOF_RESET_TEST_FILES'\ntest_commands.json\nEOF_RESET_TEST_FILES\n"
    )
    return package


def _bound_report(package, **overrides):
    reference = package.parent / "reference.patch"
    reference.write_text("diff --git a/pkg/mod.py b/pkg/mod.py\n")
    return _report(
        package=str(package),
        package_digest=compute_task_digests(package).package,
        reference={
            "patch": str(reference),
            "sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
        },
        **overrides,
    )


def _plan(tmp_path: Path) -> RefRepairPlan:
    package = _package(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    report = _bound_report(package)
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
            variant_digest=compute_task_digests(parent).package,
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
        status_fn=lambda *a, **k: None,
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


def test_classify_partial_only_when_every_escape_is_declared():
    rows = _checks([1, 1, 1], [0, 0, 0], [("a", 0.0), ("netlify", 1.0), ("fixture", 1.0)])
    verdict, reason = classify_ref_validation(
        rows,
        shortcut_ids=["a", "netlify", "fixture"],
        declared={"netlify": "deploy config", "fixture": "test fixture"},
    )
    assert verdict == "partial"
    # One undeclared escape keeps the whole attempt rejected.
    verdict, _ = classify_ref_validation(
        rows, shortcut_ids=["a", "netlify", "fixture"], declared={"netlify": "deploy config"}
    )
    assert verdict == "rejected"


def test_classify_partial_never_forgives_broken_controls():
    rows = _checks([1, 0, 1], [0, 0, 0], [("netlify", 1.0)])
    verdict, _ = classify_ref_validation(
        rows, shortcut_ids=["netlify"], declared={"netlify": "deploy config"}
    )
    assert verdict == "rejected"


def test_source_only_patch_drops_environment_artefacts_but_keeps_source():
    from evallab.ref_repair import source_only_patch

    patch = (
        "diff --git a/.venv/bin/activate b/.venv/bin/activate\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/.venv/bin/activate\n@@ -0,0 +1 @@\n+x\n"
        "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/talon.log b/talon.log\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/talon.log\n@@ -0,0 +1 @@\n+noise\n"
    )
    cleaned = source_only_patch(patch)
    assert "pkg/mod.py" in cleaned
    assert ".venv" not in cleaned
    assert "talon.log" not in cleaned


def test_report_less_task_needs_real_partial_fix_and_reference(tmp_path):
    package = _package(tmp_path)
    reference = tmp_path / "solution.patch"
    reference.write_text("diff --git a/x b/x\n")
    empty_dir = tmp_path / "no-report"
    with pytest.raises(ValueError, match="--package and --reference-patch"):
        plan_ref_repair(empty_dir)
    with pytest.raises(ValueError, match="nothing to repair"):
        plan_ref_repair(empty_dir, package=package, reference_patch=reference)
    diff = tmp_path / "agent.diff"
    diff.write_text("diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n")
    plan = plan_ref_repair(
        empty_dir,
        package=package,
        reference_patch=reference,
        partial_fixes=[("stock", diff)],
    )
    assert [s.id for s in plan.shortcuts] == ["partial-stock"]
    assert plan.shortcuts[0].patch is not None


@pytest.mark.parametrize(
    "observed",
    [[], [("minimal", 0.0), ("minimal", 0.0)], [("different", 0.0)]],
)
def test_missing_duplicate_or_substituted_shortcuts_cannot_certify_repair(observed):
    verdict, _ = classify_ref_validation(
        _checks([1, 1, 1], [0, 0, 0], observed), shortcut_ids=["minimal"]
    )
    assert verdict == "blocked"


def test_duplicate_controls_cannot_fill_the_repetition_quorum():
    checks = _checks([1, 1, 1], [0, 0, 0], [("minimal", 0.0)])
    checks[1] = checks[0]
    verdict, _ = classify_ref_validation(checks, shortcut_ids=["minimal"])
    assert verdict == "blocked"


def test_fewer_than_three_runs_cannot_lower_the_acceptance_bar():
    verdict, _ = classify_ref_validation(
        _checks([1], [0], [("minimal", 0.0)]), shortcut_ids=["minimal"], repetitions=1
    )
    assert verdict == "blocked"


def test_shortcut_intake_rejects_history_that_cannot_be_reconstructed():
    report = _report()
    report["grades"][4]["kept"] = 2
    with pytest.raises(ValueError):
        load_shortcuts(report)


def test_retained_proposal_needs_no_model_or_fabricated_quota(tmp_path):
    plan = _plan(tmp_path)
    proposal = tmp_path / "retained.json"
    proposal.write_text(
        json.dumps(
            {
                "schema": "evallab.verifier_repair.proposal/v1",
                "repairable": False,
                "rationale": "The omitted changes are outside the task contract.",
            }
        )
    )

    def forbidden(*args, **kwargs):
        pytest.fail(
            "a retained proposal must not read quota, call a model or grade a declined repair"
        )

    result = execute_ref_repair(
        plan,
        model="gpt-6.1-sol",
        repo_root=tmp_path,
        output_dir=tmp_path / "attempt",
        proposal_path=proposal,
        quota_checker=forbidden,
        model_caller=forbidden,
        grader=forbidden,
    )
    assert result.verdict == "unrepairable"
    usage = json.loads((tmp_path / "attempt" / "model-usage.json").read_text())
    assert usage["new_model_call"] is False
    assert not (tmp_path / "attempt" / "quota.json").exists()


@pytest.mark.parametrize("changed", ["task_id", "package", "reference"])
def test_report_binding_rejects_changed_task_or_source_bytes(tmp_path, changed):
    from evallab.ref_repair import load_ref_report

    package = _package(tmp_path)
    report = _bound_report(package)
    directory = tmp_path / "report"
    directory.mkdir()
    if changed == "task_id":
        report["task_id"] = "another-task"
    elif changed == "package":
        (package / "instruction.md").write_text("Different instruction.\n")
    else:
        Path(report["reference"]["patch"]).write_text("Different reference.\n")
    (directory / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        load_ref_report(directory)


@pytest.mark.parametrize(
    "metadata",
    ["new file mode 120000", "old mode 100644\nnew mode 100755", "GIT binary patch"],
)
def test_byte_only_replay_refuses_unsupported_file_types(metadata):
    from evallab.ref_repair import _checked_patch_paths

    with pytest.raises(ValueError):
        _checked_patch_paths("diff --git a/pkg/item.py b/pkg/item.py\n" + metadata + "\n")
