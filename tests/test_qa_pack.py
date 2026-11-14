"""Hermetic regression-pack validation, staging, and measured replay contracts."""

from __future__ import annotations

import argparse
import difflib
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.dataset_audit_contracts import AuditObservation
from evallab.qa import pack
from evallab.qa.parser import build_qa_parser
from evallab.registry import task_directory_digest


@pytest.fixture(autouse=True)
def _isolate_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("EVALLAB_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pack, "utcnow", lambda: "2026-11-14T12:00:00+00:00")

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("pack replay must not contact a network in this test")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@dataclass
class RegressionPack:
    directory: Path
    task: Path
    manifest: dict[str, Any]
    finding: dict[str, Any]

    def save(self) -> None:
        _write_json(self.directory / "pack.json", self.manifest)
        _write_json(self.directory / "finding.json", self.finding)

    def patch(self, before: str, after: str, target: str = "tests/test_answer.py") -> None:
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{target}",
                tofile=f"b/{target}",
            )
        )
        (self.directory / "fix/tests.patch").write_text(diff, encoding="utf-8")


ORIGINAL_TEST = "def test_answer():\n    assert 42 == 42\n"
STRENGTHENED_TEST = ORIGINAL_TEST + '    assert len("42") == 2\n'


@pytest.fixture
def regression_pack(tmp_path: Path) -> RegressionPack:
    task = tmp_path / "task"
    for directory in ("tests", "solution", "environment"):
        (task / directory).mkdir(parents=True)
    files = {
        "task.toml": (
            'schema_version = "1.0"\n[task]\nname = "bench/answer"\n'
            '[verifier]\nenvironment_mode = "separate"\n'
        ),
        "instruction.md": "Write exactly 42 to /app/answer.txt.\n",
        "tests/test_answer.py": ORIGINAL_TEST,
        "tests/test.sh": "#!/bin/bash\npython -m pytest /tests/test_answer.py\n",
        "solution/solve.sh": "#!/bin/bash\nprintf 42 > /app/answer.txt\n",
        "environment/Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\n",
    }
    for name, content in files.items():
        (task / name).write_text(content, encoding="utf-8")
    directory = tmp_path / "pack"
    (directory / "fix").mkdir(parents=True)
    for name, content in {
        "requirement.md": "instruction.md:1 requires exactly 42; the witness writes 0.\n",
        "witness.sh": "#!/bin/bash\nprintf 0 > /app/answer.txt\n",
        "alt.sh": "#!/bin/bash\necho -n 42 > /app/answer.txt\n",
        "check.sh": '#!/bin/bash\ntest "$(cat /app/answer.txt)" = 42\n',
        "REPLAY.md": "evallab qa replay . --task ../task --plan\n",
    }.items():
        (directory / name).write_text(content, encoding="utf-8")
    (directory / "receipts").mkdir()
    digest = task_directory_digest(task)
    cells = [
        ("oracle-original", "oracle", "original", None, 1),
        ("nop-original", "nop", "original", None, 0),
        ("witness-original", "witness.sh", "original", None, 1),
        ("oracle-check", "oracle", "none", "check.sh", "pass"),
        ("witness-check", "witness.sh", "none", "check.sh", "fail"),
        ("witness-fixed", "witness.sh", "fixed", None, 0),
        ("oracle-fixed", "oracle", "fixed", None, 1),
        ("alt-original", "alt.sh", "original", None, 1),
        ("alt-fixed", "alt.sh", "fixed", None, 1),
        ("alt-check", "alt.sh", "none", "check.sh", "pass"),
    ]
    manifest = {
        "schema": "qa.pack/v1",
        "task": {
            "dataset": "fixture", "version": "1", "task_id": "bench/answer",
            "package_digest": digest, "source_url": "https://example.invalid/task",
            "source_commit": "a" * 40,
        },
        "defect_class": "wrong_pass", "layer": "task_package",
        "claim": "The grader accepts the wrong answer.",
        "requirement": {
            "file": "instruction.md", "lines": [1, 1],
            "quote": "Write exactly 42 to /app/answer.txt.",
        },
        "prior_reports": [],
        "cells": [
            dict(id=ident, submission=submission, grader=grader, check=check, expect=expect)
            for ident, submission, grader, check, expect in cells
        ],
        "fix": {"patch": "fix/tests.patch"},
        "authors": {
            "witness": {"model": "fixture-a", "lane": "test"},
            "fix": {"model": "fixture-b", "lane": "test"}, "verifier": None,
        },
        "status": "candidate",
    }
    finding = {
        "schema": "qa.finding/v1", "id": "answer-wrong-pass",
        "subject": {"kind": "task", "task_id": "bench/answer", "task_digest": digest},
        "stage": "S3", "verdict": "fail", "category": "weak",
        "severity": "high", "fault": "eval",
        "reproducer": {"kind": "command", "path": "witness.sh", "expected_reward": 1},
        "reviewer": {"agent": "fixture", "model": "fixture-a"},
    }
    fixture = RegressionPack(directory, task, manifest, finding)
    fixture.patch(ORIGINAL_TEST, STRENGTHENED_TEST)
    fixture.save()
    return fixture


@pytest.mark.parametrize("location", ["submission", "check", "patch", "reproducer", "evidence"])
@pytest.mark.parametrize("absolute", [False, True], ids=["escape", "absolute"])
def test_pack_paths_refuse_escape_and_absolute_files(
    regression_pack: RegressionPack, tmp_path: Path, location: str, absolute: bool,
) -> None:
    fixture = regression_pack
    outside = tmp_path / "outside.sh"
    outside.write_text("exit 0\n", encoding="utf-8")
    path = str(outside) if absolute else "../outside.sh"
    if location == "submission":
        fixture.manifest["cells"][2]["submission"] = path
    elif location == "check":
        fixture.manifest["cells"][3]["check"] = path
    elif location == "patch":
        fixture.manifest["fix"]["patch"] = path
    elif location == "reproducer":
        fixture.finding["reproducer"]["path"] = path
    else:
        fixture.finding["evidence_refs"] = [path]
    fixture.save()
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


def test_pack_refuses_symlink_escape(regression_pack: RegressionPack, tmp_path: Path) -> None:
    fixture = regression_pack
    outside = tmp_path / "outside.sh"
    outside.write_text("exit 0\n", encoding="utf-8")
    (fixture.directory / "linked.sh").symlink_to(outside)
    fixture.finding["reproducer"]["path"] = "linked.sh"
    fixture.save()
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


def test_pack_refuses_task_digest_mismatch(regression_pack: RegressionPack) -> None:
    fixture = regression_pack
    (fixture.task / "instruction.md").write_text("Changed task.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        pack.validate_pack(fixture.directory, fixture.task)


@pytest.mark.parametrize("field,value", [("task_id", "other"), ("task_digest", "sha256:" + "0" * 64)])
def test_finding_must_bind_to_manifest_subject(
    regression_pack: RegressionPack, field: str, value: str,
) -> None:
    fixture = regression_pack
    fixture.finding["subject"][field] = value
    fixture.save()
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


@pytest.mark.parametrize("invalid", ["verdict", "missing-schema", "wrong-schema"])
def test_pack_refuses_invalid_existing_finding_schema(
    regression_pack: RegressionPack, invalid: str,
) -> None:
    fixture = regression_pack
    if invalid == "verdict":
        fixture.finding["verdict"] = "invented"
    elif invalid == "missing-schema":
        del fixture.finding["schema"]
    else:
        fixture.finding["schema"] = "qa.finding/v2"
    fixture.save()
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


def test_required_cell_matches_semantics_not_its_id(regression_pack: RegressionPack) -> None:
    fixture = regression_pack
    fixture.manifest["cells"][1]["submission"] = "oracle"
    fixture.save()
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


def test_pack_refuses_missing_required_check_cell(regression_pack: RegressionPack) -> None:
    fixture = regression_pack
    fixture.manifest["cells"].pop(4)
    fixture.save()
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


@pytest.mark.parametrize(
    "replacement",
    [
        "def test_answer():\n    pass\n",
        "import pytest\n@pytest.mark.skip\n" + ORIGINAL_TEST,
        ORIGINAL_TEST + "    pytest.xfail()\n",
    ],
    ids=["deleted-assertion", "added-skip", "added-xfail"],
)
def test_patch_refuses_weakened_tests(regression_pack: RegressionPack, replacement: str) -> None:
    fixture = regression_pack
    fixture.patch(ORIGINAL_TEST, replacement)
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


def test_strengthening_and_arbitrary_cell_ids_validate(regression_pack: RegressionPack) -> None:
    fixture = regression_pack
    for index, cell in enumerate(fixture.manifest["cells"]):
        cell["id"] = f"case-{index}"
    fixture.save()
    validated = pack.validate_pack(fixture.directory, fixture.task)
    assert validated["task"]["package_digest"] == task_directory_digest(fixture.task)
    assert len(validated["cells"]) == 10


def test_patch_refuses_changes_outside_tests(regression_pack: RegressionPack) -> None:
    fixture = regression_pack
    fixture.patch("old\n", "new\n", "solution/solve.sh")
    with pytest.raises(ValueError):
        pack.validate_pack(fixture.directory, fixture.task)


def test_fixed_staging_leaves_original_and_pack_unchanged(
    regression_pack: RegressionPack, tmp_path: Path,
) -> None:
    fixture = regression_pack
    original = _snapshot(fixture.task)
    pack_bytes = _snapshot(fixture.directory)
    staged = pack.stage_fixed(fixture.task, fixture.directory / "fix/tests.patch", tmp_path / "fixed")
    assert staged.resolve() != fixture.task.resolve()
    assert (staged / "tests/test_answer.py").read_text(encoding="utf-8") == STRENGTHENED_TEST
    assert task_directory_digest(staged) != task_directory_digest(fixture.task)
    (staged / "tests/test_answer.py").write_text("staged edit\n", encoding="utf-8")
    assert _snapshot(fixture.task) == original
    assert _snapshot(fixture.directory) == pack_bytes


class FakeNativeBackend:
    def __init__(self, cells: list[dict[str, Any]], outcomes: dict[str, Any] | None = None) -> None:
        self.cells = cells
        self.outcomes = outcomes or {}
        self.calls: list[tuple[Path, dict[str, Any]]] = []
        self.gates: list[Any] = []

    def factory(self, *, repo_root: Path, spend_gate: Any) -> FakeNativeBackend:
        self.gates.append(spend_gate)
        return self

    def _native_trial(self, package: Path, **kwargs: Any) -> AuditObservation:
        cell = self.cells[len(self.calls)]
        self.calls.append((package, kwargs))
        result = self.outcomes.get(cell["id"], cell["expect"])
        trial = kwargs["trial_dir"]
        (trial / "agent").mkdir(parents=True, exist_ok=True)
        exception = "fixture timeout" if result == "exception" else None
        reward = result if isinstance(result, (int, float)) else None
        if cell["grader"] == "none":
            assert kwargs["verifier"] is False
            if result is not None:
                code = 0 if result in ("pass", "exception") else 1
                (trial / "agent/replay-check-exit-code.txt").write_text(str(code), encoding="utf-8")
        else:
            assert kwargs.get("verifier", True) is True
        _write_json(trial / "result.json", {"exception_info": exception, "verifier_result": {"rewards": {"reward": reward}}})
        return AuditObservation(
            stage="oracle", status="failed" if exception else "executed",
            facts={"reward": reward, "exception": exception}, reason=exception,
        )


def _run(fixture: RegressionPack, tmp_path: Path, backend: FakeNativeBackend, **kwargs: Any) -> dict[str, Any]:
    return pack.replay(
        fixture.directory, fixture.task, max_usd=2, approve="hermetic test; no cloud calls",
        run_dir=tmp_path / "run", executor_factory=backend.factory, **kwargs,
    )


@pytest.mark.parametrize(
    "outcomes,matched,result",
    [({}, True, 1), ({"oracle-original": 0}, False, 0), ({"oracle-original": None}, False, "unmeasured")],
    ids=["all-match", "mismatch", "unmeasured"],
)
def test_replay_requires_every_measured_expectation(
    regression_pack: RegressionPack, tmp_path: Path, outcomes: dict[str, Any], matched: bool, result: Any,
) -> None:
    fixture = regression_pack
    task_bytes, pack_bytes = _snapshot(fixture.task), _snapshot(fixture.directory)
    backend = FakeNativeBackend(fixture.manifest["cells"], outcomes)
    report = _run(fixture, tmp_path, backend)
    assert report["matched"] is matched
    assert report["cells"][0]["result"] == result
    assert report["cells"][0]["measurement"] == ("unmeasured" if result == "unmeasured" else "measured")
    assert json.loads((tmp_path / "run/replay.json").read_text())["matched"] is matched
    assert len(backend.calls) == 10
    assert len({call[1]["trial_dir"] for call in backend.calls}) == 10
    assert len({id(gate) for gate in backend.gates}) == 1
    digests = {cell["id"]: cell["variant_digest"] for cell in report["cells"]}
    assert digests["oracle-original"] == task_directory_digest(fixture.task)
    assert digests["oracle-fixed"] != digests["oracle-original"]
    assert all(cell["native_results"] for cell in report["cells"])
    assert _snapshot(fixture.task) == task_bytes
    assert _snapshot(fixture.directory) == pack_bytes


def test_check_marker_does_not_override_native_failure(
    regression_pack: RegressionPack, tmp_path: Path,
) -> None:
    fixture = regression_pack
    backend = FakeNativeBackend(fixture.manifest["cells"], {"oracle-check": "exception"})
    report = _run(fixture, tmp_path, backend)
    check = next(cell for cell in report["cells"] if cell["id"] == "oracle-check")
    assert check["result"] == "unmeasured"
    assert check["reason"]
    assert report["matched"] is False


def test_selected_cells_cannot_hide_required_measurements(
    regression_pack: RegressionPack, tmp_path: Path,
) -> None:
    fixture = regression_pack
    backend = FakeNativeBackend(fixture.manifest["cells"][:1])
    report = _run(fixture, tmp_path, backend, cells=["oracle-original"])
    assert len(backend.calls) == 1
    assert report["matched"] is False


def test_plan_never_constructs_backend_or_writes_files(
    regression_pack: RegressionPack, tmp_path: Path,
) -> None:
    fixture = regression_pack
    before = _snapshot(fixture.directory)

    def forbidden_factory(**kwargs: Any) -> Any:
        raise AssertionError("--plan constructed an execution backend")

    report = pack.replay(
        fixture.directory, fixture.task, plan=True, max_usd=2,
        run_dir=tmp_path / "unused", executor_factory=forbidden_factory,
    )
    assert len(report["cells"]) == 10
    assert report["execution_upper_bound"] == 7 * 6 + 3 * 3
    assert report["estimated_usd"] == pytest.approx(51 * 0.0025)
    assert not (tmp_path / "unused").exists()
    assert _snapshot(fixture.directory) == before


def test_shared_grader_plan_reserves_only_one_environment_per_cell(
    regression_pack: RegressionPack,
) -> None:
    fixture = regression_pack
    task_config = fixture.task / "task.toml"
    task_config.write_text(
        task_config.read_text(encoding="utf-8").replace(
            'environment_mode = "separate"', 'environment_mode = "shared"',
        ),
        encoding="utf-8",
    )
    digest = task_directory_digest(fixture.task)
    fixture.manifest["task"]["package_digest"] = digest
    fixture.finding["subject"]["task_digest"] = digest
    fixture.save()
    report = pack.replay(fixture.directory, fixture.task, plan=True, max_usd=2)
    assert report["execution_upper_bound"] == 30
    assert report["estimated_usd"] == pytest.approx(30 * 0.0025)
    assert all(cell["execution_upper_bound"] == 3 for cell in report["cells"])


@pytest.mark.parametrize("outcome", [1, 0, None], ids=["match", "mismatch", "unmeasured"])
def test_cli_exit_status_reports_actual_replay(
    regression_pack: RegressionPack, tmp_path: Path, capsys: pytest.CaptureFixture[str], outcome: int | None,
) -> None:
    fixture = regression_pack
    parser = argparse.ArgumentParser()
    build_qa_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args([
        "qa", "replay", str(fixture.directory), "--task", str(fixture.task),
        "--run-dir", str(tmp_path / "cli-run"), "--max-usd", "2", "--approve", "hermetic test",
    ])
    backend = FakeNativeBackend(fixture.manifest["cells"], {"oracle-original": outcome})
    status = args.func(
        args, tmp_path, harbor="fake unused CLI dispatch", executor_factory=backend.factory,
    )
    report = json.loads(capsys.readouterr().out)
    assert status == (0 if outcome == 1 else 1)
    assert report["matched"] is (outcome == 1)
    assert report["cells"][0]["result"] == ("unmeasured" if outcome is None else outcome)


def test_cli_plan_validates_without_backend(
    regression_pack: RegressionPack, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = regression_pack
    parser = argparse.ArgumentParser()
    build_qa_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args([
        "qa", "replay", str(fixture.directory), "--task", str(fixture.task), "--plan",
    ])

    def forbidden_factory(**kwargs: Any) -> Any:
        raise AssertionError("CLI --plan constructed an execution backend")

    assert args.func(
        args, tmp_path, harbor="fake unused CLI dispatch", executor_factory=forbidden_factory,
    ) == 0
    assert len(json.loads(capsys.readouterr().out)["cells"]) == 10
