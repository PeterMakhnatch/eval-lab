from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.control_expect import declared_expectation
from evallab.task_lint import discover_tasks, lint_task

REPO_ROOT = Path(__file__).resolve().parents[1]

# A shared verifier is a known, allowed warning on these two packages. The
# hidden-inputs rule must keep every other library package error-free.
_ALLOWED_VERIFIER_ISOLATION_WARNINGS = {
    ("library/tasks/query-optimize/task.toml", "verifier-isolation"),
    ("library/tasks/tau3-retail-1/task.toml", "verifier-isolation"),
}


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


def test_copy_of_existing_environment_tests_directory_is_agent_visible(task: Path) -> None:
    (task / "environment/tests").mkdir()
    (task / "environment/tests/test_smoke.py").write_text("def test_ok(): pass\n")
    (task / "environment/Dockerfile").write_text(
        "FROM python:3.12\nCOPY app/ /app\nCOPY tests /app/tests\n"
    )
    assert lint_task(task) == []


@pytest.mark.parametrize(
    "instruction",
    [
        "COPY ../tests /private",
        "COPY /abs/tests /private",
        "COPY tests /private",
        "COPY ../solution /private",
        "COPY nested/tests /private",
    ],
)
def test_copy_sources_outside_the_build_context_still_flag(task: Path, instruction: str) -> None:
    (task / "tests").mkdir()
    (task / "environment/Dockerfile").write_text(f"FROM python:3.12\n{instruction}\n")
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("hidden-inputs-not-baked", "error")


def test_environment_symlink_to_hidden_tests_dir_still_flags(task: Path) -> None:
    (task / "tests").mkdir()
    (task / "environment/tests").symlink_to(task / "tests")
    (task / "environment/Dockerfile").write_text(
        "FROM python:3.12\nCOPY tests /app/tests\n"
    )
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


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("# expect: 1.0", 1.0),
        ("# expect: 0.0", 0.0),
        ("  # expect: 1", 1.0),
    ],
)
def test_control_header_is_accepted_by_lint_and_runtime(
    task: Path, header: str, expected: float
) -> None:
    (task / "controls").mkdir()
    control = task / "controls/control.sh"
    control.write_text(f"#!/bin/sh\n{header}\nexit 0\n")
    assert lint_task(task) == []
    assert declared_expectation(control) == expected


@pytest.mark.parametrize(
    "text",
    [
        "#!/bin/sh\n" + "\n" * 19 + "# expect: 1\nexit 0\n",
        "#!/bin/sh\n# expect 1\nexit 0\n",
        "#!/bin/sh\n# expect: 10\nexit 0\n",
    ],
)
def test_control_header_is_rejected_by_lint_and_runtime(task: Path, text: str) -> None:
    (task / "controls").mkdir()
    control = task / "controls/control.sh"
    control.write_text(text)
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("controls-declare-expectation", "error")
    assert declared_expectation(control) is None


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


def test_discovery_recurses_from_a_collection_and_accepts_multiple_paths(
    task: Path, capsys
) -> None:
    collection = task.parent / "collection"
    sibling = collection / "shared-task"
    (sibling / "solution").mkdir(parents=True)
    (sibling / "task.toml").write_text('[verifier]\nenvironment_mode = "shared"\n')
    (sibling / "solution/solve.sh").write_text("exit 0\n")
    nested = collection / "not-a-task" / "nested"
    (nested / "solution").mkdir(parents=True)
    (nested / "solution/solve.sh").write_text("exit 0\n")
    (nested / "task.toml").write_text("invalid TOML")
    empty = task.parent / "empty-collection"
    empty.mkdir()

    assert cli.run_cli(
        ["tasks", "lint", str(task), str(collection), str(empty), "--json"], workspace=task.parent
    ) == 1
    findings = json.loads(capsys.readouterr().out)
    assert {(finding["rule"], finding["severity"], finding["path"]) for finding in findings} == {
        ("verifier-isolation", "warning", str(sibling / "task.toml")),
        ("task-toml-parses", "error", str(nested / "task.toml")),
    }


def test_discovery_reports_a_nested_package_error_from_the_root(
    tmp_path: Path, capsys
) -> None:
    nested = tmp_path / "collection" / "group" / "deep-task"
    (nested / "solution").mkdir(parents=True)
    (nested / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (nested / "tests").mkdir()
    (nested / "environment").mkdir()
    (nested / "environment/Dockerfile").write_text("FROM python:3.12\nCOPY tests /private\n")
    (nested / "task.toml").write_text(
        'artifacts = ["/app/result.json"]\n[verifier]\nenvironment_mode = "separate"\n'
    )

    assert discover_tasks([tmp_path / "collection"]) == [nested]
    assert cli.run_cli(
        ["tasks", "lint", str(tmp_path / "collection"), "--json"], workspace=tmp_path
    ) == 1
    finding, = json.loads(capsys.readouterr().out)
    assert (finding["rule"], finding["severity"], finding["path"]) == (
        "hidden-inputs-not-baked",
        "error",
        str(nested / "environment/Dockerfile"),
    )


def test_discovery_does_not_descend_into_a_task_package(tmp_path: Path) -> None:
    package = tmp_path / "collection" / "task"
    (package / "tests").mkdir(parents=True)
    (package / "task.toml").write_text("")
    (package / "tests/task.toml").write_text("")

    assert discover_tasks([tmp_path / "collection"]) == [package]


def test_discovery_skips_hidden_directories_and_sorts_by_posix_path(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    for name in ("z-task", "a-task", ".hidden-task"):
        (collection / name).mkdir(parents=True)
        (collection / name / "task.toml").write_text("")
    nested = collection / "m-group" / "nested-task"
    nested.mkdir(parents=True)
    (nested / "task.toml").write_text("")

    assert discover_tasks([collection]) == [
        collection / "a-task",
        nested,
        collection / "z-task",
    ]


def test_committed_task_library_lints_without_error_findings() -> None:
    tasks = discover_tasks([REPO_ROOT / "library/tasks"])
    assert len(tasks) == 12

    findings = [finding for task in tasks for finding in lint_task(task)]
    assert [finding for finding in findings if finding.severity == "error"] == []
    warnings = {
        (Path(finding.path).relative_to(REPO_ROOT).as_posix(), finding.rule)
        for finding in findings
        if finding.severity == "warning"
    }
    assert warnings == _ALLOWED_VERIFIER_ISOLATION_WARNINGS
    assert cli.run_cli(
        ["tasks", "lint", str(REPO_ROOT / "library/tasks")], workspace=REPO_ROOT
    ) == 0


