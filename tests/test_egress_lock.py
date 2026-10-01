"""Mandatory Daytona egress lock for MiMo runs (HAR-140).

Behavior: the default on (MiMo model or MiMo task on Daytona, controls
included), each dispatch refusal, the treatment-key split, the lock-failure
infra classification, and the nop lock path (a model-free agent issues no
exec, so the verifier's first command takes the lock).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# --- Harbor stubs (the lab env has no `harbor` package installed) ------------


def _install_harbor_stubs() -> None:
    if "harbor.environments.daytona.environment" in sys.modules:
        return
    harbor = types.ModuleType("harbor")
    envs = types.ModuleType("harbor.environments")
    daytona_pkg = types.ModuleType("harbor.environments.daytona")
    daytona_mod = types.ModuleType("harbor.environments.daytona.environment")
    models = types.ModuleType("harbor.models")
    task_pkg = types.ModuleType("harbor.models.task")
    config_mod = types.ModuleType("harbor.models.task.config")

    class NetworkMode:
        PUBLIC = "public"
        ALLOWLIST = "allowlist"

    class DaytonaEnvironment:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._compose_mode = False
            self._network_policy = object()
            self._phase_network_policies: list[Any] = []
            self._sandbox: Any = None
            self.logger = logging.getLogger("test-daytona")
            self.trial_paths = SimpleNamespace(trial_dir=Path("."))
            self.session_id = "sess"
            self.environment_name = "env"

        @contextlib.contextmanager
        def scoped_exec_env(self, env: dict[str, str]):  # type: ignore[no-untyped-def]
            yield

        async def exec(self, *args: Any, **kwargs: Any) -> Any:
            return ("base-exec", args, kwargs)

        async def _create_sandbox(self, params: Any, daytona: Any = None) -> None:
            self._sandbox = SimpleNamespace(id="sbx-test", network_block_all=None)

        async def _apply_network_policy(self, network_policy: Any) -> None:
            return None

        async def stop(self, delete: bool) -> None:
            self.stopped = delete

    class _DaytonaDinD:
        def __init__(self, env: Any) -> None:
            self._env = env

    daytona_mod.DaytonaEnvironment = DaytonaEnvironment  # type: ignore[attr-defined]
    daytona_mod._DaytonaDinD = _DaytonaDinD  # type: ignore[attr-defined]
    config_mod.NetworkMode = NetworkMode  # type: ignore[attr-defined]
    harbor.environments = envs  # type: ignore[attr-defined]
    envs.daytona = daytona_pkg  # type: ignore[attr-defined]
    daytona_pkg.environment = daytona_mod  # type: ignore[attr-defined]
    harbor.models = models  # type: ignore[attr-defined]
    models.task = task_pkg  # type: ignore[attr-defined]
    task_pkg.config = config_mod  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": envs,
            "harbor.environments.daytona": daytona_pkg,
            "harbor.environments.daytona.environment": daytona_mod,
            "harbor.models": models,
            "harbor.models.task": task_pkg,
            "harbor.models.task.config": config_mod,
        }
    )


_install_harbor_stubs()

from evallab.counts import classify_counts  # noqa: E402
from evallab.database import AGENT_STOP_EXCEPTIONS  # noqa: E402
from evallab.execution_contracts import (  # noqa: E402
    MIMO_SELFHOSTED_MODEL_SELECTOR,
    TERMINUS_LOCAL_MODEL_SELECTOR,
    RunRequest,
    build_command,
    is_mimo_dataset_task,
    is_mimo_family_model,
    is_mimo_run,
    resolve_egress_lock,
    validate_request,
)
from evallab.harbor_daytona import BoundedDaytonaEnvironment  # noqa: E402
from evallab.trial_treatment import _egress_lock_value, collect_treatment  # noqa: E402

MIMO_TASK_TOML = """\
[task]
name = "mimo-v2.6-rl/format-code-task-000001"

[metadata]
source_dataset = "XiaomiMiMo/MiMo-V2.6-RL-oss"

[agent]
timeout_sec = 900.0
"""

PLAIN_TASK_TOML = """\
[task]
name = "terminal-bench/html-js-filter"

[agent]
timeout_sec = 900.0
"""

CEILINGS: dict[str, Any] = {
    "max_requests": 2000,
    "max_input_tokens": 64_000_000,
    "max_output_tokens": 1_000_000,
    "max_total_tokens": 65_000_000,
    "cost_limit_usd": 1.0,
}


def _task(tmp_path: Path, *, mimo: bool, compose: bool = False, phase: bool = False) -> Path:
    task = tmp_path / ("mimo-task" if mimo else "plain-task")
    task.mkdir(parents=True, exist_ok=True)
    text = MIMO_TASK_TOML if mimo else PLAIN_TASK_TOML
    if phase:
        text += '\n[verifier]\nnetwork_mode = "no-network"\n'
    (task / "task.toml").write_text(text, encoding="utf-8")
    if compose:
        env = task / "environment"
        env.mkdir(parents=True, exist_ok=True)
        (env / "docker-compose.yaml").write_text("services:\n  main: {}\n", encoding="utf-8")
    return task


def _request(task: Path, tmp_path: Path, **overrides: Any) -> RunRequest:
    base: dict[str, Any] = {
        "task": task,
        "agent": "terminus-2",
        "model": MIMO_SELFHOSTED_MODEL_SELECTOR,
        "name": "har140-test-run",
        "jobs_dir": tmp_path / "runs",
        "environment": "daytona",
        "allow_billable": True,
    }
    base.update(overrides)
    agent = base.get("agent")
    model = base.get("model")
    metered = agent in {"mini-swe-agent", "zai-opencode"} or (
        agent == "terminus-2" and model is not None and model != TERMINUS_LOCAL_MODEL_SELECTOR
    )
    if metered:
        for key, value in CEILINGS.items():
            base.setdefault(key, value)
    else:
        for key in CEILINGS:
            base.pop(key, None)
    if agent in {"nop", "oracle"}:
        base.pop("allow_billable", None)
    return RunRequest(**base)  # type: ignore[arg-type]


# --- default on ---------------------------------------------------------------


def test_mimo_model_daytona_defaults_locked(tmp_path: Path) -> None:
    request = _request(_task(tmp_path, mimo=False), tmp_path)
    assert is_mimo_family_model(request.model)
    assert is_mimo_run(request.task, request.model)
    assert resolve_egress_lock(request) is True
    validate_request(request)
    assert "--environment-kwarg" in build_command(request)
    assert "egress_lock=true" in build_command(request)


def test_non_mimo_and_non_daytona_default_unlocked(tmp_path: Path) -> None:
    task = _task(tmp_path, mimo=False)
    other = _request(task, tmp_path, agent="terminus-2", model="zai/glm-5.3-flash")
    assert not is_mimo_family_model(other.model)
    assert resolve_egress_lock(other) is False
    assert "egress_lock=true" not in build_command(other)
    docker = _request(task, tmp_path, environment="docker")
    assert resolve_egress_lock(docker) is False


def test_nop_on_mimo_task_defaults_locked_without_a_model(tmp_path: Path) -> None:
    task = _task(tmp_path, mimo=True)
    assert is_mimo_dataset_task(task)
    nop = _request(task, tmp_path, agent="nop", model=None)
    assert is_mimo_run(nop.task, nop.model)
    assert resolve_egress_lock(nop) is True
    validate_request(nop)
    assert "egress_lock=true" in build_command(nop)


def test_nop_on_plain_task_stays_unlocked(tmp_path: Path) -> None:
    nop = _request(_task(tmp_path, mimo=False), tmp_path, agent="nop", model=None)
    assert resolve_egress_lock(nop) is False
    assert "egress_lock=true" not in build_command(nop)


def test_explicit_true_wins_on_plain_task(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=False), tmp_path, agent="nop", model=None, egress_lock=True
    )
    assert resolve_egress_lock(request) is True
    assert "egress_lock=true" in build_command(request)


# --- refusals ------------------------------------------------------------------


def test_explicit_false_on_mimo_is_refused(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=True), tmp_path, agent="nop", model=None, egress_lock=False
    )
    with pytest.raises(ValueError, match="explicit egress_lock=false"):
        validate_request(request)


def test_compose_task_is_refused(tmp_path: Path) -> None:
    request = _request(_task(tmp_path, mimo=True, compose=True), tmp_path, agent="nop", model=None)
    with pytest.raises(ValueError, match="single-container"):
        validate_request(request)


def test_phase_network_policy_is_refused(tmp_path: Path) -> None:
    request = _request(_task(tmp_path, mimo=True, phase=True), tmp_path, agent="nop", model=None)
    with pytest.raises(ValueError, match="phase network policies"):
        validate_request(request)


def test_installed_local_model_is_refused(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=True),
        tmp_path,
        agent="terminus-2",
        model=TERMINUS_LOCAL_MODEL_SELECTOR,
    )
    with pytest.raises(ValueError, match="inside the sandbox"):
        validate_request(request)


def test_in_sandbox_agent_is_refused(tmp_path: Path) -> None:
    request = _request(
        _task(tmp_path, mimo=True), tmp_path, agent="codex", model=None, allow_billable=True
    )
    with pytest.raises(ValueError, match="inside the sandbox"):
        validate_request(request)


# --- treatment key -------------------------------------------------------------


def _trial(
    root: Path,
    name: str,
    *,
    lock: dict[str, Any] | None,
    legacy: bool = False,
) -> tuple[Path, Path]:
    job = root / name
    trial = job / f"{name}__abc"
    trial.mkdir(parents=True, exist_ok=True)
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {
                "repository": {},
                "task_staging": {"source_package_digest": "sha256:task-a"},
                "tools": {"harbor": "0.21.0"},
            }
        ),
        encoding="utf-8",
    )
    (job / "experiment-spec.json").write_text(
        json.dumps({"timeout_seconds": 900}), encoding="utf-8"
    )
    (trial / "config.json").write_text(
        json.dumps(
            {
                "agent": {"name": "nop", "model_name": None, "kwargs": {}},
                "environment": {"import_path": "evallab.harbor_daytona:BoundedDaytonaEnvironment"},
            }
        ),
        encoding="utf-8",
    )
    (trial / "result.json").write_text(
        json.dumps({"agent_info": {"name": "nop"}}), encoding="utf-8"
    )
    if lock is not None:
        (trial / "egress-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    elif legacy:
        (trial / "egress-lock.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "locked_at": "2026-09-30T00:00:00+00:00",
                    "sandbox_id": "sbx-legacy",
                    "network_block_all": True,
                    "mechanism": "daytona update_network_settings(network_block_all=True)",
                }
            ),
            encoding="utf-8",
        )
    return job, trial


def test_treatment_key_differs_by_egress_lock(tmp_path: Path) -> None:
    from evallab.trial_treatment import CommitSources

    sources = CommitSources(tmp_path / "repo")
    locked = {
        "schema_version": 2,
        "requested": True,
        "applied": True,
        "locked_at": "2026-10-01T00:00:00+00:00",
        "recorded_at": "2026-10-01T00:00:00+00:00",
        "sandbox_id": "sbx-1",
        "network_block_all": True,
        "mechanism": "daytona update_network_settings(network_block_all=True)",
        "error": None,
    }
    open_: dict[str, Any] = {
        "schema_version": 2,
        "requested": False,
        "applied": False,
        "locked_at": None,
        "recorded_at": "2026-10-01T00:00:00+00:00",
        "sandbox_id": "sbx-2",
        "network_block_all": False,
        "mechanism": None,
        "error": None,
    }
    job_a, trial_a = _trial(tmp_path / "runs", "locked", lock=locked)
    job_b, trial_b = _trial(tmp_path / "runs", "open", lock=open_)
    job_c, trial_c = _trial(tmp_path / "runs", "legacy", lock=None, legacy=True)
    job_d, trial_d = _trial(tmp_path / "runs", "missing", lock=None)
    row_locked = collect_treatment(job_a, trial_a, sources)
    row_open = collect_treatment(job_b, trial_b, sources)
    row_legacy = collect_treatment(job_c, trial_c, sources)
    row_missing = collect_treatment(job_d, trial_d, sources)
    assert row_locked["egress_lock"] is True
    assert row_open["egress_lock"] is False
    assert row_legacy["egress_lock"] is True
    assert row_missing["egress_lock"] is False
    assert row_locked["treatment_key"] != row_open["treatment_key"]
    assert row_locked["setup_key"] != row_open["setup_key"]
    value, _ = _egress_lock_value(trial_a)
    assert value is True


# --- lock failure is infra -------------------------------------------------------


def _env(trial_dir: Path, *, locked: bool) -> BoundedDaytonaEnvironment:
    env = BoundedDaytonaEnvironment.__new__(BoundedDaytonaEnvironment)
    env._trial_ttl_minutes = 60
    env._egress_lock = locked
    env._egress_scope_depth = 0
    env._egress_lock_due = False
    env._egress_locked = False
    env._egress_record_written = False
    env._egress_lock_guard = asyncio.Lock()
    env._compose_mode = False
    env._network_policy = object()
    env._phase_network_policies = []
    env._sandbox = None
    env.logger = logging.getLogger("test-egress")
    env.trial_paths = SimpleNamespace(trial_dir=trial_dir)
    env.session_id = "sess"
    env.environment_name = "env"
    return env  # type: ignore[return-value]


def test_lock_failure_is_infra_and_leaves_a_record(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    env = _env(trial_dir, locked=True)

    class FailingSandbox:
        id = "sbx-fail"
        network_block_all = False

        async def update_network_settings(self, **kwargs: Any) -> None:
            raise RuntimeError("Daytona refused the lock")

    env._sandbox = FailingSandbox()
    with pytest.raises(RuntimeError, match="Daytona refused"):
        asyncio.run(env._lock_egress())
    assert "RuntimeError" not in AGENT_STOP_EXCEPTIONS
    record = json.loads((trial_dir / "egress-lock.json").read_text(encoding="utf-8"))
    assert record["requested"] is True
    assert record["applied"] is False
    assert "Daytona refused" in (record["error"] or "")
    counts = classify_counts(
        reward=None,
        scored=False,
        exception={"exception_type": "RuntimeError", "exception_message": "Daytona refused"},
    )
    assert counts["verdict"] == "excluded"
    assert "infra" in counts["reasons"]


def test_nop_locks_on_the_verifier_first_exec(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    env = _env(trial_dir, locked=True)

    class LockingSandbox:
        id = "sbx-nop"
        network_block_all = False

        async def update_network_settings(self, **kwargs: Any) -> None:
            assert kwargs.get("network_block_all") is True
            self.network_block_all = True

    env._sandbox = LockingSandbox()
    # Agent setup runs inside the first top-level scope; a nop agent then
    # issues no exec of its own.
    with env.scoped_exec_env({}):
        pass
    assert env._egress_lock_due is True
    assert env._egress_locked is False
    outcome = asyncio.run(env.exec("verifier", "run"))
    assert outcome[0] == "base-exec"
    assert env._egress_locked is True
    record = json.loads((trial_dir / "egress-lock.json").read_text(encoding="utf-8"))
    assert record["requested"] is True
    assert record["applied"] is True
    assert record["network_block_all"] is True
