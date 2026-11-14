from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab import cli
from evallab.task_artifact_lint import lint_artifact_overwrites
from evallab.task_lint import lint_task


def _task(tmp_path: Path, artifacts: list[str], mode: str = "separate") -> Path:
    task = tmp_path / "task"
    (task / "solution").mkdir(parents=True)
    (task / "solution/solve.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "task.toml").write_text(
        f"artifacts = {json.dumps(artifacts)}\n"
        f"[verifier]\nenvironment_mode = {json.dumps(mode)}\n"
    )
    (task / "instruction.md").write_text(f"Write these artifacts: {', '.join(artifacts)}\n")
    (task / "tests").mkdir()
    (task / "tests/verify.py").write_text(f"ARTIFACTS = {artifacts!r}\n")
    return task


@pytest.mark.parametrize(
    ("artifact", "root"),
    [
        ("/", "/tests"),
        ("/tests", "/tests"),
        ("/tests/test_outputs.py", "/tests"),
        ("/logs", "/logs/verifier"),
        ("/logs/verifier/reward.txt", "/logs/verifier"),
        ("/etc/profile", "/etc"),
        ("/usr/lib/x", "/usr"),
        ("/lib64/tool", "/lib64"),
        ("/bin", "/bin"),
        ("/sbin", "/sbin"),
        ("/lib", "/lib"),
        ("/opt", "/opt"),
        ("/var", "/var/lib"),
        ("/var/lib", "/var/lib"),
        ("/proc", "/proc"),
        ("/sys", "/sys"),
        ("/dev", "/dev"),
    ],
)
def test_artifact_overwrites_verifier(tmp_path: Path, artifact: str, root: str) -> None:
    task = _task(tmp_path, [artifact])

    finding, = [
        item for item in lint_task(task) if item.rule == "artifact-overwrites-verifier"
    ]

    assert finding.severity == "error"
    assert finding.path == str(task / "task.toml")
    assert f"declared artifact {artifact} covers {root}," in finding.message
    assert finding.message == (
        f"declared artifact {artifact} covers {root}, which the verifier grades from; "
        "Harbor uploads declared artifacts into the verifier container before grading, "
        "so the agent can replace the grader, its reward file, or the verifier interpreter "
        "and score 1; declare a path that holds only task output"
    )


@pytest.mark.parametrize(
    ("artifact", "normalized", "root"),
    [
        ("/tests/", "/tests", "/tests"),
        ("/app/../tests", "/tests", "/tests"),
        ("/logs/verifier/", "/logs/verifier", "/logs/verifier"),
        ("//tests//test_outputs.py", "/tests/test_outputs.py", "/tests"),
        ("///logs//verifier/./reward.txt", "/logs/verifier/reward.txt", "/logs/verifier"),
        ("//", "/", "/tests"),
    ],
)
def test_artifact_paths_are_normalized(
    tmp_path: Path, artifact: str, normalized: str, root: str
) -> None:
    task = _task(tmp_path, [artifact])

    finding, = [
        item for item in lint_task(task) if item.rule == "artifact-overwrites-verifier"
    ]

    assert f"declared artifact {normalized} covers {root}," in finding.message


@pytest.mark.parametrize(
    "artifact",
    [
        "/app/out",
        "/root/patch",
        "/srv/origin.git",
        "/workspace/release",
        "/home/user/out",
        "/lib-extra/tool",
        "/tests-extra/out",
        "/logs/verifier-extra/reward.txt",
        "/var/lib-extra/out",
        "/tests/../app/out",
    ],
)
def test_output_artifacts_stay_silent(tmp_path: Path, artifact: str) -> None:
    task = _task(tmp_path, [artifact])

    assert not [
        item for item in lint_task(task) if item.rule == "artifact-overwrites-verifier"
    ]


@pytest.mark.parametrize("artifact", [None, 0, False, "", "tests", "app/../tests"])
def test_nonabsolute_artifacts_are_ignored(tmp_path: Path, artifact: Any) -> None:
    assert lint_artifact_overwrites(tmp_path, [artifact]) == []


def test_findings_preserve_declaration_order_and_first_matching_root(tmp_path: Path) -> None:
    artifacts = ["/etc/profile", "/app/out", "/", "/logs", "/tests", "/tests"]
    task = _task(tmp_path, artifacts)

    findings = [
        item for item in lint_task(task) if item.rule == "artifact-overwrites-verifier"
    ]

    assert [finding.message.split(",", 1)[0] for finding in findings] == [
        "declared artifact /etc/profile covers /etc",
        "declared artifact / covers /tests",
        "declared artifact /logs covers /logs/verifier",
        "declared artifact /tests covers /tests",
        "declared artifact /tests covers /tests",
    ]


def test_shared_mode_skips_artifact_overwrite_rule(tmp_path: Path) -> None:
    task = _task(tmp_path, ["/tests", "/etc/profile"], mode="shared")

    assert not [
        item for item in lint_task(task) if item.rule == "artifact-overwrites-verifier"
    ]


def test_artifact_overwrite_error_gates_cli_exit(tmp_path: Path, capsys) -> None:
    task = _task(tmp_path, ["/tests"])

    assert cli.run_cli(["tasks", "lint", str(task), "--json"], workspace=task.parent) == 1
    finding, = [
        item for item in json.loads(capsys.readouterr().out)
        if item["rule"] == "artifact-overwrites-verifier"
    ]
    assert finding["severity"] == "error"
