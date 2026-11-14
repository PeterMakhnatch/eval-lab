"""Contract for the dropped-uid reward-write warning rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from evallab.task_lint import lint_task

_RULE = "dropped-uid-reward-write"
_PINNED_BASE = (
    "FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)


@pytest.fixture
def task(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    (task / "solution").mkdir(parents=True)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "environment").mkdir()
    (task / "environment/Dockerfile").write_text(_PINNED_BASE + "\n")
    (task / "task.toml").write_text(
        'artifacts = ["/app/result.json"]\n[verifier]\nenvironment_mode = "separate"\n'
    )
    (task / "instruction.md").write_text("Write the result to /app/result.json.\n")
    (task / "tests").mkdir()
    (task / "tests/verify.py").write_text('RESULT = "/app/result.json"\n')
    return task


def _findings(task: Path) -> list:
    return [finding for finding in lint_task(task) if finding.rule == _RULE]


def _drop_python_privileges(task: Path) -> None:
    (task / "tests/conftest.py").write_text(
        "import os\n\n\ndef _drop():\n    os.setgroups([])\n    os.setgid(65534)\n"
        "    os.setuid(65534)\n"
    )


def _script(task: Path, *lines: str) -> Path:
    script = task / "tests/test.sh"
    script.write_text("#!/bin/bash\n" + "\n".join(lines) + "\n")
    return script


def test_dropped_uid_reward_write_warns(task: Path) -> None:
    _drop_python_privileges(task)
    script = _script(
        task,
        "chmod 700 /logs/verifier",
        "echo 0 > /logs/verifier/reward.txt",
        "pytest -q /tests/test_outputs.py",
        'echo "$?" > /logs/verifier/reward.txt',
    )

    finding, = _findings(task)
    assert finding.severity == "warning"
    assert finding.path == str(script)
    assert "line 5:" in finding.message
    assert "tests/conftest.py line 7" in finding.message
    assert "kill the dropped uid before the final reward write" in finding.message


@pytest.mark.parametrize(
    "kill", ["pkill -u nobody", "killall -u nobody", "kill -9 -1"]
)
def test_kill_before_the_last_reward_write_is_silent(task: Path, kill: str) -> None:
    _drop_python_privileges(task)
    _script(
        task,
        "chmod 700 /logs/verifier",
        "echo 0 > /logs/verifier/reward.txt",
        "pytest -q /tests/test_outputs.py",
        kill,
        "echo 1 > /logs/verifier/reward.txt",
    )

    assert _findings(task) == []


def test_kill_after_the_last_reward_write_still_warns(task: Path) -> None:
    _drop_python_privileges(task)
    _script(
        task,
        "chmod 700 /logs/verifier",
        "echo 1 > /logs/verifier/reward.txt",
        "pkill -u nobody",
    )

    finding, = _findings(task)
    assert "line 3:" in finding.message


def test_no_privilege_drop_is_silent(task: Path) -> None:
    _script(
        task,
        "chmod 700 /logs/verifier",
        "echo 1 > /logs/verifier/reward.txt",
    )

    assert _findings(task) == []


def test_no_reward_dir_lock_is_silent(task: Path) -> None:
    _drop_python_privileges(task)
    _script(task, "echo 1 > /logs/verifier/reward.txt")

    assert _findings(task) == []


@pytest.mark.parametrize(
    "drop",
    [
        "setpriv --reuid=65534 --regid=65534 --clear-groups true",
        "runuser -u nobody -- true",
        "su nobody -s /bin/sh -c true",
        "gosu nobody true",
    ],
)
def test_shell_privilege_drop_warns(task: Path, drop: str) -> None:
    script = _script(
        task,
        "chmod 700 /logs/verifier",
        drop,
        "echo 1 > /logs/verifier/reward.txt",
    )

    finding, = _findings(task)
    assert finding.path == str(script)
    assert "tests/test.sh line 3" in finding.message
