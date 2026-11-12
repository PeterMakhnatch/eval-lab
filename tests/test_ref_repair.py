"""Ref-subset repair: shortcut intake, x3 acceptance, fake-runtime orchestration."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

import pytest

from evallab.ref_repair import (
    RefRepairCheck,
)
from evallab.registry import compute_task_digests


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


@pytest.mark.parametrize(
    "metadata",
    ["new file mode 120000", "old mode 100644\nnew mode 100755", "GIT binary patch"],
)
def test_byte_only_replay_refuses_unsupported_file_types(metadata):
    from evallab.ref_repair import _checked_patch_paths

    with pytest.raises(ValueError):
        _checked_patch_paths("diff --git a/pkg/item.py b/pkg/item.py\n" + metadata + "\n")


def _supported_witness_inputs(tmp_path):
    package = _package(tmp_path)
    base = "def total(values):\n    return 0\n"
    reference = "def total(values):\n    return sum(values)\n"
    candidate = "def total(values):\n    return sum(values[:-1])\n"

    def patch(before, after):
        return "diff --git a/pkg/mod.py b/pkg/mod.py\n" + "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="a/pkg/mod.py",
                tofile="b/pkg/mod.py",
            )
        )

    directory = tmp_path / "report"
    (directory / "mutants").mkdir(parents=True)
    report = _bound_report(
        package,
        mutants={
            "verdict": "fail",
            "survivors": [{"id": "line-mutant", "edit": patch(reference, candidate)}],
        },
    )
    reference_path = Path(report["reference"]["patch"])
    reference_path.write_text(patch(base, reference))
    report["reference"]["sha256"] = hashlib.sha256(reference_path.read_bytes()).hexdigest()
    for grade in report["grades"]:
        grade["rc"] = 0
    (directory / "report.json").write_text(json.dumps(report))
    complete = patch(base, candidate)
    (directory / "mutants" / "line-mutant.patch").write_text(complete)
    (directory / "grades.log").write_text(
        "== full reward=1.0 rc=0 scored 0.1s\n"
        "== nop reward=0.0 rc=0 scored 0.1s\n"
        "== mutant:line-mutant reward=1.0 rc=0 scored 0.1s\n"
    )
    root = tmp_path / "findings-repository"
    key = root / "research" / "answer-keys" / "mimo-weak-tests-v2"
    (key / "witnesses").mkdir(parents=True)
    witness_path = key / "witnesses" / "line-mutant.patch"
    witness_path.write_text(complete)
    finding = {
        "schema": "qa.finding/v1",
        "id": f"mimo-weak-tests-v2:{report['task_id']}:line-mutant",
        "subject": {
            "kind": "task",
            "task_id": "mimo-v2.6-rl/" + report["task_id"],
            "task_digest": report["package_digest"],
        },
        "stage": "S2.ref_mutants",
        "verdict": "fail",
        "category": "weak",
        "severity": "high",
        "fault": "eval",
        "measurement": "measured",
        "reproducer": {
            "kind": "patch",
            "path": witness_path.relative_to(root).as_posix(),
            "expected_reward": 1.0,
        },
        "refutation": {"status": "verified", "by": "Traces semantic adjudication"},
        "reviewer": {"agent": "Traces"},
    }
    findings = key / "findings-supported-v2.jsonl"
    findings.write_text(json.dumps(finding) + "\n")
    return directory, findings, root, base


