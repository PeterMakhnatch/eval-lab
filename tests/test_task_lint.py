from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.task_lint import lint_task


@pytest.fixture
def task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "solution").mkdir(parents=True)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text("FROM python:3.12\nCOPY app/ /app\n")
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json"]\n[verifier]\nenvironment_mode = "separate"\n'
    )
    return task


@pytest.fixture(autouse=True)
def isolated_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    monkeypatch.setattr(cli, "load_local_env", lambda _path: None)


def test_clean_separate_task_is_read_only(task: Path, capsys) -> None:
    (task / "controls").mkdir()
    (task / "controls/positive.sh").write_text("#!/bin/sh\n# expect: 1\nexit 0\n")
    (task / "controls/negative.sh").write_text("#!/bin/sh\n# expect: 0\nexit 0\n")
    before = {path: path.read_bytes() for path in task.rglob("*") if path.is_file()}

    assert lint_task(task) == []
    assert cli.run_cli(["tasks", "lint", str(task), "--json"], workspace=task.parent) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert {path: path.read_bytes() for path in task.rglob("*") if path.is_file()} == before


def test_shared_verifier_warns_without_failing(task: Path, capsys) -> None:
    (task / "task.toml").write_text('[verifier]\nenvironment_mode = "shared"\n')
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("verifier-isolation", "warning")
    assert cli.run_cli(["tasks", "lint", str(task)], workspace=task.parent) == 0
    assert capsys.readouterr().out == (
        f"warning verifier-isolation {task / 'task.toml'}: {finding.message}\n"
    )


def test_separate_verifier_requires_artifacts(task: Path, capsys) -> None:
    (task / "task.toml").write_text('[verifier]\nenvironment_mode = "separate"\n')
    assert cli.run_cli(["tasks", "lint", str(task), "--json"], workspace=task.parent) == 1
    finding, = json.loads(capsys.readouterr().out)
    assert (finding["rule"], finding["severity"]) == ("artifacts-declared", "error")
    assert finding["path"] == str(task / "task.toml")


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY tests/ /tests",
        "COPY tests /private",
        'COPY ["solution", "/private"]',
        'COPY --chown=1000:1000 --link ["app", "nested/tests", "/private"]',
        "ADD --chown=1000:1000\tpublic solution /private",
        'COPY "nested directory/tests" /private',
        'add ["./SoLuTiOn/", "/hidden"]',
        "COPY \\\n# comment between continued lines\n  tests/ /tests",
        "COPY --chown=1000:1000 \\\n  app \\\n# comment\n  solution /private",
    ],
)
def test_hidden_verifier_inputs_cannot_enter_agent_image(task: Path, instruction: str) -> None:
    (task / "environment/Dockerfile").write_text(f"FROM python:3.12\n{instruction}\n")
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("hidden-inputs-not-baked", "error")


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY contests/ public+tests/ /app",
        "COPY app/ /solution/",
        'ADD ["app", "/tests/"]',
        'COPY --from=tests --chown=1000:1000 ["app", "/solution"]',
        "COPY --link \\\n  app /tests/",
    ],
)
def test_dockerfile_comments_and_unrelated_paths_do_not_flag(task: Path, instruction: str) -> None:
    (task / "environment/Dockerfile").write_text(
        f"FROM python:3.12\n# COPY tests/ /tests\n{instruction}\n"
    )
    assert lint_task(task) == []


@pytest.mark.parametrize(
    "instruction",
    [
        'COPY ["tests", "/private"',
        'ADD ["app", 42]',
        'COPY ["app", ""]',
        "COPY app",
        "ADD",
        'COPY "app /private',
        "COPY --chown= app /private",
        "COPY --chown app /private",
        "COPY app /private \\",
        "COPY <<EOF /private\ncontent\nEOF",
    ],
)
def test_malformed_copy_reports_actionable_error(task: Path, instruction: str, capsys) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(f"FROM python:3.12\n{instruction}\n")
    assert cli.run_cli(["tasks", "lint", str(task), "--json"], workspace=task.parent) == 1
    finding, = json.loads(capsys.readouterr().out)
    assert (finding["rule"], finding["severity"]) == ("dockerfile-copy-parses", "error")
    assert finding["path"] == str(dockerfile)
    assert "line 2:" in finding["message"]


def test_unsupported_escape_directive_is_not_silently_accepted(task: Path) -> None:
    (task / "environment/Dockerfile").write_text("# escape=`\nFROM python:3.12\n")
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("dockerfile-copy-parses", "error")


def test_non_utf8_dockerfile_reports_error(task: Path) -> None:
    (task / "environment/Dockerfile").write_bytes(b"FROM python:3.12\nCOPY \xff /private\n")
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("dockerfile-copy-parses", "error")


def test_control_requires_exact_expectation_header(task: Path) -> None:
    (task / "controls").mkdir()
    control = task / "controls/forged-reward.sh"
    control.write_text("#!/bin/sh\n# expect: 10\necho '# expect: 0'\n")
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("controls-declare-expectation", "error")
    assert finding.path == str(control)


def test_missing_manifest_and_solution_are_reported(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing-task"
    assert cli.run_cli(["tasks", "lint", str(missing), "--json"], workspace=tmp_path) == 1
    assert {item["rule"] for item in json.loads(capsys.readouterr().out)} == {
        "task-toml-parses", "solution-present"
    }


def test_invalid_manifest_does_not_guess_verifier_mode(task: Path) -> None:
    (task / "task.toml").write_text("[verifier\n")
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("task-toml-parses", "error")


def test_directory_discovery_is_immediate_and_accepts_multiple_paths(task: Path, capsys) -> None:
    collection = task.parent / "collection"
    sibling = collection / "shared-task"
    (sibling / "solution").mkdir(parents=True)
    (sibling / "task.toml").write_text('[verifier]\nenvironment_mode = "shared"\n')
    (sibling / "solution/solve.sh").write_text("exit 0\n")
    nested = collection / "not-a-task" / "nested"
    nested.mkdir(parents=True)
    (nested / "task.toml").write_text("invalid TOML")
    empty = task.parent / "empty-collection"
    empty.mkdir()

    assert cli.run_cli(
        ["tasks", "lint", str(task), str(collection), str(empty), "--json"], workspace=task.parent
    ) == 0
    finding, = json.loads(capsys.readouterr().out)
    assert finding["rule"] == "verifier-isolation"
    assert finding["path"] == str(sibling / "task.toml")
