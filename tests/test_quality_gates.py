"""Exercise gate failures against repository text without invoking host tools.

The Harbor CLI pin is imported from ``evallab.runner`` rather than copied here,
so the gate and doctor compare workflow installs with the same constant.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from evallab.runner import HARBOR_CLI_PIN

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


WORKFLOW_FILES = sorted((ROOT / ".github/workflows").glob("*.yml"))
# Intentional exemptions mapped to documented reasons.
# No dispatch-only manual workflows remain: the dose-ladder dispatch-only reduction
# is superseded; its push/pull triggers are preserved for automatic coverage.
# Workflows exempt from PR/push triggers (dispatch-only manual workflows) are explicitly tracked.
DISPATCH_ONLY_WORKFLOW_EXEMPTIONS: dict[str, str] = {}
WORKFLOW_CONCURRENCY_EXEMPTIONS: dict[str, str] = {}


def test_dispatch_only_workflows_exemption_list() -> None:
    """Validate that no workflow is dispatch-only without an explicit exemption.

    Workflows without pull_request or push triggers must belong to the explicit
    dispatch-only exemption list, and every exempted workflow must actually exist
    and be dispatch-only. The maintenance dose-ladder dispatch-only exemption is
    superseded: dose-ladder retains automatic push/pull triggers.
    """
    dispatch_only: list[str] = []
    for workflow_path in WORKFLOW_FILES:
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
            # Workflow has no PR or push trigger; must be dispatch-only
            assert trigger_names == {"workflow_dispatch"}, (
                f"Workflow '{workflow_path.name}' has no PR/push triggers but triggers are {trigger_names}."
            )
            dispatch_only.append(workflow_path.stem)

    assert sorted(dispatch_only) == sorted(DISPATCH_ONLY_WORKFLOW_EXEMPTIONS.keys()) == []
    assert list(DISPATCH_ONLY_WORKFLOW_EXEMPTIONS.keys()) == []


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

    # Workflows owning a `*-required` gate are asserted by
    # test_required_gate_workflows_never_cancel_a_running_run: they must not cancel.
    if any(name.endswith("-required") for name in (workflow.get("jobs") or {})):
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
    assert concurrency.get("cancel-in-progress") == "${{ github.event_name == 'pull_request' }}", (
        f"Workflow '{workflow_path.name}' must cancel superseded PR runs, never push runs."
    )
    assert "${{ github.event.pull_request.number || github.run_id }}" in group, (
        f"Workflow '{workflow_path.name}' must group PR runs together and isolate non-PR runs "
        "by run_id so newer pushes cannot replace pending runs."
    )


def test_required_gate_workflows_never_cancel_a_running_run() -> None:
    """A cancelled prerequisite fails its own gate, so a running gate run is never killed.

    Cancellation is not evidence about the commit: GitHub delivered two
    pull_request events for the unchanged head 55da087, the concurrency group
    cancelled the first run, and its `always()` gate reported
    `typecheck-required` failed on a commit whose typecheck was green (HAR-232).
    Gate workflows keep a per-PR group but set `cancel-in-progress: false`, so
    GitHub may only replace a pending, superseded run and the newest head always
    gets its own verdict.
    """
    gate_workflows: set[str] = set()
    for workflow_path in WORKFLOW_FILES:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        gates = {name for name in (workflow.get("jobs") or {}) if name.endswith("-required")}
        if not gates:
            continue
        gate_workflows.add(workflow_path.name)
        concurrency = workflow.get("concurrency")
        assert isinstance(concurrency, dict), (
            f"Workflow '{workflow_path.name}' owns {sorted(gates)} and must declare a "
            "per-PR concurrency group with cancel-in-progress: false."
        )
        assert concurrency.get("group"), (
            f"Workflow '{workflow_path.name}' must declare a non-empty concurrency group."
        )
        assert concurrency.get("cancel-in-progress") is False, (
            f"Workflow '{workflow_path.name}' owns {sorted(gates)} and must set "
            "cancel-in-progress: false: a cancelled run fails its always() gate and "
            "publishes a failed required check for a commit that was never evaluated."
        )
        assert "${{ github.event.pull_request.number || github.run_id }}" in concurrency["group"], (
            f"Workflow '{workflow_path.name}' must group PR runs together and isolate non-PR "
            "runs by run_id so newer pushes cannot replace pending runs (HAR-234)."
        )

    assert gate_workflows == {"ci.yml", "typecheck.yml"}


_PYTHON_RUN_LINE = re.compile(r"(?<![\w./-])python3?\b")
# Bootstrapping uv via `python3 -m pip install --user uv==...` is interpreter-version
# agnostic: the uv wheel lands under ~/.local/bin on PATH regardless of which CPython
# the runner image ships. Every other python/python3 invocation must come after the
# job pins its interpreter, or a runner-image bump (ubuntu-26.04 shipping no system
# 3.12) silently changes what the code runs on.
_UV_BOOTSTRAP_LINE = re.compile(r"python3?\s+-m\s+pip\s+install\b")


@pytest.mark.parametrize("workflow_path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_job_declares_timeout_minutes(workflow_path: Path) -> None:
    """Every job must cap its wall time explicitly.

    A job without timeout-minutes inherits GitHub's 6 h default, so a hung step
    burns runner minutes for hours before anyone notices (HAR-245; the
    linux-cp312-wheelhouse job in mcp-wheelhouse-platform.yml carried no bound).
    """
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs") or {}
    assert jobs, f"Workflow '{workflow_path.name}' declares no jobs."
    for job_name, job in jobs.items():
        assert isinstance(job, dict) and isinstance(job.get("timeout-minutes"), (int, float)), (
            f"Job '{job_name}' in '{workflow_path.name}' must declare a numeric "
            "timeout-minutes so a hung runner cannot burn the 6 h GitHub default."
        )


@pytest.mark.parametrize("workflow_path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_python_run_steps_follow_an_interpreter_pin(workflow_path: Path) -> None:
    """Bare python/python3 invocations must come after the job pins an interpreter.

    tau-knowledge-lean failed three runs on 2026-10-29 (runs 40937083639 and
    40802303644) when ubuntu-latest moved to 26.04: bare python3 resolved to a
    different interpreter and the wheelhouse path lost 3.12. Every job that
    executes python code must first pin the interpreter with
    actions/setup-python or `uv python install`.

    Exempted: `python3 -m pip install --user uv==...` bootstrap lines, which are
    version-agnostic (uv is a self-contained binary on PATH), and the
    `uv python install` line itself, which is the pin. Invocation is checked per
    run-script line so a pin earlier in the same script still counts.
    """
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs") or {}
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps") or []
        pinned = False
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            uses = step.get("uses") or ""
            run = step.get("run") or ""
            if "actions/setup-python" in uses:
                pinned = True
            for line in run.splitlines():
                if "uv python install" in line:
                    pinned = True
                    continue
                if _UV_BOOTSTRAP_LINE.search(line):
                    continue
                if _PYTHON_RUN_LINE.search(line):
                    assert pinned, (
                        f"Job '{job_name}' in '{workflow_path.name}' invokes a bare "
                        f"python interpreter at step {index} "
                        f"({step.get('name') or '<unnamed>'}) before pinning one. "
                        "Add an earlier actions/setup-python step or run "
                        "`uv python install <version>` first (HAR-245; bare python3 "
                        "broke tau-knowledge-lean when ubuntu-latest moved to 26.04)."
                    )


def test_workbench_certification_workflows_cadence_and_path_isolation() -> None:
    """Preserved automatic triggers: no weekly schedule, task_workbench paths retained.

    The maintenance weekly schedule and task_workbench path removal are superseded.
    Certification still runs automatically on workbench changes; no new schedule is
    added as implied authorization for paid/old research experiments.
    """
    cert_workflows = ["tau-knowledge.yml", "funcdag-workbench-certification.yml"]
    for filename in cert_workflows:
        workflow_path = ROOT / ".github/workflows" / filename
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        triggers = workflow.get("on") or workflow.get(True) or {}
        assert isinstance(triggers, dict), f"{filename} triggers must be a mapping"

        # No schedule trigger is declared (no new schedules as implied authorization).
        assert triggers.get("schedule") is None, f"{filename} must not declare a schedule trigger"

        # task_workbench paths are preserved for automatic coverage.
        found_task_workbench = False
        for trigger_config in triggers.values():
            if isinstance(trigger_config, dict):
                paths = trigger_config.get("paths", [])
                for path in paths:
                    if "task_workbench" in path:
                        found_task_workbench = True
        assert found_task_workbench, (
            f"{filename} must list task_workbench paths for automatic coverage"
        )


WORKFLOW_DIR = ROOT / ".github/workflows"
PREMERGE_SCRIPT = ROOT / "scripts/premerge.sh"
# The Harbor CLI pin has one home: evallab.runner.HARBOR_CLI_PIN, which doctor
# also compares the local CLI against (HAR-265). A bump changes that constant
# and every workflow together; the test below refuses drift.


# HAR-252: mcp-wheelhouse-platform run 41387496649 on main failed with "Failed to
# download pytz==2026.5 after 3 retries" -- uv's default UV_HTTP_RETRIES (3) at the
# moment a single PyPI/index blip hit a job that fetched packages cold every run.
# uv honours UV_HTTP_RETRIES (added in 0.7.21; the workflows pin 0.9.24) for sync,
# `uv run`, `uv tool install`, `uv python install` and `uvx`, so one variable at job
# or workflow scope bounds every download the job makes. The cache step removes the
# download itself; retries cover the download that still happens (cache miss, new
# lock hash, wheelhouse fetch).
_UV_RETRY_FLOOR = 5
_UV_CACHE_PATH = "~/.cache/uv"
_UV_SYNC_LINE = re.compile(r"(?<![\w./-])uv\s+sync\b")
_UV_RUN_LINE = re.compile(r"(?<![\w./-])uv\s+run\b")
_UV_FETCH_LINE = re.compile(
    r"(?<![\w./-])uv\s+(?:tool\s+install|python\s+install)\b|(?<![\w./-])uvx\b"
)
_UV_LOCK_HASH = re.compile(r"hashFiles\(\s*'uv\.lock'")


def _step_runs(job: dict) -> list[tuple[int, str]]:
    return [
        (index, step["run"])
        for index, step in enumerate(job.get("steps") or [])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]


def _syncs_environment(run: str) -> bool:
    """True when a run script resolves and installs the locked environment."""
    for line in run.splitlines():
        if _UV_SYNC_LINE.search(line):
            return True
        # `uv run` without --no-sync performs an implicit `uv sync`.
        if _UV_RUN_LINE.search(line) and "--no-sync" not in line:
            return True
    return False


def _downloads_from_index(run: str) -> bool:
    return _syncs_environment(run) or bool(_UV_FETCH_LINE.search(run))


def _uv_cache_steps(job: dict) -> list[tuple[int, dict]]:
    found = []
    for index, step in enumerate(job.get("steps") or []):
        if not isinstance(step, dict):
            continue
        uses = step.get("uses") or ""
        params = step.get("with") or {}
        if (
            isinstance(uses, str)
            and uses.startswith("actions/cache@")
            and _UV_CACHE_PATH in str(params.get("path", ""))
        ):
            found.append((index, step))
    return found


def _uv_retry_floor(workflow: dict, job: dict) -> int | None:
    for scope in (job.get("env") or {}, workflow.get("env") or {}):
        if isinstance(scope, dict) and "UV_HTTP_RETRIES" in scope:
            try:
                return int(str(scope["UV_HTTP_RETRIES"]))
            except ValueError:
                return None
    return None


def _uv_download_problems(workflow: dict) -> list[str]:
    """Report jobs that reach an index without bounded retries or a warm cache."""
    problems: list[str] = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        runs = _step_runs(job)
        sync_steps = [index for index, run in runs if _syncs_environment(run)]
        if not sync_steps and not any(_downloads_from_index(run) for _, run in runs):
            continue

        retries = _uv_retry_floor(workflow, job)
        if retries is None or retries < _UV_RETRY_FLOOR:
            problems.append(
                f"job '{job_name}' downloads packages with UV_HTTP_RETRIES="
                f"{retries!r}; set UV_HTTP_RETRIES >= {_UV_RETRY_FLOOR}"
            )

        if not sync_steps:
            continue
        caches = _uv_cache_steps(job)
        if not caches:
            problems.append(
                f"job '{job_name}' syncs the environment without an actions/cache "
                f"step on {_UV_CACHE_PATH} keyed by hashFiles('uv.lock')"
            )
            continue
        cache_index, cache_step = caches[0]
        key = str((cache_step.get("with") or {}).get("key", ""))
        if not _UV_LOCK_HASH.search(key):
            problems.append(
                f"job '{job_name}' cache key {key!r} is not keyed by "
                "hashFiles('uv.lock'), so a changed lock reuses stale packages"
            )
        if cache_index > sync_steps[0]:
            problems.append(
                f"job '{job_name}' restores the uv cache after its first sync step, "
                "so that sync cannot use it"
            )
    return problems


@pytest.mark.parametrize("workflow_path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_uv_downloading_jobs_cache_packages_and_retry(workflow_path: Path) -> None:
    """Every job that resolves packages must cache the uv cache and retry downloads.

    A single PyPI blip reddened main (mcp-wheelhouse-platform run 41387496649) because
    uv stops after 3 retries by default and only the quality/typecheck jobs restored
    a package cache. `uv run` without --no-sync syncs implicitly, so those jobs are
    covered too; a job that only installs a tool or an interpreter needs the retry
    floor but no project cache.
    """
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    problems = _uv_download_problems(workflow)
    assert not problems, f"'{workflow_path.name}' violates HAR-252: " + "; ".join(problems)


def _fixture_workflow(
    *,
    sync: str = "uv sync --locked",
    has_cache: bool = True,
    cache_before_sync: bool = True,
    key: str = "uv-0.9.24-${{ runner.os }}-3.12-fixture-${{ hashFiles('uv.lock') }}",
    retries: str | int | None = "5",
) -> dict:
    cache_step = {
        "name": "Cache locked Python packages",
        "uses": "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9",
        "with": {"path": "~/.cache/uv", "key": key},
    }
    steps: list[dict] = [{"uses": "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"}]
    if has_cache and cache_before_sync:
        steps.append(cache_step)
    steps.append({"name": "Sync", "run": sync})
    if has_cache and not cache_before_sync:
        steps.append(cache_step)
    workflow: dict = {
        "jobs": {"fixture": {"runs-on": "ubuntu-latest", "timeout-minutes": 10, "steps": steps}}
    }
    if retries is not None:
        workflow["env"] = {"UV_HTTP_RETRIES": retries}
    return workflow


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ({}, None),
        ({"retries": 5}, None),
        ({"sync": "uv run --no-sync pytest -q", "has_cache": False}, None),
        ({"retries": None}, "UV_HTTP_RETRIES"),
        ({"retries": "3"}, "UV_HTTP_RETRIES"),
        ({"has_cache": False}, "actions/cache"),
        ({"sync": "uv run pytest -q", "has_cache": False}, "actions/cache"),
        ({"cache_before_sync": False}, "after its first sync step"),
        ({"key": "uv-cache-${{ runner.os }}"}, "hashFiles('uv.lock')"),
    ],
)
def test_uv_download_governance_rejects_unprotected_fixture_jobs(
    fixture: dict, expected: str | None
) -> None:
    """The gate must fail a uv-sync job missing the cache step or the retry floor.

    A fixture keeps the failure mode independent of the repository's own workflows:
    dropping the cache step, lowering the retry count, or restoring the cache after
    the sync each has to fail here even after the real workflows are repaired.
    """
    problems = _uv_download_problems(_fixture_workflow(**fixture))
    if expected is None:
        assert problems == []
    else:
        assert any(expected in problem for problem in problems), problems


_UV_VERSION_DECLARATION = re.compile(r'^readonly UV_VERSION="(?P<version>[^"]+)"\s*$', re.MULTILINE)
_UV_PIN = re.compile(r"\buv==(?P<version>[0-9][\w.]*)")
_UV_COMMAND = re.compile(
    r"\b(?:uv\s+(?:run|sync|python|tool|pip|venv|lock|export|add|remove)\b|uvx\b)"
)
_INSTALLER = re.compile(r"\b(?:pip3?|pipx|uvx|uv\s+tool\s+install|uv\s+pip\s+install)\b")
# A first package spec naming the Harbor CLI: `harbor`, `harbor==X`, `harbor[extra]`.
_HARBOR_SPEC = re.compile(r"harbor(?=$|==|\[|@|>|<|~=)")
_INSTALL_VERBS = frozenset({"install", "download", "add", "upgrade"})
HARBOR_INSTALLER = "uv tool install"


def _uv_version(premerge: Path) -> str:
    match = _UV_VERSION_DECLARATION.search(premerge.read_text(encoding="utf-8"))
    assert match is not None, f"{premerge} must declare a quoted readonly UV_VERSION"
    return match.group("version")


def _install_specs(line: str) -> list[tuple[str, str]]:
    """Return (installer, first package spec) for every install command on the line.

    Taking the first non-option token after the installer keeps file paths and
    modules that merely contain "harbor" (e.g. `uvx ty@0.0.71 check
    src/evallab/harbor_deepseek.py`) from reading as a Harbor installation.
    """
    specs: list[tuple[str, str]] = []
    for installer in _INSTALLER.finditer(line):
        for token in line[installer.end() :].split():
            if token in _INSTALL_VERBS or token.startswith("-"):
                continue
            specs.append((" ".join(installer.group(0).split()), token.strip("'\"")))
            break
    return specs


def _pin_findings(workflows_dir: Path, premerge: Path) -> list[str]:
    """Return one message per workflow line that breaks the single uv/harbor pin.

    `scripts/premerge.sh` declares UV_VERSION for the local premerge gate. A
    workflow pinned to anything else silently splits hosted CI from local
    premerge, so every `uv==X` there must equal it, and a workflow that invokes
    uv at all must pin it. Harbor is only ever installed from PyPI through
    `uv tool install "harbor==<pin>"`: a pip install (loca-lean.yml used
    `python3 -m pip install harbor==0.21.0`) or a floating pin converges on a
    different installer and version than the other benchmark workflows.
    """
    uv_version = _uv_version(premerge)
    findings: list[str] = []
    for workflow_path in sorted(workflows_dir.glob("*.yml")):
        text = workflow_path.read_text(encoding="utf-8")
        if _UV_COMMAND.search(text) and not _UV_PIN.search(text):
            findings.append(
                f"{workflow_path.name}: invokes uv without a uv=={uv_version} pin "
                "(scripts/premerge.sh UV_VERSION)"
            )
        for line_number, raw in enumerate(text.splitlines(), 1):
            if raw.strip().startswith("#"):
                continue
            for match in _UV_PIN.finditer(raw):
                if match.group("version") != uv_version:
                    findings.append(
                        f"{workflow_path.name}:{line_number}: uv pin "
                        f"{match.group('version')} != {uv_version} "
                        "(scripts/premerge.sh UV_VERSION)"
                    )
            if "harbor" not in raw:
                continue
            for installer, spec in _install_specs(raw):
                if not _HARBOR_SPEC.match(spec):
                    continue
                if installer != HARBOR_INSTALLER:
                    findings.append(
                        f"{workflow_path.name}:{line_number}: harbor must be installed with "
                        f'uv tool install "harbor=={HARBOR_CLI_PIN}", found '
                        f"{installer} {spec}"
                    )
                elif spec != f"harbor=={HARBOR_CLI_PIN}":
                    findings.append(
                        f"{workflow_path.name}:{line_number}: harbor pin {spec} "
                        f"!= harbor=={HARBOR_CLI_PIN}"
                    )
    return findings


def test_every_workflow_pins_uv_and_harbor_to_one_version() -> None:
    """CI and local premerge install one uv and one Harbor version (HAR-251).

    Thirteen workflows copied `uv==0.9.24` from scripts/premerge.sh and seven
    copied `harbor==0.21.0`, while loca-lean.yml ran `python3 -m pip install
    harbor==0.21.0`. Nothing compared those literals, so a single bump split
    hosted CI from local premerge. This gate makes the drift visible at review
    time instead of at the first divergent run.
    """
    assert WORKFLOW_FILES, "no workflows found to check"
    assert _pin_findings(WORKFLOW_DIR, PREMERGE_SCRIPT) == []

    harbor_installs = {
        workflow_path.name
        for workflow_path in WORKFLOW_FILES
        if any(
            installer == HARBOR_INSTALLER and spec == f"harbor=={HARBOR_CLI_PIN}"
            for line in workflow_path.read_text(encoding="utf-8").splitlines()
            for installer, spec in _install_specs(line)
        )
    }
    assert "loca-lean.yml" in harbor_installs, (
        "loca-lean.yml must install Harbor with uv tool install, not pip"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "      - run: |\n"
            "          python3 -m pip install --user uv==0.9.24\n"
            f'          uv tool install "harbor=={HARBOR_CLI_PIN}"\n',
            None,
            id="matching-pins",
        ),
        pytest.param(
            "          python3 -m pip install --user uv==0.9.23\n",
            "uv pin 0.9.23 != 0.9.24",
            id="uv-version-drift",
        ),
        pytest.param(
            "          python3 -m pip install --user uv==0.9.24\n"
            '          uv tool install "harbor==0.20.0"\n',
            f"harbor pin harbor==0.20.0 != harbor=={HARBOR_CLI_PIN}",
            id="harbor-version-drift",
        ),
        pytest.param(
            "          python3 -m pip install --user uv==0.9.24\n"
            "          python3 -m pip install --disable-pip-version-check harbor==0.21.0\n",
            "harbor must be installed with uv tool install",
            id="harbor-installed-with-pip",
        ),
        pytest.param(
            "          python3 -m pip install --user uv==0.9.24\n"
            "          uv tool install harbor\n",
            f"harbor pin harbor != harbor=={HARBOR_CLI_PIN}",
            id="harbor-unpinned",
        ),
        pytest.param(
            "          python3 -m pip install --user uv==0.9.24\n"
            "          uvx --from harbor==0.21.0 harbor run --path .\n",
            "harbor must be installed with uv tool install",
            id="harbor-installed-by-uvx",
        ),
        pytest.param(
            "          uv sync --locked\n",
            "invokes uv without a uv==0.9.24 pin",
            id="uv-invoked-without-pin",
        ),
        pytest.param(
            "          python3 -m pip install --user uv==0.9.24\n"
            "          uvx ty@0.0.71 check src/evallab/harbor_deepseek.py\n",
            None,
            id="harbor-in-a-file-path-is-not-an-install",
        ),
    ],
)
def test_pin_gate_rejects_workflow_drift(tmp_path: Path, body: str, expected: str | None) -> None:
    """The gate must fail on drifted fixtures, not merely pass on this checkout."""
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "fixture.yml").write_text(f"jobs:\n  job:\n    steps:\n{body}", encoding="utf-8")
    premerge = tmp_path / "premerge.sh"
    premerge.write_text('readonly UV_VERSION="0.9.24"\n', encoding="utf-8")

    findings = _pin_findings(workflows, premerge)
    if expected is None:
        assert findings == []
    else:
        assert any(expected in finding for finding in findings), findings


# HAR-260: a test file that skips unless the Harbor SDK is importable only runs
# where the SDK is installed. The main pytest step syncs benchmarks+lance, so
# Harbor is absent there and such files skip or cover only their stubs; the
# native step below installs it through `--extra laminar` and names its files
# explicitly. Nothing compared those two sets, so eight files -- including the
# host-isolation, compose-isolation and agent network-policy suites -- never ran.
NATIVE_HARBOR_STEP = "Native Harbor diff, regrade and observability contracts"
_STEP_TEST_FILE = re.compile(r"\btests/[\w./-]+\.py\b")
_SKIP_CALLS = frozenset({"importorskip", "skip", "skipif"})

# Harbor-named skip guards the native step cannot satisfy, so listing the file
# there would still skip it. Every entry needs a written reason.
HARBOR_STEP_EXEMPTIONS: dict[str, str] = {
    "test_integrity_reward.py": (
        "its only Harbor-named skip needs a Harbor source checkout at "
        "/tmp/harbor024/repo/packages/rewardkit/src; installing harbor[modal] "
        "does not create that tree, so the native step cannot un-skip it"
    ),
}


def _called_name(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _harbor_skip_evidence(source: str) -> str | None:
    """Return the first skip guard in `source` that only an installed Harbor satisfies.

    Two shapes count. `importorskip("harbor.<module>")` gates on the module alone.
    `skip`/`skipif` counts when its message names Harbor, which covers files whose
    guard is `shutil.which("harbor")` or a Harbor source tree rather than a module
    import. A non-Harbor `importorskip` keeps its own reason: the module argument,
    not the prose beside it, decides whether the file runs.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        called = _called_name(node.func)
        leaf = called.rsplit(".", 1)[-1]
        if leaf not in _SKIP_CALLS:
            continue
        arguments = [
            argument.value
            for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        ]
        arguments += [
            keyword.value.value
            for keyword in node.keywords
            if keyword.arg in {"reason", "msg"}
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ]
        for argument in arguments:
            if leaf == "importorskip":
                gated = argument == "harbor" or argument.startswith("harbor.")
            else:
                gated = "harbor" in argument.lower()
            if gated:
                return f"line {node.lineno}: {called}({argument!r})"
    return None


def _harbor_gated_files(tests_dir: Path) -> dict[str, str]:
    """Map every test file that skips without Harbor to the guard that skips it."""
    gated: dict[str, str] = {}
    for path in sorted(tests_dir.glob("test_*.py")):
        evidence = _harbor_skip_evidence(path.read_text(encoding="utf-8"))
        if evidence is not None:
            gated[path.name] = evidence
    return gated


# Scanned once: the parametrised drop test below reuses it for every gated file.
REPO_HARBOR_GATED_TESTS: dict[str, str] = _harbor_gated_files(ROOT / "tests")


def _native_harbor_step(workflow: dict) -> dict:
    for job in (workflow.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("name") == NATIVE_HARBOR_STEP:
                return step
    raise AssertionError(f"no workflow step is named {NATIVE_HARBOR_STEP!r}")


def _native_harbor_step_files(workflow: dict) -> list[str]:
    run = _native_harbor_step(workflow).get("run") or ""
    executable = "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))
    return [Path(match).name for match in _STEP_TEST_FILE.findall(executable)]


def test_native_harbor_step_files_ignore_commented_paths() -> None:
    """A path named in a step comment is documentation, not a listed test file."""
    workflow = {
        "jobs": {
            "test": {
                "steps": [
                    {
                        "name": NATIVE_HARBOR_STEP,
                        "run": (
                            "uv run --no-sync pytest -n 0 \\\n"
                            "  # covered by tests/test_gated.py\n"
                            "  tests/test_plain.py\n"
                        ),
                    }
                ]
            }
        }
    }
    assert _native_harbor_step_files(workflow) == ["test_plain.py"]


def _harbor_step_problems(
    tests_dir: Path,
    listed: list[str],
    exemptions: dict[str, str],
    gated: dict[str, str] | None = None,
) -> list[str]:
    """Report gated files the native step omits and broken entries in its list."""
    problems: list[str] = []
    if gated is None:
        gated = _harbor_gated_files(tests_dir)
    for name in listed:
        if not (tests_dir / name).is_file():
            problems.append(f"native Harbor step lists tests/{name}, which does not exist")
    duplicates = sorted({name for name in listed if listed.count(name) > 1})
    if duplicates:
        problems.append(f"native Harbor step lists duplicates: {', '.join(duplicates)}")
    for name, evidence in sorted(gated.items()):
        if name in listed or name in exemptions:
            continue
        problems.append(
            f"tests/{name} skips unless Harbor is installed ({evidence}) but the "
            "native Harbor step does not run it"
        )
    for name, reason in exemptions.items():
        if not reason.strip():
            problems.append(f"native Harbor step exemption {name!r} has no written reason")
        if name not in gated:
            problems.append(
                f"native Harbor step exemption {name!r} is stale: the file has no Harbor skip guard"
            )
        elif name in listed:
            problems.append(
                f"native Harbor step exemption {name!r} is redundant: the file is listed"
            )
    return problems


def test_full_suite_writes_junit_then_reports_skip_census() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    full_suite_index = next(
        (
            index
            for index, step in enumerate(steps)
            if isinstance(step, dict) and step.get("name") == "Full test suite"
        ),
        None,
    )
    assert full_suite_index is not None, "the test job must retain its Full test suite step"
    full_suite_run = steps[full_suite_index].get("run") or ""
    assert "--junitxml=full-suite.xml" in full_suite_run, (
        "the full suite must write the JUnit report consumed by the skip census"
    )

    slowest = steps[full_suite_index + 1]
    assert slowest.get("name") == "Slowest test files", (
        "the slowest-files report must immediately follow the full suite"
    )
    assert "scripts/ci/report_slowest_files.py full-suite.xml" in (slowest.get("run") or "")

    census = steps[full_suite_index + 2]
    assert census.get("name") == "Full-suite skip reason census", (
        "the skip census must follow the slowest-files report"
    )
    census_run = census.get("run") or ""
    assert "scripts/ci/check_harbor_skips.py --report-only full-suite.xml" in census_run


def _junit_artifact_problems(job: dict) -> list[str]:
    """Report what keeps a test shard from reporting and keeping its JUnit files (HAR-305)."""
    steps = [step for step in job.get("steps") or [] if isinstance(step, dict)]
    problems: list[str] = []
    suite = next((i for i, s in enumerate(steps) if s.get("name") == "Full test suite"), None)
    native = next((i for i, s in enumerate(steps) if s.get("name") == NATIVE_HARBOR_STEP), None)
    if suite is None or native is None:
        return ["the test job lost its Full test suite or native Harbor step"]
    slowest = [
        i
        for i, s in enumerate(steps)
        if "scripts/ci/report_slowest_files.py full-suite.xml" in (s.get("run") or "")
    ]
    if slowest != [suite + 1]:
        problems.append("report_slowest_files.py must run in the step right after Full test suite")
    uploads = [
        (i, s)
        for i, s in enumerate(steps)
        if str(s.get("uses") or "").startswith("actions/upload-artifact@")
    ]
    if len(uploads) != 1:
        return problems + ["the test job must have exactly one upload-artifact step"]
    index, upload = uploads[0]
    with_ = upload.get("with") or {}
    if index <= native:
        problems.append("the JUnit upload must run after the native Harbor step")
    if upload.get("if") != "always()":
        problems.append("the JUnit upload must run with if: always()")
    if not re.fullmatch(
        r"junit-\$\{\{ matrix\.python-version \}\}-\$\{\{ [\w.]+ \}\}",
        str(with_.get("name") or ""),
    ):
        problems.append("the JUnit artifact must be named junit-<python>-<sanitised shard>")
    if "matrix.shard }}" in str(with_.get("name") or ""):
        problems.append("the JUnit artifact name must not embed the raw shard slash")
    paths = str(with_.get("path") or "").split()
    if sorted(paths) != ["full-suite.xml", "harbor-native.xml"]:
        problems.append("the JUnit upload must contain full-suite.xml and harbor-native.xml")
    if with_.get("retention-days") != 14:
        problems.append("the JUnit upload must set retention-days: 14")
    return problems


def test_test_shards_report_slowest_files_and_upload_junit() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["test"]
    assert _junit_artifact_problems(job) == []

    without_slowest = {
        **job,
        "steps": [s for s in job["steps"] if s.get("name") != "Slowest test files"],
    }
    assert any("report_slowest_files" in p for p in _junit_artifact_problems(without_slowest))

    without_upload = {
        **job,
        "steps": [
            s
            for s in job["steps"]
            if not str(s.get("uses") or "").startswith("actions/upload-artifact")
        ],
    }
    assert any("upload-artifact" in p for p in _junit_artifact_problems(without_upload))


def _live_postgres_problems(job: dict) -> list[str]:
    """Report what keeps the full suite from running its live-PostgreSQL tests (HAR-297)."""
    problems: list[str] = []
    postgres = (job.get("services") or {}).get("postgres")
    if not isinstance(postgres, dict):
        problems.append("the test job must declare a services.postgres container")
    else:
        if not str(postgres.get("image", "")).startswith("postgres:18.4"):
            problems.append(
                "services.postgres must use the Postgres 18.4 image pinned in compose.yaml"
            )
        if "54329:5432" not in [str(port) for port in postgres.get("ports") or []]:
            problems.append("services.postgres must publish 54329:5432, the default DSN port")
        if "pg_isready" not in str(postgres.get("options", "")):
            problems.append("services.postgres must wait for pg_isready before steps run")
    step = next(
        (
            s
            for s in job.get("steps", [])
            if isinstance(s, dict) and s.get("name") == "Full test suite"
        ),
        {},
    )
    env = step.get("env") or {}
    if str(env.get("EVALLAB_RUN_POSTGRES_TESTS")) != "1":
        problems.append("Full test suite must set EVALLAB_RUN_POSTGRES_TESTS=1")
    if ":54329/" not in str(env.get("DATABASE_URL", "")):
        problems.append("Full test suite must set DATABASE_URL to the service on port 54329")
    steps = [s for s in job.get("steps", []) if isinstance(s, dict)]
    init_index = next(
        (i for i, s in enumerate(steps) if "evallab db init" in str(s.get("run"))), None
    )
    suite_index = next((i for i, s in enumerate(steps) if s.get("name") == "Full test suite"), None)
    if init_index is None or suite_index is None or init_index > suite_index:
        problems.append("evallab db init must run before the Full test suite step")
    return problems


def test_full_suite_runs_live_postgres_tests_against_a_service() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    assert _live_postgres_problems(workflow["jobs"]["test"]) == []


def test_live_postgres_gate_rejects_missing_service_or_opt_in() -> None:
    job = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))["jobs"]["test"]
    no_service = {**job, "services": {}}
    assert any("services.postgres" in p for p in _live_postgres_problems(no_service))
    steps = [
        {**s, "env": {k: v for k, v in s["env"].items() if k != "EVALLAB_RUN_POSTGRES_TESTS"}}
        if s.get("name") == "Full test suite"
        else s
        for s in job["steps"]
    ]
    no_opt_in = {**job, "steps": steps}
    assert any("EVALLAB_RUN_POSTGRES_TESTS" in p for p in _live_postgres_problems(no_opt_in))


def _postgres_server_binaries_problems(steps: list[dict]) -> list[str]:
    """Report when the test job does not put the runner's PostgreSQL bin dir on PATH.

    The corpus-queue and cloud-trace tests skip unless initdb and pg_ctl are on PATH,
    and the ubuntu runner image keeps them under /usr/lib/postgresql/<major>/bin.
    """
    names = [s.get("name") for s in steps]
    if "Full test suite" not in names:
        return ["the test job must retain its Full test suite step"]
    suite_index = names.index("Full test suite")
    for step in steps[:suite_index]:
        run = str(step.get("run") or "")
        if "/usr/lib/postgresql/" in run and "GITHUB_PATH" in run:
            return []
    return [
        "no step before Full test suite adds /usr/lib/postgresql/<major>/bin to "
        "GITHUB_PATH, so the local-PostgreSQL corpus-queue tests skip (HAR-304)"
    ]


def test_full_suite_finds_postgres_server_binaries_on_path() -> None:
    job = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))["jobs"]["test"]
    steps = [s for s in job["steps"] if isinstance(s, dict)]
    assert _postgres_server_binaries_problems(steps) == []
    dropped = [s for s in steps if "/usr/lib/postgresql/" not in str(s.get("run"))]
    assert _postgres_server_binaries_problems(dropped) != []
    expose = "Expose the runner PostgreSQL server binaries"
    late = [s for s in steps if s.get("name") != expose]
    late.append(next(s for s in steps if s.get("name") == expose))
    assert _postgres_server_binaries_problems(late) != []


def _full_suite_inspect_problems(steps: list[dict]) -> list[str]:
    """Report when the environment the full suite runs in cannot have `inspect`.

    uv sync prunes group packages the command does not name, so the gate looks at
    the LAST sync step before "Full test suite": it must pass `--group inspect`.
    An earlier dedicated inspect sync cannot survive a later sync without it.
    """
    problems: list[str] = []
    full_suite_index = next(
        (
            index
            for index, step in enumerate(steps)
            if isinstance(step, dict) and step.get("name") == "Full test suite"
        ),
        None,
    )
    if full_suite_index is None:
        problems.append("the test job must retain its Full test suite step")
        return problems
    syncs = [
        (index, run)
        for index, run in _step_runs({"steps": steps[:full_suite_index]})
        if _syncs_environment(run)
    ]
    if not syncs:
        problems.append(
            "no uv sync step runs before Full test suite, so the inspect "
            "dependency group cannot be installed"
        )
        return problems
    index, last_sync = syncs[-1]
    if "--group inspect" not in last_sync:
        problems.append(
            f"the last uv sync before Full test suite (step {index}) omits "
            "--group inspect, so the ~25 Inspect-gated tests in "
            "test_scout_metr.py, test_inspect_adapter.py and "
            "test_inspect_storage.py skip (HAR-287)"
        )
    return problems


def test_full_suite_sync_step_installs_the_inspect_group() -> None:
    """The Inspect-gated suites run in the full suite; its env must install them.

    tests/test_scout_metr.py guards on HAVE_INSPECT and test_inspect_adapter.py /
    test_inspect_storage.py importorskip inspect_ai: without `--group inspect` in
    the sync the census logged 23 'inspect dependency group' plus 2 'optional
    Inspect dependency group is not installed' skips per shard (HAR-287).
    """
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    assert _full_suite_inspect_problems(steps) == []


def test_full_suite_inspect_gate_rejects_dropping_the_group() -> None:
    """Dropping --group inspect from the effective sync reddens the gate."""
    dropped = [
        {"run": "uv sync --locked --no-group observability --group benchmarks --group lance"},
        {"name": "Full test suite", "run": "uv run --no-sync pytest"},
    ]
    assert _full_suite_inspect_problems(dropped) == [
        "the last uv sync before Full test suite (step 0) omits --group inspect, "
        "so the ~25 Inspect-gated tests in test_scout_metr.py, "
        "test_inspect_adapter.py and test_inspect_storage.py skip (HAR-287)"
    ]
    assert _full_suite_inspect_problems([{"name": "Full test suite", "run": "pytest"}]) == [
        "no uv sync step runs before Full test suite, so the inspect dependency "
        "group cannot be installed"
    ]
    shadowed = [
        {"run": "uv sync --locked --group inspect"},
        {"run": "uv sync --locked --group benchmarks"},
        {"name": "Full test suite", "run": "uv run --no-sync pytest"},
    ]
    assert _full_suite_inspect_problems(shadowed), (
        "a later sync without --group inspect prunes the group the earlier dedicated step installed"
    )


def test_native_harbor_step_checks_junit_skips_after_pytest() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    run = _native_harbor_step(workflow).get("run") or ""
    lines = run.splitlines()
    pytest_command = next(
        (index for index, line in enumerate(lines) if "uv run --no-sync pytest" in line),
        None,
    )
    checker_command = next(
        (index for index, line in enumerate(lines) if "scripts/ci/check_harbor_skips.py" in line),
        None,
    )
    assert pytest_command is not None and checker_command is not None, (
        "the native Harbor step must run scripts/ci/check_harbor_skips.py after pytest"
    )
    assert checker_command > pytest_command, "the Harbor skip checker must run after pytest"
    assert "--junitxml=harbor-native.xml" in run
    assert "scripts/ci/check_harbor_skips.py harbor-native.xml" in run


def test_every_harbor_gated_test_file_runs_in_the_native_harbor_step() -> None:
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    run = _native_harbor_step(workflow).get("run") or ""
    assert "--extra laminar" in run and "pytest" in run, (
        "the native Harbor step must sync the laminar extra and run pytest, "
        "otherwise Harbor stays absent and its files skip"
    )
    problems = _harbor_step_problems(
        ROOT / "tests",
        _native_harbor_step_files(workflow),
        HARBOR_STEP_EXEMPTIONS,
        REPO_HARBOR_GATED_TESTS,
    )
    assert problems == [], "\n".join(problems)


def test_native_harbor_step_syncs_the_pinned_xiaomi_interpreter() -> None:
    """The mimoagent worker tests run only against tools/mimoagent-harbor/.venv.

    tests/test_mimoagent_worker.py skips every test when
    tools/mimoagent-harbor/.venv/bin/python is absent (31 skips on main run
    41448725961), so the step must `uv sync` the tool project before its
    pytest command. --python 3.12 pins the tool interpreter on the 3.14
    matrix row; tools/mimoagent-harbor/uv.lock is in the step's cache key.
    HAR-271.
    """
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    run = _native_harbor_step(workflow).get("run") or ""
    executable = "\n".join(line for line in run.splitlines() if not line.lstrip().startswith("#"))
    sync = "uv sync --project tools/mimoagent-harbor --locked"
    assert sync in executable, (
        "the native Harbor step must install the pinned Xiaomi interpreter "
        f"with {sync!r} before pytest, or tests/test_mimoagent_worker.py skips"
    )
    assert "mimoagent-harbor/uv.lock" in ((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")), (
        "tools/mimoagent-harbor/uv.lock must be part of the uv cache key"
    )
    assert executable.index(sync) < executable.index("pytest"), (
        "the mimoagent-harbor uv sync must run before the step's pytest command"
    )


@pytest.mark.parametrize("dropped", sorted(REPO_HARBOR_GATED_TESTS))
def test_native_harbor_step_gate_rejects_dropping_a_gated_file(dropped: str) -> None:
    """Dropping any gated file from the step reddens the gate (HAR-260)."""
    workflow = yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8"))
    listed = _native_harbor_step_files(workflow)
    if dropped in HARBOR_STEP_EXEMPTIONS:
        pytest.skip(HARBOR_STEP_EXEMPTIONS[dropped])
    problems = _harbor_step_problems(
        ROOT / "tests",
        [name for name in listed if name != dropped],
        HARBOR_STEP_EXEMPTIONS,
        REPO_HARBOR_GATED_TESTS,
    )
    assert any(dropped in problem for problem in problems), (
        f"removing {dropped} from the native Harbor step produced no problem: {problems}"
    )


def test_harbor_step_gate_rejects_a_gated_file_missing_from_the_list(tmp_path: Path) -> None:
    """The gate must fail on a new gated file, not merely pass on this checkout."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_gated.py").write_text(
        'import pytest\n\npytest.importorskip("harbor.job_diff")\n', encoding="utf-8"
    )
    (tests_dir / "test_plain.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    assert _harbor_step_problems(tests_dir, ["test_plain.py"], {}) == [
        "tests/test_gated.py skips unless Harbor is installed "
        "(line 3: pytest.importorskip('harbor.job_diff')) but the native Harbor step "
        "does not run it"
    ]
    assert _harbor_step_problems(tests_dir, ["test_plain.py"], {"test_gated.py": "probe"}) == []
    assert _harbor_step_problems(tests_dir, ["test_gated.py"], {"test_gated.py": "probe"}) == [
        "native Harbor step exemption 'test_gated.py' is redundant: the file is listed"
    ]
    assert _harbor_step_problems(tests_dir, ["test_plain.py"], {"test_gated.py": " "}) == [
        "native Harbor step exemption 'test_gated.py' has no written reason"
    ]
    assert _harbor_step_problems(
        tests_dir,
        ["test_plain.py"],
        {"test_gated.py": "probe", "test_absent.py": "probe"},
    ) == [
        "native Harbor step exemption 'test_absent.py' is stale: the file has no Harbor skip guard"
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('pytest.importorskip("harbor.analyze")', True, id="harbor-module"),
        pytest.param('pytest.importorskip("harbor")', True, id="harbor-package"),
        pytest.param('pytest.importorskip("harborx")', False, id="harbor-prefix"),
        pytest.param('pytest.importorskip("lancedb")', False, id="other-module"),
        pytest.param(
            'pytest.importorskip("inspect_scout", reason="Harbor joins are optional")',
            False,
            id="other-module-with-harbor-prose",
        ),
        pytest.param('pytest.skip("harbor is not installed")', True, id="skip-reason"),
        pytest.param(
            'pytest.mark.skipif(not PATH.is_dir(), reason="Harbor 0.24 tree absent")',
            True,
            id="skipif-reason",
        ),
        pytest.param('pytest.skip("unrelated")', False, id="unrelated-skip"),
    ],
)
def test_harbor_skip_detection_uses_module_and_named_reason(source: str, expected: bool) -> None:
    assert (_harbor_skip_evidence(source) is not None) is expected
