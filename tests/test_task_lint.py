from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import evallab.task_lint as task_lint
from evallab import cli
from evallab.control_expect import declared_expectation
from evallab.task_lint import discover_tasks, lint_task

REPO_ROOT = Path(__file__).resolve().parents[1]

# Digest-pinned base image: tests that exercise other Dockerfile findings must
# not also raise the base-image rule.
_PINNED_BASE = (
    "FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)

# Known warnings in the committed library, by rule and package. Every other
# finding is unexpected, so a new warning must be added here deliberately.
_ALLOWED_LIBRARY_WARNINGS = {
    "unpinned-image": {
        "library/tasks/event-summary",
        "library/tasks/query-optimize",
        "library/tasks/release-branch-rescue",
        "library/tasks/sheet-recalc",
        "library/tasks/sheet-recalc-cutoff",
        "library/tasks/tau3-retail-1",
        "library/tasks/terminal-bench-html-js-filter",
        "library/tasks/transaction-reconciliation",
    },
    "unpinned-clone": {"library/tasks/tau3-retail-1"},
    "verifier-isolation": {
        "library/tasks/query-optimize",
        "library/tasks/tau3-retail-1",
    },
    "verifier-runtime-install": {"library/tasks/query-optimize"},
}


def _dockerfile(*lines: str) -> str:
    return "\n".join((_PINNED_BASE, *lines)) + "\n"


@pytest.fixture
def task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "solution").mkdir(parents=True)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text(_dockerfile("COPY app/ /app"))
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json"]\n[verifier]\nenvironment_mode = "separate"\n'
    )
    (task / "instruction.md").write_text("Write the result to /app/result.json.\n")
    (task / "tests").mkdir()
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')
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


def test_separate_instruction_must_name_a_declared_artifact(task: Path) -> None:
    (task / "instruction.md").write_text("Write /app/elsewhere.json.\n")
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')

    (finding,) = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "artifacts-instructed",
        "warning",
        str(task / "instruction.md"),
    )


def test_separate_tests_must_name_a_declared_artifact(task: Path) -> None:
    (task / "instruction.md").write_text("Write /app/result.json.\n")
    (task / "tests/verify.py").write_text('print("checked")\n')

    (finding,) = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "artifacts-verified",
        "warning",
        str(task / "tests"),
    )


def test_separate_verifier_cannot_read_an_undeclared_path(task: Path) -> None:
    (task / "instruction.md").write_text("Write /app/result.json.\n")
    (task / "tests/verify.py").write_text(
        'RESULT = "/app/result.json"\nOTHER = "/app/other.json"\n'
    )

    (finding,) = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "verifier-reads-undeclared",
        "warning",
        str(task / "tests/verify.py"),
    )
    assert "/app/other.json" in finding.message


def test_artifact_contract_accepts_declared_mentions(task: Path) -> None:
    (task / "instruction.md").write_text("Write /app/result.json.\n")
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')

    assert lint_task(task) == []
    assert cli.run_cli(["tasks", "lint", str(task)], workspace=task.parent) == 0


def test_separate_instruction_requires_each_artifact_path(task: Path) -> None:
    (task / "instruction.md").write_text("Write the result in /app.\n")
    (task / "tests/verify.py").write_text('ROOT = "/app"\n')

    (finding,) = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "artifacts-instructed",
        "warning",
        str(task / "instruction.md"),
    )
    assert "/app/result.json" in finding.message


def test_separate_instruction_requires_every_declared_artifact(task: Path) -> None:
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json", "/app/log.txt"]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    (task / "instruction.md").write_text("Write /app/result.json.\n")
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\nLOG = "/app/log.txt"\n')

    (finding,) = lint_task(task)
    assert finding.rule == "artifacts-instructed"
    assert "/app/log.txt" in finding.message
    assert "/app/result.json" not in finding.message


def test_separate_instruction_does_not_accept_a_path_below_the_artifact(task: Path) -> None:
    (task / "task.toml").write_text(
        'artifacts = ["/app/output"]\n[verifier]\nenvironment_mode = "separate"\n'
    )
    (task / "instruction.md").write_text("Write /app/output/answer.json.\n")
    (task / "tests/verify.py").write_text('OUTPUT = "/app/output"\n')

    (finding,) = lint_task(task)
    assert finding.rule == "artifacts-instructed"
    assert "/app/output" in finding.message


def test_separate_task_without_instruction_warns(task: Path) -> None:
    (task / "instruction.md").unlink()

    (finding,) = lint_task(task)
    assert (finding.rule, finding.path) == ("artifacts-instructed", str(task / "instruction.md"))
    assert "missing" in finding.message


def test_separate_tests_must_name_every_declared_artifact(task: Path) -> None:
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json", "/app/log.txt"]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    (task / "instruction.md").write_text("Write /app/result.json and /app/log.txt.\n")
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')

    (finding,) = lint_task(task)
    assert (finding.rule, finding.path) == ("artifacts-verified", str(task / "tests"))
    assert "/app/log.txt" in finding.message


def test_separate_task_without_tests_directory_warns(task: Path) -> None:
    for path in (task / "tests").rglob("*"):
        path.unlink()
    (task / "tests").rmdir()

    (finding,) = lint_task(task)
    assert (finding.rule, finding.path) == ("artifacts-verified", str(task / "tests"))
    assert "missing" in finding.message


def test_shared_mode_skips_the_artifact_contract(task: Path) -> None:
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json"]\n[verifier]\nenvironment_mode = "shared"\n'
    )
    (task / "instruction.md").write_text("Write the result.\n")
    (task / "tests/verify.py").write_text('OTHER = "/app/other.json"\n')

    assert [finding.rule for finding in lint_task(task)] == ["verifier-isolation"]


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
    (task / "environment/Dockerfile").write_text(_dockerfile(instruction))
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("hidden-inputs-not-baked", "error")


def test_copy_of_existing_environment_tests_directory_is_agent_visible(task: Path) -> None:
    (task / "environment/tests").mkdir()
    (task / "environment/tests/test_smoke.py").write_text("def test_ok(): pass\n")
    (task / "environment/Dockerfile").write_text(
        _dockerfile("COPY app/ /app", "COPY tests /app/tests")
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
    (task / "tests").mkdir(exist_ok=True)
    (task / "environment/Dockerfile").write_text(_dockerfile(instruction))
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("hidden-inputs-not-baked", "error")


def test_environment_symlink_to_hidden_tests_dir_still_flags(task: Path) -> None:
    (task / "tests").mkdir(exist_ok=True)
    (task / "environment/tests").symlink_to(task / "tests")
    (task / "environment/Dockerfile").write_text(_dockerfile("COPY tests /app/tests"))
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
    (task / "environment/Dockerfile").write_text(_dockerfile("# COPY tests/ /tests", instruction))
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
    dockerfile.write_text(_dockerfile(instruction))
    assert cli.run_cli(["tasks", "lint", str(task), "--json"], workspace=task.parent) == 1
    finding, = json.loads(capsys.readouterr().out)
    assert (finding["rule"], finding["severity"]) == ("dockerfile-copy-parses", "error")
    assert finding["path"] == str(dockerfile)
    assert "line 2:" in finding["message"]


def test_unsupported_escape_directive_is_not_silently_accepted(task: Path) -> None:
    (task / "environment/Dockerfile").write_text("# escape=`\n" + _dockerfile())
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("dockerfile-copy-parses", "error")


def test_non_utf8_dockerfile_reports_error(task: Path) -> None:
    (task / "environment/Dockerfile").write_bytes(b"FROM python:3.12\nCOPY \xff /private\n")
    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("dockerfile-copy-parses", "error")


def test_base_image_without_a_digest_warns_without_failing(task: Path, capsys) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\n")

    (finding,) = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-image",
        "warning",
        str(dockerfile),
    )
    assert "python:3.12-slim" in finding.message
    assert "sha256" in finding.message
    assert cli.run_cli(["tasks", "lint", str(task)], workspace=task.parent) == 0
    assert capsys.readouterr().out == (
        f"warning unpinned-image {dockerfile}: {finding.message}\n"
    )


def test_digest_pinned_base_image_does_not_warn(task: Path) -> None:
    (task / "environment/Dockerfile").write_text(_dockerfile("WORKDIR /app"))

    assert lint_task(task) == []


def test_scratch_and_earlier_stage_names_do_not_warn(task: Path) -> None:
    (task / "environment/Dockerfile").write_text(
        "\n".join(
            (
                _PINNED_BASE + " AS build",
                "FROM build AS runtime",
                "FROM scratch",
                "COPY --from=build /app /app",
            )
        )
        + "\n"
    )

    assert lint_task(task) == []


def test_build_argument_base_image_warns_that_it_cannot_be_checked(task: Path) -> None:
    (task / "environment/Dockerfile").write_text(
        "ARG BASE_IMAGE=python:3.12-slim\nFROM ${BASE_IMAGE}\n"
    )

    (finding,) = lint_task(task)
    assert (finding.rule, finding.severity) == ("unpinned-image", "warning")
    assert "build argument" in finding.message
    assert "cannot be checked" in finding.message


def test_git_clone_without_a_commit_warns(task: Path) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(
        _dockerfile(
            "RUN apt-get update && \\",
            "    git clone --depth=1 https://example.test/repo.git /opt/repo",
        )
    )

    (finding,) = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-clone",
        "warning",
        str(dockerfile),
    )
    assert "line 2:" in finding.message
    assert "40-hex commit" in finding.message


def test_git_clone_at_a_full_commit_does_not_warn(task: Path) -> None:
    (task / "environment/Dockerfile").write_text(
        _dockerfile(
            "RUN git clone --depth=1 https://example.test/repo.git /opt/repo \\",
            "    && git -C /opt/repo checkout 0123456789abcdef0123456789abcdef01234567",
        )
    )

    assert lint_task(task) == []


def test_dockerfiles_under_tests_and_nested_environment_are_scanned(task: Path) -> None:
    (task / "tests/Dockerfile").write_text("FROM ubuntu:24.04\n")
    runtime = task / "environment/runtime-server/Dockerfile"
    runtime.parent.mkdir()
    runtime.write_text(
        "FROM ubuntu:24.04\nRUN git clone https://example.test/repo.git /opt/repo\n"
    )

    findings = lint_task(task)
    assert {(finding.rule, finding.path) for finding in findings} == {
        ("unpinned-image", str(task / "tests/Dockerfile")),
        ("unpinned-image", str(runtime)),
        ("unpinned-clone", str(runtime)),
    }


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
    (nested / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')
    (nested / "instruction.md").write_text("Write /app/result.json.\n")
    (nested / "environment").mkdir()
    (nested / "environment/Dockerfile").write_text(_dockerfile("COPY tests /private"))
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

    findings = [(task, finding) for task in tasks for finding in lint_task(task)]
    assert [finding for _, finding in findings if finding.severity == "error"] == []
    assert [
        finding for _, finding in findings if finding.rule == "unpinned-python-dependency"
    ] == []
    warnings: dict[str, set[str]] = {}
    for task, finding in findings:
        if finding.severity == "warning":
            warnings.setdefault(finding.rule, set()).add(
                task.relative_to(REPO_ROOT).as_posix()
            )
    assert warnings == _ALLOWED_LIBRARY_WARNINGS
    # One finding per Dockerfile that breaks a rule, so a second unpinned
    # Dockerfile inside an already-listed package cannot hide.
    assert sum(finding.severity == "warning" for _, finding in findings) == 20
    assert cli.run_cli(
        ["tasks", "lint", str(REPO_ROOT / "library/tasks")], workspace=REPO_ROOT
    ) == 0


def test_unclosed_quote_in_solution_is_reported(task: Path) -> None:
    script = task / "solution/solve.sh"
    script.write_text('#!/bin/sh\necho "unclosed\n')

    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("sources-parse", "error")
    assert finding.path == str(script)
    assert "unexpected EOF" in finding.message


def test_unterminated_heredoc_in_control_script_is_reported(task: Path) -> None:
    (task / "controls").mkdir()
    control = task / "controls/positive.sh"
    control.write_text("#!/bin/sh\n# expect: 1\ncheck() {\n  cat <<EOF\n  body\n}\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("sources-parse", "error")
    assert finding.path == str(control)
    assert "end of file" in finding.message


def test_shell_parser_warning_without_a_failing_exit_is_reported(
    task: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bash 4 and newer warn about a heredoc that reaches end of file and exit 0.

    macOS ships bash 3.2, which accepts that shape without any diagnostic, so
    this test supplies the parser message of the bash that Harbor images run.
    """
    (task / "controls").mkdir()
    control = task / "controls/positive.sh"
    control.write_text("#!/bin/sh\n# expect: 1\ncat <<EOF\nunterminated\n")
    stub = tmp_path / "bin" / "bash"
    stub.parent.mkdir()
    stub.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        '  */positive.sh) printf "%s\\n" '
        '"warning: here-document at line 3 delimited by end-of-file" >&2 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("sources-parse", "error")
    assert finding.path == str(control)
    assert "here-document" in finding.message


def test_shell_files_are_skipped_when_bash_is_unavailable(
    task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (task / "solution/solve.sh").write_text('#!/bin/sh\necho "unclosed\n')
    monkeypatch.setattr(task_lint, "_bash_parser", lambda: None)

    assert lint_task(task) == []


def test_python_syntax_error_under_tests_is_reported(task: Path) -> None:
    (task / "tests").mkdir(exist_ok=True)
    module = task / "tests/test_broken.py"
    module.write_text("def broken(:\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("sources-parse", "error")
    assert finding.path == str(module)
    assert "invalid syntax" in finding.message


def test_invalid_json_under_tests_is_reported(task: Path) -> None:
    (task / "tests").mkdir(exist_ok=True)
    cases = task / "tests/cases.json"
    cases.write_text('{"a": 1,}\n')

    finding, = lint_task(task)
    assert (finding.rule, finding.severity) == ("sources-parse", "error")
    assert finding.path == str(cases)
    # Decoder wording differs between Python 3.12 and 3.14; the rule contract is
    # the parser label plus the reported position.
    assert finding.message.startswith("json.loads: ")
    assert "line 1 column" in finding.message


def test_well_formed_sources_report_nothing_and_write_nothing(task: Path) -> None:
    (task / "controls").mkdir()
    (task / "controls/positive.sh").write_text("#!/bin/sh\n# expect: 1\nexit 0\n")
    (task / "tests").mkdir(exist_ok=True)
    (task / "tests/conftest.py").write_text("import json\n")
    (task / "tests/cases.json").write_text('{"a": 1}\n')
    (task / "solution/helper.py").write_text("def collect():\n    return 1\n")
    before = {path: path.read_bytes() for path in task.rglob("*") if path.is_file()}

    assert lint_task(task) == []
    assert {path: path.read_bytes() for path in task.rglob("*") if path.is_file()} == before
    assert list(task.rglob("__pycache__")) == []


def test_unpinned_dockerfile_dependency_warns(task: Path) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile("RUN pip install requests"))

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2: pip install" in finding.message
    assert "dependencies: requests; pin each with ==" in finding.message


@pytest.mark.parametrize(
    "command",
    [
        "pip3 install requests",
        "python -m pip install requests",
        "uv pip install requests",
        "uv tool install requests",
        "uvx --with requests pytest",
    ],
)
def test_python_install_forms_warn(task: Path, command: str) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile(f"RUN {command}"))

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2:" in finding.message
    assert "dependencies: requests; pin each with ==" in finding.message


@pytest.mark.parametrize(
    "command",
    [
        "pip install requests==2.32.3",
        "pip install requests===2.32.3",
        "pip install -r requirements.txt",
        "pip install ./dist/pkg-1.0-py3-none-any.whl",
        'pip install "${PKG_ROOT}[knowledge]"',
        f'pip install "pkg @ https://example.com/repo/archive/{"a" * 40}.zip"',
        f'pip install "pkg @ https://example.com/pkg.tar.gz#sha256={"b" * 64}"',
        "pip install dist/pkg-1.0.whl vendor/pkg /opt/pkg ~/pkg file:///opt/pkg",
        f"pip install git+https://example.com/repo.git@{'c' * 40}",
        "uvx -p 3.13 -w pytest==8.4.1 pytest",
        "uvx --with=pytest==8.4.1 pytest",
        "uvx pytest",
        'uv tool install --from "pkg==1.0" mycmd',
    ],
)
def test_pinned_and_local_python_dependencies_are_allowed(task: Path, command: str) -> None:
    (task / "solution/solve.sh").write_text(f"#!/bin/sh\n{command}\n")

    assert lint_task(task) == []


def test_uv_tool_from_pins_the_package_not_the_command(task: Path) -> None:
    (task / "solution/solve.sh").write_text(
        '#!/bin/sh\nuv tool install --from "pkg>=1.0" mycmd\n'
    )

    finding, = lint_task(task)
    assert "dependencies: pkg>=1.0;" in finding.message
    assert "mycmd" not in finding.message


def test_unpinned_solution_dependency_warns(task: Path) -> None:
    script = task / "solution/solve.sh"
    script.write_text("#!/bin/sh\npip install fastmcp>=3.0\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert "line 2:" in finding.message
    assert "fastmcp>=3.0; pin each with ==" in finding.message


def test_verifier_runtime_installs_warn_once_per_file(task: Path) -> None:
    script = task / "tests/test.sh"
    script.write_text(
        "#!/bin/sh\n# Install the verifier tools\n\n"
        "apt-get update\ncurl -LsSf https://example.com/i.sh | sh\n"
        "uvx --with pytest==8.4.1 pytest\n"
    )

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "verifier-runtime-install", "warning", str(script)
    )
    assert "line 4:" in finding.message
    assert "bake them into tests/Dockerfile" in finding.message
    assert "mirror failure cannot turn a correct answer into 0" in finding.message


@pytest.mark.parametrize(
    "command",
    [
        "apt-get install -y curl",
        "pip install pytest==8.4.1",
        "uv pip install pytest==8.4.1",
        "uvx pytest",
        "curl -LsSf https://example.com/i.sh | sh",
        "wget -qO- https://example.com/i.sh | bash",
        "pip3 install pytest==8.4.1",
        "python -m pip install pytest==8.4.1",
        "uv tool install pytest==8.4.1",
        "curl -LsSf https://example.com/i.sh | sudo /bin/dash",
    ],
)
def test_verifier_runtime_install_forms_warn(task: Path, command: str) -> None:
    script = task / "tests/test.sh"
    script.write_text(f"#!/bin/sh\n{command}\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "verifier-runtime-install", "warning", str(script)
    )
    assert "line 2:" in finding.message
    assert "bake them into tests/Dockerfile" in finding.message


def test_build_and_solution_installs_are_not_verifier_runtime_installs(task: Path) -> None:
    (task / "tests/Dockerfile").write_text(_dockerfile("RUN pip install pytest==8.4.1"))
    (task / "solution/solve.sh").write_text("#!/bin/sh\npip install pytest==8.4.1\n")

    assert lint_task(task) == []


def test_verifier_pytest_execution_is_not_an_install(task: Path) -> None:
    (task / "tests/test.sh").write_text("#!/bin/sh\npython -m pytest\n")

    assert lint_task(task) == []


@pytest.mark.parametrize(
    "option",
    [
        "-r requirements.txt", "--requirement=requirements.txt",
        "-c constraints.txt", "-e editable-package", "-i https://example.com/simple",
        "-p 3.12", "--python python3", "--index-url https://example.com/simple",
        "--extra-index-url=https://example.com/simple", "-f wheels", "-t target",
    ],
)
def test_python_install_option_values_are_not_requirements(task: Path, option: str) -> None:
    script = task / "solution/solve.sh"
    script.write_text(f"#!/bin/sh\npip install {option} requests\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert "dependencies: requests; pin each with ==" in finding.message


@pytest.mark.parametrize(
    "instruction",
    [
        "RUN TOKEN=value sudo env OTHER=value command exec /usr/bin/pip3 install requests",
        'RUN ["pip", "install", "requests"]',
        "RUN true && pip install requests || true ; echo ok | cat |& cat & true ;; true",
        "RUN pip install \\\n# Ignore this comment\n\n requests",
    ],
)
def test_install_command_parsing_retains_the_first_line(task: Path, instruction: str) -> None:
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile(instruction))

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2:" in finding.message
    assert "dependencies: requests;" in finding.message


def test_multiple_installs_on_one_logical_line_warn_once(task: Path) -> None:
    dockerfile = task / "tests/Dockerfile"
    dockerfile.write_text(
        _dockerfile("RUN pip install requests && uv tool install fastmcp>=3.0")
    )

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(dockerfile)
    )
    assert "line 2:" in finding.message
    assert "dependencies: requests, fastmcp>=3.0;" in finding.message


@pytest.mark.parametrize(
    "spec",
    [
        "https://example.com/pkg.tar.gz",
        "pkg @ https://example.com/pkg.tar.gz",
        "git+https://example.com/repo.git@main",
        f"https://example.com/{'a' * 41}.zip",
        f"https://example.com/pkg.tar.gz#sha256={'a' * 63}",
    ],
)
def test_unpinned_urls_warn(task: Path, spec: str) -> None:
    script = task / "solution/solve.sh"
    script.write_text(f'#!/bin/sh\npip install "{spec}"\n')

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert f"dependencies: {spec}; pin each with ==" in finding.message


def test_nested_helper_dockerfiles_and_hidden_scripts_are_not_scanned(task: Path) -> None:
    helper = task / "environment/runtime-server"
    helper.mkdir()
    (helper / "Dockerfile").write_text(_dockerfile("RUN pip install requests"))
    hidden = task / ".hidden"
    hidden.mkdir()
    (hidden / "install.sh").write_text("pip install requests\n")
    (task / "solution/.install.sh").write_text("pip install requests\n")

    assert lint_task(task) == []


def test_other_package_shell_files_are_scanned(task: Path) -> None:
    script = task / "environment/setup.sh"
    script.write_text("#!/bin/sh\nuvx -w requests pytest\n")

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "unpinned-python-dependency", "warning", str(script)
    )
    assert "line 2: uvx --with" in finding.message
    assert "dependencies: requests;" in finding.message


def test_install_scan_joins_continuations_and_tolerates_bad_quotes(task: Path) -> None:
    script = task / "solution/solve.sh"
    script.write_text("#!/bin/sh\npip install \\\n# pin me\n  requests\n")
    dockerfile = task / "environment/Dockerfile"
    dockerfile.write_text(_dockerfile('RUN pip install "requests'))

    findings = lint_task(task)
    assert [(finding.severity, finding.rule, finding.path) for finding in findings] == [
        ("warning", "unpinned-python-dependency", str(dockerfile)),
        ("warning", "unpinned-python-dependency", str(script)),
    ]
    assert 'dependencies: "requests;' in findings[0].message
    assert "line 2: pip install" in findings[1].message
    assert "dependencies: requests;" in findings[1].message
