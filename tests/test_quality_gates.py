"""Exercise real checker and required-gate shell behavior with isolated tool fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.docs_consumer
BASH = shutil.which("bash")


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _environment(tmp_path: Path, checker: str | None) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("mktemp", "rm", "cat"):
        executable = shutil.which(name)
        assert executable is not None
        (bin_dir / name).symlink_to(executable)
    _executable(bin_dir / "uv", 'if [ "$1" = "--version" ]; then echo "uv 0.9.24"; fi\n')
    if checker is not None:
        _executable(bin_dir / "uvx", checker)
    return {
        **os.environ,
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "TY_BASELINE": "0",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _gate(tmp_path: Path, env: dict[str, str], surface: str) -> subprocess.CompletedProcess[str]:
    if surface == "local":
        command = [BASH, str(ROOT / "scripts/premerge.sh")]
    else:
        workflow = yaml.safe_load((ROOT / ".github/workflows/typecheck.yml").read_text())
        step = next(
            step for step in workflow["jobs"]["ty"]["steps"] if step.get("name") == "ty check"
        )
        command = [BASH, "-e", "-c", step["run"]]
    return subprocess.run(
        command, cwd=tmp_path, env=env, text=True, capture_output=True, check=False
    )


@pytest.mark.parametrize("surface", ["local", "ci"])
def test_checker_startup_failure_without_summary_cannot_pass(tmp_path: Path, surface: str) -> None:
    env = _environment(tmp_path, 'echo "checker could not start" >&2\nexit 2\n')
    result = _gate(tmp_path, env, surface)
    assert result.returncode == 2
    assert "checker could not start" in result.stdout


@pytest.mark.parametrize("surface", ["local", "ci"])
def test_clean_checker_passes_and_diagnostics_fail(tmp_path: Path, surface: str) -> None:
    env = _environment(tmp_path, "exit 0\n")
    assert _gate(tmp_path, env, surface).returncode == 0
    _executable(tmp_path / "bin/uvx", 'echo "type mismatch"\nexit 1\n')
    result = _gate(tmp_path, env, surface)
    assert result.returncode == 1
    assert "type mismatch" in result.stdout


@pytest.mark.parametrize("surface", ["local", "ci"])
def test_missing_checker_is_not_a_clean_result(tmp_path: Path, surface: str) -> None:
    assert _gate(tmp_path, _environment(tmp_path, None), surface).returncode == 127

@pytest.mark.parametrize(
    ("workflow_file", "job", "result_names"),
    [
        ("ci.yml", "quality-required", ("LINT_RESULT", "TEST_RESULT")),
        ("typecheck.yml", "typecheck-required", ("TY_RESULT",)),
    ],
)
def test_required_gate_rejects_every_non_success_prerequisite(
    tmp_path: Path, workflow_file: str, job: str, result_names: tuple[str, ...]
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_file).read_text())
    command = [BASH, "-e", "-c", workflow["jobs"][job]["steps"][0]["run"]]
    successful = {name: "success" for name in result_names}

    def run(results: dict[str, str]) -> int:
        return subprocess.run(
            command,
            cwd=tmp_path,
            env={"HOME": str(tmp_path), **results},
            capture_output=True,
            check=False,
        ).returncode

    assert run(successful) == 0
    for name in result_names:
        for result in ("failure", "cancelled", "skipped", "neutral", "", "unknown"):
            assert run({**successful, name: result}) != 0, (name, result)
        assert run({key: value for key, value in successful.items() if key != name}) != 0


def test_premerge_ty_failure_stops_before_running_pytest(tmp_path: Path) -> None:
    env = _environment(tmp_path, 'echo "type mismatch" >&2\nexit 1\n')
    bin_dir = tmp_path / "bin"
    sentinel = tmp_path / "pytest_invoked"
    uv_stub = f"""if [ "$1" = "--version" ]; then
    echo "uv 0.9.24"
    exit 0
fi
if [ "$1" = "run" ] && [ "$2" = "pytest" ]; then
    touch "{sentinel}"
    exit 0
fi
exit 0
"""
    _executable(bin_dir / "uv", uv_stub)
    result = _gate(tmp_path, env, surface="local")
    assert result.returncode == 1
    assert "type mismatch" in result.stdout or "type mismatch" in result.stderr
    assert not sentinel.exists(), "premerge must not invoke pytest when ty check fails"


