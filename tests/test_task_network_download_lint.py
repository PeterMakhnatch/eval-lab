"""Contract for the verifier network download warning rule."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import cli
from evallab.task_lint import lint_task

# Digest-pinned base image: only the rule under test may produce a finding.
_PINNED_BASE = (
    "FROM python:3.13-slim@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251"
)


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


@pytest.mark.parametrize(
    "command",
    [
        "curl -o /tmp/x https://example.com/x",
        "wget -O /tmp/x https://example.com/x",
        "curl -fsSL https://example.com/x > /tmp/x",
        "git clone https://example.com/r /tmp/r",
        "curl -fsSL https://example.com/i | sh",
        "SUM=x sudo env MODE=test /usr/bin/curl -fsSL https://example.com/x",
        "if true; then /usr/bin/wget https://example.com/x; fi",
    ],
)
@pytest.mark.parametrize("mode", ["separate", "shared"])
def test_verifier_network_download_warns_in_shell_files(
    task: Path, command: str, mode: str, capsys
) -> None:
    (task / "task.toml").write_text(
        f'artifacts = ["/app/result.json"]\n[verifier]\nenvironment_mode = "{mode}"\n'
    )
    script = task / "tests/test.sh"
    script.write_text(f"#!/bin/sh\n{command}\n")

    finding, = [
        finding for finding in lint_task(task) if finding.rule == "verifier-network-download"
    ]
    assert (finding.rule, finding.severity, finding.path) == (
        "verifier-network-download", "warning", str(script)
    )
    assert finding.message == (
        "line 2: the verifier downloads from the network without a checksum; check the bytes "
        "with sha256sum -c in the same script so a moved or vanished remote cannot turn a "
        "correct answer into 0"
    )
    assert cli.run_cli(["tasks", "lint", str(task), "--json"], workspace=task.parent) == 0
    assert not any(item["severity"] == "error" for item in json.loads(capsys.readouterr().out))


@pytest.mark.parametrize(
    ("relative", "instruction"),
    [
        ("tests/Dockerfile", "RUN curl -fsSL -o /tests/x.zip https://example.com/x.zip"),
        (
            "tests/nested/Dockerfile.verify",
            "RUN git clone https://example.com/r /tmp/r && "
            "git -C /tmp/r checkout 0123456789abcdef0123456789abcdef01234567",
        ),
    ],
)
def test_verifier_network_download_warns_in_dockerfiles(
    task: Path, relative: str, instruction: str
) -> None:
    dockerfile = task / relative
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    dockerfile.write_text(_dockerfile(instruction))

    finding, = lint_task(task)
    assert (finding.rule, finding.severity, finding.path) == (
        "verifier-network-download", "warning", str(dockerfile)
    )
    assert finding.message == (
        "line 2: the verifier downloads from the network without a checksum; check the bytes "
        "with sha256sum -c in the same RUN instruction so a moved or vanished remote cannot "
        "turn a correct answer into 0"
    )


@pytest.mark.parametrize(
    "checksum",
    [
        "sha256sum -c /tmp/sums",
        "/usr/bin/sha256sum --check /tmp/sums",
        "shasum -a 256 -c /tmp/sums",
        "shasum -a256 --check /tmp/sums",
        "shasum --algorithm 256 -c /tmp/sums",
        "shasum --algorithm=256 --check /tmp/sums",
    ],
)
def test_verifier_shell_checksum_cleans_the_whole_script(task: Path, checksum: str) -> None:
    (task / "tests/test.sh").write_text(
        f"#!/bin/sh\ncurl -fsSL -o /tmp/x https://example.com/x\n{checksum}\n"
    )
    assert lint_task(task) == []


def test_verifier_dockerfile_checksum_cleans_the_same_run(task: Path) -> None:
    (task / "tests/Dockerfile").write_text(
        _dockerfile(
            "RUN curl -fsSL -o /tmp/x https://example.com/x \\",
            '    && echo "$SUM  /tmp/x" | sha256sum -c -',
        )
    )
    assert lint_task(task) == []


def test_verifier_dockerfile_checksum_does_not_clean_another_run(task: Path) -> None:
    dockerfile = task / "tests/Dockerfile"
    dockerfile.write_text(
        _dockerfile(
            "RUN curl -fsSL -o /tmp/x https://example.com/x",
            "RUN sha256sum -c /tmp/sums",
        )
    )
    finding, = lint_task(task)
    assert (finding.rule, finding.path) == ("verifier-network-download", str(dockerfile))
    assert finding.message.startswith("line 2:")


@pytest.mark.parametrize(
    "relative", ["environment/Dockerfile", "solution/solve.sh", "controls/positive.sh"]
)
def test_network_download_outside_tests_is_silent(task: Path, relative: str) -> None:
    source = task / relative
    source.parent.mkdir(exist_ok=True)
    command = "curl -fsSL -o /tmp/x https://example.com/x"
    source.write_text(
        _dockerfile(f"RUN {command}") if source.name == "Dockerfile"
        else f"#!/bin/sh\n# expect: 1\n{command}\n"
    )
    assert lint_task(task) == []


def test_verifier_network_download_reports_once_per_file(task: Path) -> None:
    script = task / "tests/test.sh"
    script.write_text(
        "#!/bin/sh\ncurl https://example.com/x\nwget https://example.com/y\n"
    )
    finding, = lint_task(task)
    assert (finding.rule, finding.path) == ("verifier-network-download", str(script))
    assert finding.message.startswith("line 2:")


@pytest.mark.parametrize("command", ["curl --version", "wget --version"])
def test_verifier_network_download_ignores_option_only_commands(task: Path, command: str) -> None:
    (task / "tests/test.sh").write_text(f"#!/bin/sh\n{command}\n")
    assert lint_task(task) == []


@pytest.mark.parametrize(
    "checksum", ["sha256sum /tmp/x", "shasum -c /tmp/sums", "shasum -a 512 -c /tmp/sums"]
)
def test_verifier_checksum_requires_check_and_sha256(task: Path, checksum: str) -> None:
    (task / "tests/test.sh").write_text(
        f"#!/bin/sh\ncurl https://example.com/x\n{checksum}\n"
    )
    finding, = lint_task(task)
    assert finding.rule == "verifier-network-download"
