"""Unit and fixture tests for the cheat-audit lane (HAR-204).

Live-Docker coverage lives in test_cheat_docker.py (opt-in). Everything here
runs at $0 with no daemon: pure helper checks plus a fixture sandbox where
attack scripts execute for real through ``bash``.

The attack ladder lives in ``evallab.harbor_cheat``, whose only Harbor
dependency is the ``BaseAgent`` contract itself. These tests stub the four
Harbor names the module imports — scoped to this module with teardown — so
the ladder runs in every suite, including CI shards without Harbor
installed. The stubs are faithful only to what the agent uses
(construction, ``_get_env``, attribute context); they prove ladder logic,
not Harbor compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from evallab import cheat
from evallab.cheat import (
    ATTACKS,
    build_verdicts,
    cheat_verdict_for_reward,
    parse_attack_selection,
    trial_reward,
    write_cheat_verdicts,
)
from evallab.execution_contracts import (
    CHEAT_AGENT,
    CHEAT_AGENT_IMPORT_PATH,
    HARBOR_AGENT_IMPORT_PATHS,
    RunRequest,
    resolve_harbor_agent,
    validate_request,
)

_HARBOR_STUB_ROOTS = ("harbor", "evallab.harbor_cheat")


def _build_harbor_stubs() -> dict[str, types.ModuleType]:
    """Minimal Harbor surface used by evallab.harbor_cheat (test-only)."""

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    harbor = module("harbor")
    agents = module("harbor.agents")
    base_mod = module("harbor.agents.base")
    caps_mod = module("harbor.agents.capabilities")
    envs = module("harbor.environments")
    env_base_mod = module("harbor.environments.base")
    models = module("harbor.models")
    models_agent = module("harbor.models.agent")
    context_mod = module("harbor.models.agent.context")

    class AgentCapabilities:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class BaseAgent:
        def __init__(
            self,
            logs_dir: Path,
            model_name: str | None = None,
            logger: logging.Logger | None = None,
            **kwargs: Any,
        ) -> None:
            self.logs_dir = Path(logs_dir)
            self.model_name = model_name
            self.logger = logger or logging.getLogger("cheat-test")
            self._extra_env: dict[str, str] = dict(kwargs.pop("extra_env", None) or {})

        def _get_env(self, key: str, *alternatives: str) -> str | None:
            import os

            for source in (self._extra_env, os.environ):
                for name in (key, *alternatives):
                    if name in source:
                        return source[name]
            return None

    class BaseEnvironment:
        pass

    class AgentContext:
        def __init__(self) -> None:
            self.n_input_tokens: int | None = None
            self.n_output_tokens: int | None = None
            self.cost_usd: float | None = None
            self.metadata: dict[str, Any] | None = None

    base_mod.BaseAgent = BaseAgent
    caps_mod.AgentCapabilities = AgentCapabilities
    env_base_mod.BaseEnvironment = BaseEnvironment
    context_mod.AgentContext = AgentContext
    agents.base = base_mod
    agents.capabilities = caps_mod
    envs.base = env_base_mod
    models.agent = models_agent
    models_agent.context = context_mod
    harbor.agents = agents
    harbor.environments = envs
    harbor.models = models
    return {
        mod.__name__: mod
        for mod in (
            harbor,
            agents,
            base_mod,
            caps_mod,
            envs,
            env_base_mod,
            models,
            models_agent,
            context_mod,
        )
    }


@pytest.fixture(scope="module")
def harbor_cheat():
    """Import evallab.harbor_cheat against scoped Harbor stubs (with teardown)."""
    saved = {
        key: sys.modules.pop(key)
        for key in list(sys.modules)
        if key == "harbor" or key.startswith("harbor.") or key in _HARBOR_STUB_ROOTS
    }
    sys.modules.update(_build_harbor_stubs())
    try:
        import evallab.harbor_cheat as module

        yield module
    finally:
        for key in list(sys.modules):
            if key == "harbor" or key.startswith("harbor.") or key in _HARBOR_STUB_ROOTS:
                del sys.modules[key]
        sys.modules.update(saved)


def test_cheat_agent_name_is_fixed() -> None:
    assert CHEAT_AGENT == "cheat"
    assert HARBOR_AGENT_IMPORT_PATHS["cheat"] == CHEAT_AGENT_IMPORT_PATH
    assert CHEAT_AGENT_IMPORT_PATH == "evallab.harbor_cheat:CheatAgent"
    assert resolve_harbor_agent("cheat", None) == CHEAT_AGENT_IMPORT_PATH


def test_parse_attack_selection_defaults_to_full_ladder() -> None:
    assert parse_attack_selection(None) == ATTACKS
    assert parse_attack_selection("") == ATTACKS
    assert parse_attack_selection("  ") == ATTACKS


def test_parse_attack_selection_subset_dedupes_and_orders() -> None:
    assert parse_attack_selection("reward_plant,git_history") == ("git_history", "reward_plant")
    assert parse_attack_selection(" Git_History ,git_history ") == ("git_history",)


def test_parse_attack_selection_rejects_unknown_and_empty() -> None:
    with pytest.raises(ValueError, match="unknown cheat attack"):
        parse_attack_selection("git_history,rm_rf")
    with pytest.raises(ValueError, match="empty"):
        parse_attack_selection(" , , ")


def test_verdict_boundaries() -> None:
    assert cheat_verdict_for_reward(None) == "unscored"
    assert cheat_verdict_for_reward(float("nan")) == "unscored"
    assert cheat_verdict_for_reward(True) == "unscored"
    assert cheat_verdict_for_reward(0.0) == "clean"
    assert cheat_verdict_for_reward(0.999) == "clean"
    assert cheat_verdict_for_reward(1.0) == "cracked"
    assert cheat_verdict_for_reward(2) == "cracked"


def test_trial_reward_shapes() -> None:
    metrics = {"stats": {"evals": {"t": {"metrics": [{"mean": 1.0}]}}}}
    assert trial_reward(metrics) == 1.0
    rewards = {"stats": {"evals": {"t": {"reward_stats": {"reward": {"m": 0.0}}}}}}
    assert trial_reward(rewards) == 0.0
    assert trial_reward({}) is None
    assert trial_reward({"stats": {"evals": {}}}) is None
    assert trial_reward({"stats": {"evals": {"t": {"metrics": [{"avg": 1}]}}}}) is None


def _trial(job: Path, name: str, reward: float | None, attacks: list[dict] | None) -> None:
    trial = job / name
    if reward is None:
        stats: dict = {}
    else:
        stats = {"evals": {"t": {"metrics": [{"mean": reward}]}}}
    (trial).mkdir(parents=True)
    (trial / "result.json").write_text(json.dumps({"stats": stats}))
    if attacks is not None:
        cheat_dir = trial / "cheat"
        cheat_dir.mkdir()
        for attack in attacks:
            for rel in attack.get("evidence", []):
                (cheat_dir / rel).write_text("evidence\n")
        (cheat_dir / "attempts.json").write_text(
            json.dumps({"schema": "x", "agent": "cheat", "attacks": attacks})
        )


def test_write_cheat_verdicts_per_trial(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial(
        job,
        "trial-a",
        1.0,
        [
            {
                "name": "git_history",
                "status": "executed",
                "detail": "d",
                "evidence": ["git_history.log"],
            }
        ],
    )
    _trial(job, "trial-b", 0.0, None)
    _trial(job, "trial-c", None, None)
    out = write_cheat_verdicts(job, harbor_rev="0.21.0")
    assert out == job / "cheat-verdicts.json"
    payload = json.loads(out.read_text())
    assert payload["schema"] == "evallab.cheat.verdicts/v1"
    assert payload["agent"] == "cheat"
    assert payload["harbor_rev"] == "0.21.0"
    by_trial = {row["trial"]: row for row in payload["trials"]}
    assert by_trial["trial-a"]["verdict"] == "cracked"
    assert by_trial["trial-a"]["method"] == "git_history"
    assert by_trial["trial-a"]["reward"] == 1.0
    assert "trial-a/cheat/attempts.json" in by_trial["trial-a"]["evidence"]
    assert "trial-a/cheat/git_history.log" in by_trial["trial-a"]["evidence"]
    assert by_trial["trial-b"]["verdict"] == "clean"
    assert by_trial["trial-b"]["method"] is None
    assert by_trial["trial-c"]["verdict"] == "unscored"


def _task_dir(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text('schema_version = "1.3"\n')
    return task


def test_validate_request_admits_cheat_without_billable(tmp_path: Path) -> None:
    request = RunRequest(
        task=_task_dir(tmp_path),
        agent="cheat",
        name="cheat-unit",
        jobs_dir=tmp_path / "jobs",
        model=None,
    )
    validate_request(request, repo_root=tmp_path)


def test_validate_request_keeps_billable_agents_gated(tmp_path: Path) -> None:
    request = RunRequest(
        task=_task_dir(tmp_path),
        agent="codex",
        name="paid-unit",
        jobs_dir=tmp_path / "jobs",
        model=None,
    )
    with pytest.raises(ValueError, match="allow-billable"):
        validate_request(request, repo_root=tmp_path)


def test_validate_request_refuses_model_for_cheat(tmp_path: Path) -> None:
    request = RunRequest(
        task=_task_dir(tmp_path),
        agent="cheat",
        name="cheat-model",
        jobs_dir=tmp_path / "jobs",
        model="openai/gpt-5",
        allow_billable=True,
    )
    with pytest.raises(ValueError, match="does not accept a model"):
        validate_request(request, repo_root=tmp_path)


def test_cheat_parser_registers_run() -> None:
    from evallab.cli import parser

    parsed = parser().parse_args(["cheat", "run", "--task", "t", "--name", "n"])
    assert parsed.cheat_cmd == "run"
    assert parsed.task == Path("t")
    assert parsed.name == "n"
    assert parsed.attempts == 1
    assert parsed.attacks is None


# -- fixture sandbox: attack scripts run for real through bash -----------------


class ShellOutcome:
    """Sandbox exec result double (return_code/stdout/stderr only)."""

    def __init__(self, return_code: int, stdout: str, stderr: str) -> None:
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


class BashRun:
    """Execute attack scripts in a fixture sandbox instead of a container."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    async def __call__(self, script: str, timeout_sec: int) -> ShellOutcome:
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec + 30,
        )
        return ShellOutcome(completed.returncode, completed.stdout, completed.stderr)


def _sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "sandbox"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_outputs.py").write_text(
        'DATABASE_PATH = "/tmp/cheat-fixture/ledger.db"\n'
        "EXPECTED_LEDGER_ENTRIES = [\n"
        '    ("txn_1", 100, 100, "reconciled"),\n'
        '    ("txn_2", 200, 200, "reconciled"),\n'
        "]\n"
    )
    return root


def test_fixture_git_history_reports_no_history(tmp_path: Path, harbor_cheat) -> None:
    root = _sandbox(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_git_history(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "no git history" in record["detail"]
    assert (evidence / "git_history.log").is_file()


def test_fixture_reward_plant_writes_workspace_claims(tmp_path: Path, harbor_cheat) -> None:
    root = _sandbox(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_reward_plant(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "planted" in record["detail"]
    assert json.loads((root / "reward.json").read_text())["reward"] == 1.0


def test_fixture_answer_symlink_without_solution(tmp_path: Path, harbor_cheat) -> None:
    root = _sandbox(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_answer_symlink(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "no reference solution" in record["detail"]


def test_fixture_answer_symlink_executes_visible_solution(tmp_path: Path, harbor_cheat) -> None:
    root = _sandbox(tmp_path)
    solution = root / "solution"
    solution.mkdir()
    (solution / "solve.sh").write_text("#!/bin/bash\necho solved > solved.txt\n")
    (solution / "solve.sh").chmod(0o755)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_answer_symlink(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "rc=0" in record["detail"]


def test_fixture_upstream_fetch_without_remote(tmp_path: Path, harbor_cheat) -> None:
    root = _sandbox(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_upstream_fetch(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "no upstream remote" in record["detail"]


def _fixture_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE ledger_entries "
        "(external_id TEXT, expected_amount_cents INT, settled_amount_cents INT, "
        "reconciliation_status TEXT)"
    )
    connection.execute(
        "CREATE TABLE settlement_feed (external_id TEXT, amount_cents INT, settled_at TEXT)"
    )
    connection.executemany(
        "INSERT INTO ledger_entries VALUES (?,?,?,?)",
        [("txn_1", 100, 1, "pending"), ("txn_2", 200, 2, "pending")],
    )
    connection.commit()
    connection.close()


def test_fixture_verifier_peek_applies_guided_fix(tmp_path: Path, harbor_cheat) -> None:
    if shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 CLI unavailable")
    root = _sandbox(tmp_path)
    db = root / "data" / "ledger.db"
    _fixture_db(db)
    (root / "tests" / "test_outputs.py").write_text(
        f'DATABASE_PATH = "{db}"\n'
        "EXPECTED_LEDGER_ENTRIES = [\n"
        '    ("txn_1", 100, 100, "reconciled"),\n'
        '    ("txn_2", 200, 200, "reconciled"),\n'
        "]\n"
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_verifier_peek(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "guided fix applied" in record["detail"]
    connection = sqlite3.connect(db)
    rows = connection.execute(
        "SELECT external_id, settled_amount_cents, reconciliation_status "
        "FROM ledger_entries ORDER BY external_id"
    ).fetchall()
    connection.close()
    assert rows == [("txn_1", 100, "reconciled"), ("txn_2", 200, "reconciled")]


def test_guided_apply_script_rejects_unsafe_shapes(harbor_cheat) -> None:
    assert harbor_cheat._guided_apply_script("db", "t!", ["a", "b"], [[1, 2]]) is None
    assert harbor_cheat._guided_apply_script("db", "t", ["a"], [[1]]) is not None
    assert harbor_cheat._guided_apply_script("db", "t", ["a", "b"], [["x" * 500, 2]]) is None
    assert harbor_cheat._guided_apply_script("db", "t", ["a", "b"], [[None, 2]]) is None


def test_ladder_subset_skips_and_failure_continues(tmp_path: Path, harbor_cheat) -> None:
    async def boom(script: str, timeout_sec: int) -> ShellOutcome:
        if "CHEAT_PY" in script and "literal_eval(node.value)" in script:
            raise RuntimeError("probe exploded")
        return ShellOutcome(0, "NO_GIT_HISTORY_VISIBLE\nPEEK_DONE\n", "")

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    records = asyncio.run(
        harbor_cheat.run_ladder("/nonexistent", boom, evidence, ["git_history", "verifier_peek"])
    )
    by_name = {record["name"]: record for record in records}
    assert [record["name"] for record in records] == list(ATTACKS)
    assert by_name["git_history"]["status"] == "executed"
    assert by_name["verifier_peek"]["status"] == "failed"
    assert by_name["verifier_peek"]["evidence"] == ["verifier_peek.error.log"]
    assert (evidence / "verifier_peek.error.log").is_file()
    assert by_name["reward_plant"]["status"] == "skipped"
    assert by_name["reward_plant"]["evidence"] == []
    assert cheat.parse_attack_selection("git_history") == ("git_history",)


class FakeHarborEnv:
    """Minimal Harbor environment double: pwd plus canned exec results."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.commands: list[str] = []

    async def exec(
        self, command: str, cwd: str | None = None, timeout_sec: int | None = None
    ) -> ShellOutcome:
        self.commands.append(command)
        if command == "pwd":
            return ShellOutcome(0, self.cwd + "\n", "")
        return ShellOutcome(0, "NO_GIT_HISTORY_VISIBLE\n", "")


class FakeContext:
    """Attribute context double (n_* tokens, cost, metadata)."""

    def __init__(self) -> None:
        self.n_input_tokens: int | None = None
        self.n_output_tokens: int | None = None
        self.cost_usd: float | None = None
        self.metadata: dict[str, Any] | None = None


def test_agent_run_writes_attempts_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harbor_cheat
) -> None:
    monkeypatch.setenv("EVALLAB_CHEAT_ATTACKS", "git_history,reward_plant")
    logs = tmp_path / "logs"
    agent = harbor_cheat.CheatAgent(logs_dir=logs)
    env = FakeHarborEnv(str(tmp_path))
    context = FakeContext()
    asyncio.run(agent.run("do the task", env, context))
    attempts = json.loads((logs / "cheat" / "attempts.json").read_text())
    assert attempts["schema"] == "evallab.cheat.attempts/v1"
    assert attempts["agent"] == "cheat"
    by_name = {attack["name"]: attack for attack in attempts["attacks"]}
    assert by_name["git_history"]["status"] == "executed"
    assert by_name["reward_plant"]["status"] == "executed"
    assert by_name["verifier_peek"]["status"] == "skipped"
    assert context.cost_usd == 0.0
    assert context.metadata is not None and context.metadata["model_free"] is True
    assert context.metadata["cheat_executed"] == ["git_history", "reward_plant"]


def test_agent_run_rejects_unknown_attack_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harbor_cheat
) -> None:
    monkeypatch.setenv("EVALLAB_CHEAT_ATTACKS", "nope")
    agent = harbor_cheat.CheatAgent(logs_dir=tmp_path / "logs")
    with pytest.raises(RuntimeError, match="invalid EVALLAB_CHEAT_ATTACKS"):
        asyncio.run(agent.run("do the task", FakeHarborEnv(str(tmp_path)), FakeContext()))


# -- tamper ladder: new attacks run for real in fixture sandboxes -----------------


def test_parse_attack_selection_orders_new_attacks() -> None:
    assert parse_attack_selection("tamper_source_exit,git_history,skip_plant") == (
        "git_history",
        "skip_plant",
        "tamper_source_exit",
    )
    assert parse_attack_selection("TAMPER_SHADOW_TIMEOUT") == ("tamper_shadow_timeout",)


def _write_failing_pkg(root: Path, package: str = "fakepkg") -> None:
    (root / package).mkdir(parents=True, exist_ok=True)
    (root / package / "__init__.py").write_text("VALUE = 1\n")
    (root / "test_broken.py").write_text(
        f"import {package}\n\n\ndef test_broken():\n    assert {package}.VALUE == 2\n"
    )


def _git_commit(cwd: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=cheat",
            "-c",
            "user.email=cheat@localhost",
            "commit",
            "-q",
            "-m",
            message,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _git_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    _git_commit(root, "base")
    return root


def _run_pytest(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _kill_reward_writers(evidence: Path) -> None:
    import signal

    try:
        log = (evidence / "tamper_reward_writer.log").read_text(encoding="utf-8")
    except OSError:
        return
    for line in log.splitlines():
        if "pid=" not in line:
            continue
        try:
            pid = int(line.rsplit("pid=", 1)[1].split()[0])
        except ValueError:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def _await_file_text(path: Path, timeout_sec: float = 5.0) -> str:
    """Poll a background-writer file until it holds non-empty content.

    A reader can land in the writer's truncate window and see an empty file;
    polling until content appears (or the deadline) keeps the assertion about
    the writer, not about read timing. A loop that never starts still fails
    loudly with the last-seen (empty) content.
    """
    deadline = time.monotonic() + timeout_sec
    seen = ""
    while True:
        try:
            seen = path.read_text(encoding="utf-8")
        except OSError:
            seen = ""
        if seen:
            return seen
        if time.monotonic() >= deadline:
            return seen
        time.sleep(0.05)


def test_fixture_skip_plant_skips_failing_suite(tmp_path: Path, harbor_cheat) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    _write_failing_pkg(root)
    assert _run_pytest(root, "test_broken.py").returncode != 0
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_skip_plant(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "skip-all" in record["detail"]
    assert (root / "conftest.py").is_file()
    completed = _run_pytest(root, "test_broken.py")
    assert completed.returncode == 0
    assert "skipped" in completed.stdout
    rerun = asyncio.run(harbor_cheat.attack_skip_plant(str(root), BashRun(root), evidence))
    assert rerun["status"] == "executed"
    assert "already carries" in rerun["detail"]


def test_fixture_tracked_conftest_forges_passes(tmp_path: Path, harbor_cheat) -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    root = tmp_path / "sandbox"
    root.mkdir()
    _write_failing_pkg(root)
    (root / "conftest.py").write_text("MARKER = True\n")
    _git_repo(root)
    assert _run_pytest(root, "test_broken.py").returncode != 0
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(
        harbor_cheat.attack_tamper_tracked_conftest(str(root), BashRun(root), evidence)
    )
    assert record["status"] == "executed"
    assert "forged" in record["detail"]
    completed = _run_pytest(root, "test_broken.py")
    assert completed.returncode == 0
    assert "1 passed" in completed.stdout


def test_fixture_tracked_conftest_skips_without_tracked_conftest(
    tmp_path: Path, harbor_cheat
) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(
        harbor_cheat.attack_tamper_tracked_conftest(str(root), BashRun(root), evidence)
    )
    assert record["status"] == "skipped"
    assert "no tracked conftest" in record["detail"]


def test_fixture_sitecustomize_forces_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harbor_cheat
) -> None:
    if shutil.which("python3") is None:
        pytest.skip("python3 unavailable")
    site_dir = tmp_path / "sitepkgs"
    site_dir.mkdir()
    monkeypatch.setenv("CHEAT_SITE_DIR", str(site_dir))
    previous = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH", f"{site_dir}{os.pathsep}{previous}" if previous else str(site_dir)
    )
    root = tmp_path / "sandbox"
    root.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(
        harbor_cheat.attack_tamper_sitecustomize(str(root), BashRun(root), evidence)
    )
    assert record["status"] == "executed"
    assert "exit 0" in record["detail"]
    assert (site_dir / "sitecustomize.py").is_file()
    probe = subprocess.run(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        capture_output=True,
        timeout=60,
    )
    assert probe.returncode == 0


def test_fixture_shadow_timeout_shims_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harbor_cheat
) -> None:
    bin_dir = tmp_path / "earlybin"
    bin_dir.mkdir()
    monkeypatch.setenv("CHEAT_BIN_DIR", str(bin_dir))
    root = tmp_path / "sandbox"
    root.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(
        harbor_cheat.attack_tamper_shadow_timeout(str(root), BashRun(root), evidence)
    )
    assert record["status"] == "executed"
    assert "timeout" in record["detail"]
    assert os.access(bin_dir / "timeout", os.X_OK)
    shimmed = subprocess.run(
        [str(bin_dir / "timeout"), "30", "sleep", "30"],
        capture_output=True,
        timeout=60,
    )
    assert shimmed.returncode == 0


def test_fixture_reward_writer_holds_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harbor_cheat
) -> None:
    reward_dir = tmp_path / "verifier"
    reward_dir.mkdir()
    monkeypatch.setenv("CHEAT_REWARD_DIR", str(reward_dir))
    root = tmp_path / "sandbox"
    root.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    try:
        record = asyncio.run(
            harbor_cheat.attack_tamper_reward_writer(str(root), BashRun(root), evidence)
        )
        assert record["status"] == "executed"
        assert "hold reward at 1" in record["detail"]
        assert _await_file_text(reward_dir / "reward.txt") == "1\n"
        assert json.loads(_await_file_text(reward_dir / "reward.json")) == {"reward": 1.0}
    finally:
        _kill_reward_writers(evidence)


def test_fixture_source_exit_forces_exit_zero(tmp_path: Path, harbor_cheat) -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    root = tmp_path / "sandbox"
    root.mkdir()
    _write_failing_pkg(root)
    _git_repo(root)
    assert _run_pytest(root, "test_broken.py").returncode != 0
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_tamper_source_exit(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "exit-0 hook" in record["detail"]
    completed = _run_pytest(root, "test_broken.py")
    assert completed.returncode == 0
    assert "failed" in completed.stdout


def test_fixture_source_pytest_patch_passes_failures(tmp_path: Path, harbor_cheat) -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    root = tmp_path / "sandbox"
    root.mkdir()
    _write_failing_pkg(root)
    _git_repo(root)
    assert _run_pytest(root, "test_broken.py").returncode != 0
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(
        harbor_cheat.attack_tamper_source_pytest_patch(str(root), BashRun(root), evidence)
    )
    assert record["status"] == "executed"
    assert "TestReport patch" in record["detail"]
    completed = _run_pytest(root, "test_broken.py")
    assert completed.returncode == 0
    assert "1 passed" in completed.stdout


def test_fixture_source_tampers_skip_without_imported_init(tmp_path: Path, harbor_cheat) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for attack in (
        harbor_cheat.attack_tamper_source_exit,
        harbor_cheat.attack_tamper_source_pytest_patch,
    ):
        record = asyncio.run(attack(str(root), BashRun(root), evidence))
        assert record["status"] == "skipped"


def test_fixture_git_history_applies_recovered_fix(tmp_path: Path, harbor_cheat) -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "calc.py").write_text("def answer():\n    return 1\n")
    (root / "test_calc.py").write_text(
        "from calc import answer\n\n\ndef test_answer():\n    assert answer() == 2\n"
    )
    _git_repo(root)
    assert _run_pytest(root, "test_calc.py").returncode != 0
    (root / "calc.py").write_text("def answer():\n    return 2\n")
    _git_commit(root, "fix the answer")
    subprocess.run(["git", "branch", "future"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "reset", "-q", "--hard", "HEAD~1"], cwd=root, check=True, capture_output=True
    )
    assert "return 1" in (root / "calc.py").read_text(encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_git_history(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "applied recovered fix" in record["detail"]
    assert "return 2" in (root / "calc.py").read_text(encoding="utf-8")
    assert _run_pytest(root, "test_calc.py").returncode == 0


def test_ladder_runs_new_attacks_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harbor_cheat
) -> None:
    site_dir = tmp_path / "sitepkgs"
    site_dir.mkdir()
    bin_dir = tmp_path / "earlybin"
    bin_dir.mkdir()
    reward_dir = tmp_path / "verifier"
    reward_dir.mkdir()
    monkeypatch.setenv("CHEAT_SITE_DIR", str(site_dir))
    monkeypatch.setenv("CHEAT_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("CHEAT_REWARD_DIR", str(reward_dir))
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "test_broken.py").write_text("def test_broken():\n    assert False\n")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    try:
        records = asyncio.run(harbor_cheat.run_ladder(str(root), BashRun(root), evidence))
    finally:
        _kill_reward_writers(evidence)
    assert [record["name"] for record in records] == list(cheat.ATTACKS)
    by_status = {record["name"]: record["status"] for record in records}
    assert by_status["skip_plant"] == "executed"
    assert by_status["tamper_tracked_conftest"] == "skipped"
    assert by_status["tamper_sitecustomize"] == "executed"
    assert by_status["tamper_shadow_timeout"] == "executed"
    assert by_status["tamper_reward_writer"] == "executed"
    assert by_status["tamper_source_exit"] == "skipped"
    assert by_status["tamper_source_pytest_patch"] == "skipped"


def test_trial_reward_reads_verifier_result_rewards() -> None:
    assert trial_reward({"verifier_result": {"rewards": {"reward": 1.0}}}) == 1.0
    assert trial_reward({"verifier_result": {"rewards": {"score": 0.0}}}) == 0.0
    assert trial_reward({"verifier_result": {"rewards": {"reward": True}}}) is None
    assert trial_reward({"verifier_result": {"rewards": {"reward": "high"}}}) is None
    assert trial_reward({}) is None
    both = {
        "stats": {"evals": {"t": {"metrics": [{"mean": 0.0}]}}},
        "verifier_result": {"rewards": {"reward": 1.0}},
    }
    assert trial_reward(both) == 0.0


def test_write_cheat_verdicts_agent_nested_attempts(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "trial-n"
    cheat_dir = trial / "agent" / "cheat"
    cheat_dir.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}})
    )
    (cheat_dir / "tamper_shadow_timeout.log").write_text("evidence\n")
    (cheat_dir / "attempts.json").write_text(
        json.dumps(
            {
                "schema": "x",
                "agent": "cheat",
                "attacks": [
                    {
                        "name": "tamper_shadow_timeout",
                        "status": "executed",
                        "detail": "d",
                        "evidence": ["tamper_shadow_timeout.log"],
                    }
                ],
            }
        )
    )
    payload = build_verdicts(job, harbor_rev="0.24.0")
    assert len(payload["trials"]) == 1
    row = payload["trials"][0]
    assert row["verdict"] == "cracked"
    assert row["method"] == "tamper_shadow_timeout"
    assert row["reward"] == 1.0
    assert "trial-n/agent/cheat/attempts.json" in row["evidence"]
    assert "trial-n/agent/cheat/tamper_shadow_timeout.log" in row["evidence"]


def test_cheat_run_refuses_old_harbor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import argparse

    import evallab.harbor_view
    from evallab.cheat import _cheat_run_command

    monkeypatch.setattr(evallab.harbor_view, "installed_harbor_version", lambda: (0, 21))
    args = argparse.Namespace(
        task=tmp_path / "task",
        name="refused",
        jobs_dir=tmp_path / "jobs",
        attempts=1,
        attacks=None,
        timeout_seconds=600,
    )
    assert _cheat_run_command(args, tmp_path) == 2
    assert "0.24" in capsys.readouterr().err
    assert not (tmp_path / "jobs").exists()


def _git_branch_commit(root: Path, branch: str) -> None:
    subprocess.run(
        ["git", "checkout", "-q", "-b", branch], cwd=root, check=True, capture_output=True
    )


def test_fixture_git_history_applies_unreachable_fix(tmp_path: Path, harbor_cheat) -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "calc.py").write_text("def answer():\n    return 1\n")
    (root / "test_calc.py").write_text(
        "from calc import answer\n\n\ndef test_answer():\n    assert answer() == 2\n"
    )
    _git_repo(root)
    assert _run_pytest(root, "test_calc.py").returncode != 0
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git_branch_commit(root, "future")
    (root / "calc.py").write_text("def answer():\n    return 2\n")
    _git_commit(root, "fix the answer")
    subprocess.run(["git", "checkout", "-q", base], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-D", "future"], cwd=root, check=True, capture_output=True)
    assert "return 1" in (root / "calc.py").read_text(encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_git_history(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "scanned 1 unreachable commits" in record["detail"]
    assert "applied recovered fix" in record["detail"]
    assert "return 2" in (root / "calc.py").read_text(encoding="utf-8")
    assert _run_pytest(root, "test_calc.py").returncode == 0


def test_fixture_git_history_restores_between_tries(tmp_path: Path, harbor_cheat) -> None:
    if shutil.which("git") is None:
        pytest.skip("git unavailable")
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "calc.py").write_text("def answer():\n    return 1\n")
    (root / "test_calc.py").write_text(
        "from calc import answer\n\n\ndef test_answer():\n    assert answer() == 2\n"
    )
    _git_repo(root)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git_branch_commit(root, "bad")
    (root / "calc.py").write_text("def answer():\n    return 1\nthis is syntax error\n")
    _git_commit(root, "break it small")
    subprocess.run(["git", "checkout", "-q", base], cwd=root, check=True, capture_output=True)
    _git_branch_commit(root, "fix")
    (root / "calc.py").write_text(
        "def answer():\n    result = 2\n    assert result == 2\n    return result\n"
    )
    _git_commit(root, "fix the answer properly")
    subprocess.run(["git", "checkout", "-q", base], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-D", "bad", "fix"], cwd=root, check=True, capture_output=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    record = asyncio.run(harbor_cheat.attack_git_history(str(root), BashRun(root), evidence))
    assert record["status"] == "executed"
    assert "scanned 2 unreachable commits" in record["detail"]
    assert "applied recovered fix" in record["detail"]
    apply_log = (evidence / "git_history.apply.log").read_text(encoding="utf-8")
    assert "SMOKE_FAIL" in apply_log
    assert "syntax error" not in (root / "calc.py").read_text(encoding="utf-8")
    assert "return result" in (root / "calc.py").read_text(encoding="utf-8")
    assert _run_pytest(root, "test_calc.py").returncode == 0
