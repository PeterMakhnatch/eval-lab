"""Contract for the verifier artifact-execution warning rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.task_lint import lint_task

_PINNED_BASE = (
    "FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "solution").mkdir(parents=True)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text(_PINNED_BASE + "\n")
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json", "/app/dispatch.py", "/app/bin"]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    (task / "instruction.md").write_text("Write the result to /app/result.json.\n")
    (task / "tests").mkdir()
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')
    return task


def _script(task: Path, line: str) -> Path:
    script = task / "tests/test.sh"
    script.write_text(f"#!/bin/sh\n{line}\n")
    return script


@pytest.mark.parametrize(
    ("line", "artifact"),
    [
        ("/app/dispatch.py --output /output/flight_plan.json", "/app/dispatch.py"),
        ("python /app/dispatch.py", "/app/dispatch.py"),
        ("python3 /app/dispatch.py", "/app/dispatch.py"),
        ("bash /app/bin/run.sh", "/app/bin/run.sh"),
        ("sh /app/bin/run.sh", "/app/bin/run.sh"),
        ("node /app/bin/tool.js", "/app/bin/tool.js"),
    ],
)
def test_verifier_artifact_execution_warns(task: Path, line: str, artifact: str) -> None:
    script = _script(task, line)

    finding, = [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]
    assert finding.severity == "warning"
    assert finding.path == str(script)
    assert "line 2:" in finding.message
    assert artifact in finding.message


@pytest.mark.parametrize(
    "line",
    [
        "chmod 755 /app/dispatch.py",
        "test -f /app/dispatch.py && echo ok",
        "[ -f /app/dispatch.py ] && chmod 755 /app/dispatch.py",
        "cat /app/result.json",
        "cp /app/result.json /tmp/result.json",
        "setpriv --reuid=nobody --regid=nogroup --clear-groups python /app/dispatch.py",
        "runuser -u nobody -- python /app/dispatch.py",
        'su nobody -s /bin/sh -c "python /app/dispatch.py"',
        "gosu nobody python /app/dispatch.py",
    ],
)
def test_non_execution_and_privilege_drops_are_silent(task: Path, line: str) -> None:
    _script(task, line)

    assert not [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]


def test_non_separate_verifier_is_silent(task: Path) -> None:
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json", "/app/dispatch.py", "/app/bin"]\n'
    )
    _script(task, "python /app/dispatch.py")

    assert not [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]


def test_execution_warning_keeps_physical_line_number(task: Path) -> None:
    script = _script(task, "# first\n# second\n# third\npython /app/dispatch.py")

    finding, = [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]
    assert finding.path == str(script)
    assert "line 5:" in finding.message


def test_inline_interpreter_command_warns(task: Path) -> None:
    script = _script(task, 'bash -c "python /app/dispatch.py"')

    finding, = [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]
    assert finding.path == str(script)
    assert "line 2:" in finding.message
    assert "/app/dispatch.py" in finding.message


@pytest.mark.parametrize(
    "line",
    [
        "sudo python /app/dispatch.py",
        "env OUTPUT=/tmp/result python /app/dispatch.py",
        "command exec nohup python /app/dispatch.py",
        "/usr/bin/python3.12 /app/dispatch.py",
        "python2 /app/dispatch.py",
        "dash /app/bin/run.sh",
        'bash --command "python /app/dispatch.py"',
    ],
)
def test_wrappers_and_interpreter_variants_warn(task: Path, line: str) -> None:
    _script(task, line)

    finding, = [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]
    assert "line 2:" in finding.message


@pytest.mark.parametrize(
    "line",
    [
        "python /app/dispatch.py.backup",
        "bash /app/binary/run.sh",
        "python --output=/app/dispatch.py",
        "pythonista /app/dispatch.py",
        "python3x /app/dispatch.py",
        "bash -c \"bash -c 'python /app/dispatch.py'\"",
    ],
)
def test_only_exact_artifacts_and_one_inline_level_warn(task: Path, line: str) -> None:
    _script(task, line)

    assert not [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]


def test_recursive_shell_scan_is_sorted_and_reports_once_per_command(task: Path) -> None:
    nested = task / "tests/nested"
    nested.mkdir()
    (nested / "ignored.sh").mkdir()
    first = nested / "a.sh"
    first.write_text("#!/bin/sh\npython /app/dispatch.py /app/result.json\n")
    last = _script(task, "python /app/dispatch.py; bash /app/bin/run.sh")
    (nested / "ignored.txt").write_text("python /app/dispatch.py\n")

    findings = [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]
    assert [finding.path for finding in findings] == [str(first), str(last), str(last)]
    assert "/app/dispatch.py" in findings[0].message
    assert "/app/bin/run.sh" in findings[2].message


def test_artifact_declarations_are_absolute_and_trim_trailing_slashes(task: Path) -> None:
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json", "/app/bin/", "relative.py"]\n'
        '[verifier]\nenvironment_mode = "separate"\n'
    )
    _script(task, "python relative.py; bash /app/bin/run.sh")

    finding, = [
        finding for finding in lint_task(task)
        if finding.rule == "verifier-executes-artifact"
    ]
    assert "/app/bin/run.sh" in finding.message


def test_imported_verifier_execution_regressions() -> None:
    tasks = REPO_ROOT / "library/external/terminal-bench-v3.0.0/tasks"
    cargo = tasks / "cargo-flight-dispatch"

    finding, = [
        finding for finding in lint_task(cargo)
        if finding.rule == "verifier-executes-artifact"
    ]
    assert finding.path == str(cargo / "tests/test.sh")
    assert "line 13:" in finding.message
    assert "/app/dispatch.py" in finding.message

    for name in ("ico-path-patch", "embedding-drift-monitor"):
        assert not [
            finding for finding in lint_task(tasks / name)
            if finding.rule == "verifier-executes-artifact"
        ]
