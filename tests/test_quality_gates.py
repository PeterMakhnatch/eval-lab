"""Exercise gate failures without installing dependencies or invoking host tools."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.docs_consumer
BASH = shutil.which("bash")
GIT = shutil.which("git")


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _environment(tmp_path: Path, checker: str | None) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("mkdir", "dirname", "cat"):
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


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [GIT, "-c", "core.hooksPath=/dev/null", *args],
        cwd=root,
        text=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    ).strip()


def _repository(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "gate@example.invalid")
    _git(root, "config", "user.name", "Gate regression")
    _git(root, "commit", "--allow-empty", "-qm", "baseline")


def test_merge_and_rewrite_hooks_preserve_review_head_and_uncommitted_work(tmp_path: Path) -> None:
    _repository(tmp_path)
    (tmp_path / "draft").write_text("staged\n")
    _git(tmp_path, "add", "draft")
    (tmp_path / "draft").write_text("working\n")
    before = (_git(tmp_path, "rev-parse", "HEAD"), _git(tmp_path, "diff", "--cached"))
    for hook in ("post-merge", "post-rewrite"):
        subprocess.run(
            [BASH, str(ROOT / ".githooks" / hook)], cwd=tmp_path, input="", text=True, check=True
        )
    assert (_git(tmp_path, "rev-parse", "HEAD"), _git(tmp_path, "diff", "--cached")) == before
    assert (tmp_path / "draft").read_text() == "working\n"


def test_merge_driver_failure_preserves_ours(tmp_path: Path) -> None:
    _repository(tmp_path)
    _executable(tmp_path / "bin/uv", 'echo "generator failed" >&2\nexit 1\n')
    ours = tmp_path / "ours"
    ours.write_text("recoverable original\n")
    result = subprocess.run(
        [
            BASH,
            str(ROOT / "scripts/git-merge-regen.sh"),
            "ancestor",
            str(ours),
            "theirs",
            "docs/INDEX.md",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.defpath}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "generator failed" in result.stderr
    assert ours.read_text() == "recoverable original\n"


def test_setup_preserves_custom_hook_configuration(tmp_path: Path) -> None:
    _repository(tmp_path)
    _git(tmp_path, "config", "core.hooksPath", "custom-hooks")
    subprocess.run([BASH, str(ROOT / "scripts/setup-git.sh")], cwd=tmp_path, check=True)
    # Do not use _git: its invocation-level no-hooks override is intentionally stronger.
    assert (
        subprocess.check_output(
            [GIT, "config", "--local", "core.hooksPath"], cwd=tmp_path, text=True
        ).strip()
        == "custom-hooks"
    )


def test_setup_does_not_enable_hooks_in_another_worktree(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _repository(checkout)
    sibling = tmp_path / "sibling"
    _git(checkout, "worktree", "add", "--detach", str(sibling))
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run([BASH, str(ROOT / "scripts/setup-git.sh")], cwd=checkout, env=env, check=True)
    result = subprocess.run(
        [GIT, "config", "--local", "--get", "core.hooksPath"],
        cwd=sibling,
        env=env,
        capture_output=True,
    )
    assert result.returncode == 1, "setup must not activate another worktree's hooks"


@pytest.mark.parametrize(
    "relative",
    [
        "library/tasks/example/instruction.md",
        "research/experiments/spec.json",
        "agents/config.yaml",
        ".githooks/post-merge",
    ],
)
def test_profile_scope_does_not_hide_runtime_changes(tmp_path: Path, relative: str) -> None:
    _repository(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n")
    _git(tmp_path, "add", relative)
    _git(tmp_path, "commit", "-qm", "runtime change")
    output = subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/profile/change_scope.py"), base, "HEAD"],
        cwd=tmp_path,
        text=True,
    )
    assert output.strip() == "profile=true"


def test_profile_scope_skips_only_proven_documentation_changes(tmp_path: Path) -> None:
    _repository(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("documentation\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "documentation")
    command = [sys.executable, str(ROOT / "scripts/profile/change_scope.py")]
    assert (
        subprocess.check_output([*command, base, "HEAD"], cwd=tmp_path, text=True).strip()
        == "profile=false"
    )
    assert (
        subprocess.check_output([*command, "unavailable", "HEAD"], cwd=tmp_path, text=True).strip()
        == "profile=true"
    )


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


def test_ci_workflow_lane_gating_and_wheelhouse_triggers() -> None:
    ci_path = ROOT / ".github/workflows/ci.yml"
    wheelhouse_path = ROOT / ".github/workflows/mcp-wheelhouse-platform.yml"

    ci = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    wheelhouse = yaml.safe_load(wheelhouse_path.read_text(encoding="utf-8"))

    assert "scope" in ci["jobs"]
    assert ci["jobs"]["scope"]["outputs"]["profile"] == "${{ steps.changes.outputs.profile }}"
    assert ci["jobs"]["test"]["needs"] == "scope"

    test_steps = ci["jobs"]["test"]["steps"]
    full_suite_steps = [
        s
        for s in test_steps
        if "uv run --no-sync pytest" in s.get("run", "")
        and "-m docs_consumer" not in s.get("run", "")
    ]
    docs_lane_steps = [
        s for s in test_steps if "uv run --no-sync pytest -m docs_consumer" in s.get("run", "")
    ]

    assert len(full_suite_steps) == 1, "expected exactly one full test suite step"
    assert len(docs_lane_steps) == 1, "expected exactly one docs_consumer test suite step"

    assert "needs.scope.outputs.profile == 'true'" in full_suite_steps[0].get("if", "")
    assert "needs.scope.outputs.profile == 'false'" in docs_lane_steps[0].get("if", "")

    assert "GITHUB_STEP_SUMMARY" in full_suite_steps[0].get("run", "")
    assert "GITHUB_STEP_SUMMARY" in docs_lane_steps[0].get("run", "")

    on_triggers = wheelhouse.get("on") or wheelhouse.get(True) or {}
    push_branches = on_triggers.get("push", {}).get("branches", [])
    assert push_branches == ["main", "integrate/**"]

    concurrency = wheelhouse.get("concurrency", {})
    assert concurrency.get("cancel-in-progress") is True
    assert "github.workflow" in concurrency.get("group", "")


WORKFLOW_FILES = sorted((ROOT / ".github/workflows").glob("*.yml"))
# Intentional exemptions mapped to documented reasons. There are currently none;
# all workflows triggering on pull_request or push must declare concurrency cancellation.
WORKFLOW_CONCURRENCY_EXEMPTIONS: dict[str, str] = {}


@pytest.mark.parametrize("workflow_path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_workflows_with_pr_or_push_declare_cancel_in_progress_concurrency(
    workflow_path: Path,
) -> None:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    triggers = workflow.get("on") or workflow.get(True) or {}
    if isinstance(triggers, str):
        triggers = [triggers]
    if isinstance(triggers, list):
        trigger_names = set(triggers)
    elif isinstance(triggers, dict):
        trigger_names = set(triggers.keys())
    else:
        trigger_names = set()

    if not (trigger_names & {"pull_request", "push"}):
        return

    if workflow_path.name in WORKFLOW_CONCURRENCY_EXEMPTIONS:
        pytest.skip(
            f"Workflow {workflow_path.name} is intentionally exempt from concurrency check: "
            f"{WORKFLOW_CONCURRENCY_EXEMPTIONS[workflow_path.name]}"
        )

    concurrency = workflow.get("concurrency")
    assert concurrency is not None, (
        f"Workflow '{workflow_path.name}' triggers on pull_request/push but lacks a concurrency block."
    )
    assert isinstance(concurrency, dict), (
        f"Workflow '{workflow_path.name}' concurrency block must be a mapping."
    )
    group = concurrency.get("group")
    assert group and isinstance(group, str), (
        f"Workflow '{workflow_path.name}' must declare a non-empty concurrency group."
    )
    assert concurrency.get("cancel-in-progress") is True, (
        f"Workflow '{workflow_path.name}' concurrency must set cancel-in-progress: true."
    )


def test_workbench_certification_workflows_cadence_and_path_isolation() -> None:
    cert_workflows = ["tau-knowledge.yml", "funcdag-workbench-certification.yml"]
    for filename in cert_workflows:
        workflow_path = ROOT / ".github/workflows" / filename
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        triggers = workflow.get("on") or workflow.get(True) or {}
        assert isinstance(triggers, dict), f"{filename} triggers must be a mapping"

        # Assert schedule trigger is declared (weekly cadence)
        schedule = triggers.get("schedule")
        assert schedule is not None and isinstance(schedule, list) and len(schedule) > 0, (
            f"{filename} must declare a schedule trigger"
        )
        assert any(
            isinstance(entry, dict) and entry.get("cron") == "17 4 * * 1" for entry in schedule
        ), f"{filename} schedule must specify weekly cron '17 4 * * 1'"

        # Assert task_workbench paths are not listed in any trigger path filters
        for trigger_name, trigger_config in triggers.items():
            if isinstance(trigger_config, dict):
                paths = trigger_config.get("paths", [])
                for path in paths:
                    assert "task_workbench" not in path, (
                        f"{filename} trigger '{trigger_name}' must not list task_workbench path: {path}"
                    )
