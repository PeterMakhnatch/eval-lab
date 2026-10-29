"""Mandatory Daytona egress lock for MiMo runs (HAR-140).

Behavior: the default on (MiMo model or MiMo task on Daytona, controls
included), each dispatch refusal, the treatment-key split, the lock-failure
infra classification, and the nop lock path (a model-free agent issues no
exec, so the verifier's first command takes the lock).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import importlib
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
    try:
        importlib.import_module("harbor.environments.daytona.environment")
        return
    except ModuleNotFoundError as error:
        if error.name != "harbor" and not (error.name or "").startswith("harbor."):
            raise
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

from evallab import egress_verification  # noqa: E402
from evallab.counts import classify_counts  # noqa: E402
from evallab.database import AGENT_STOP_EXCEPTIONS  # noqa: E402
from evallab.execution_contracts import (  # noqa: E402
    LOCKED_MODAL_ENVIRONMENT_IMPORT_PATH,
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


@pytest.fixture(autouse=True)
def isolate_native_daytona_transport(monkeypatch):
    """Keep native SDK availability from turning these boundary tests into I/O."""
    base = BoundedDaytonaEnvironment.__bases__[0]

    @contextlib.contextmanager
    def scope(self, env):
        yield

    async def execute(self, *args, **kwargs):
        return ("base-exec", args, kwargs)

    async def stop(self, delete):
        self.stopped = delete

    monkeypatch.setattr(base, "scoped_exec_env", scope)
    monkeypatch.setattr(base, "exec", execute)
    monkeypatch.setattr(base, "stop", stop)


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
    metered = agent in {"mini-swe-agent", "mimoagent", "zai-opencode"} or (
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


def test_openrouter_mimo_locks_on_mimo_task_and_stays_unlocked_on_non_mimo_task(
    tmp_path: Path,
) -> None:
    model = "openrouter-metered/xiaomi/mimo-v2.6-flash"
    non_mimo = _request(_task(tmp_path, mimo=False), tmp_path, model=model)
    assert is_mimo_family_model(non_mimo.model)
    assert not is_mimo_run(non_mimo.task, non_mimo.model)
    assert resolve_egress_lock(non_mimo) is False
    validate_request(non_mimo)

    mimo_task = _task(tmp_path, mimo=True)
    on_mimo = _request(mimo_task, tmp_path, model=model)
    assert is_mimo_run(on_mimo.task, on_mimo.model)
    assert resolve_egress_lock(on_mimo) is True
    validate_request(on_mimo)
    with pytest.raises(ValueError, match="explicit egress_lock=false"):
        validate_request(_request(mimo_task, tmp_path, model=model, egress_lock=False))


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


@pytest.mark.parametrize(
    ("agent", "model"),
    [
        ("codex", None),
        ("claude-code", None),
        ("terminus-2", TERMINUS_LOCAL_MODEL_SELECTOR),
    ],
)
def test_no_network_task_does_not_admit_in_container_model_agents(
    tmp_path: Path, agent: str, model: str | None
) -> None:
    task = tmp_path / "in-container-model"
    task.mkdir()
    (task / "task.toml").write_text('[agent]\nnetwork_mode = "no-network"\n', encoding="utf-8")
    request = _request(
        task, tmp_path, environment="docker", agent=agent, model=model, egress_lock=True
    )

    with pytest.raises(ValueError, match="sandbox"):
        validate_request(request)


@pytest.mark.parametrize(
    ("agent", "phase"),
    [
        ("terminus-2", '[agent]\nnetwork_mode = "no-network"\n'),
        (
            "mimoagent",
            '[agent]\nnetwork_mode = "no-network"\n[verifier]\nnetwork_mode = "no-network"\n',
        ),
        ("oracle", '[agent]\nnetwork_mode = "no-network"\n'),
        ("nop", '[verifier]\nnetwork_mode = "no-network"\n'),
    ],
)
def test_locked_docker_admits_no_network_phases_for_host_loops(
    tmp_path: Path, agent: str, phase: str
) -> None:
    task = tmp_path / "airgapped-task"
    task.mkdir()
    (task / "task.toml").write_text(phase, encoding="utf-8")
    request = _request(
        task,
        tmp_path,
        environment="docker",
        agent=agent,
        model=MIMO_SELFHOSTED_MODEL_SELECTOR if agent not in {"oracle", "nop"} else None,
        egress_lock=True,
    )

    validate_request(request)
    assert resolve_egress_lock(request) is True


@pytest.mark.parametrize("phase", ["agent", "verifier"])
@pytest.mark.parametrize("mode", ['"public"', '"allowlist"', "false"])
def test_locked_docker_refuses_phase_network_grants(tmp_path: Path, phase: str, mode: str) -> None:
    task = tmp_path / "network-grant"
    task.mkdir()
    (task / "task.toml").write_text(f"[{phase}]\nnetwork_mode = {mode}\n", encoding="utf-8")
    request = _request(
        task, tmp_path, environment="docker", agent="nop", model=None, egress_lock=True
    )

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
    with pytest.raises(ValueError):
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
    env._egress_failure = None
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
    env._probe_exec = _network(blocked=True)
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
    assert record["verified"] is True


# --- post-lock verification from inside the sandbox (HAR-205) -------------------

_NO_ANSWER = SimpleNamespace(return_code=2, stdout="", stderr="")


def _result(code: int | None, out: str = "", err: str = "") -> Any:
    return SimpleNamespace(return_code=code, stdout=out, stderr=err)


def _network(
    *,
    blocked: bool = True,
    leaks: tuple[str, ...] = (),
    broken: dict[str, Any] | None = None,
    calls: list[str] | None = None,
) -> Any:
    """A fake in-sandbox shell keyed on the probe command, never a real socket.

    ``leaks`` names probes (or name prefixes such as ``tcp6``) that get
    through; ``broken`` maps a probe name to the exception to raise, the
    coroutine function to await, or the result to return in place of the
    answer.
    """

    by_command = {probe.command: probe.name for probe in egress_verification.PROBES}

    async def execute(command: str, timeout_sec: int) -> Any:
        name = by_command[command]
        if calls is not None:
            calls.append(name)
        replacement = (broken or {}).get(name)
        if isinstance(replacement, BaseException):
            raise replacement
        if callable(replacement):
            return await replacement()
        if replacement is not None:
            return replacement
        if name == "control_loopback_tcp":
            return _result(1, "", "bash: connect: Connection refused")
        if name == "control_local_dns":
            return _result(0, "127.0.0.1 localhost")
        leaked = (not blocked) or any(name.startswith(prefix) for prefix in leaks)
        if name.startswith("dns"):
            return _result(0, "93.184.216.34 STREAM example.com") if leaked else _NO_ANSWER
        return _result(0) if leaked else _result(124)

    return execute


def _locking_sandbox() -> Any:
    class Sandbox:
        id = "sbx-verify"
        network_block_all = False
        runner_id = "runner-7"
        target = "us"
        snapshot = "snap-1"
        sandbox_class = SimpleNamespace(value="container")
        daemon_version = "0.9"
        outbound_proxy_url = None

        async def update_network_settings(self, **kwargs: Any) -> None:
            self.network_block_all = kwargs["network_block_all"]

    return Sandbox()


def _locked_env(tmp_path: Path, probe: Any) -> tuple[BoundedDaytonaEnvironment, Path]:
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    env = _env(trial_dir, locked=True)
    env._sandbox = _locking_sandbox()
    env._probe_exec = probe
    # exec's failure path asks the admission guard whether the sandbox vanished.
    env._daytona_usage = {"schema_version": 1}
    env._daytona_guard = SimpleNamespace(sandbox_missing=lambda sandbox_id: False)
    return env, trial_dir


def _record_of(trial_dir: Path) -> dict[str, Any]:
    return json.loads((trial_dir / "egress-lock.json").read_text(encoding="utf-8"))


def test_working_block_is_verified_and_recorded_durably(tmp_path: Path) -> None:
    env, trial_dir = _locked_env(tmp_path, _network(blocked=True))
    asyncio.run(env._lock_egress())
    assert env._egress_locked is True
    record = _record_of(trial_dir)
    assert (record["applied"], record["verified"], record["error"]) == (True, True, None)
    verification = record["verification"]
    assert verification["verified"] is True and verification["reason"]
    assert verification["runner"] == {
        "id": "sbx-verify",
        "runner_id": "runner-7",
        "target": "us",
        "snapshot": "snap-1",
        "sandbox_class": "container",
        "daemon_version": "0.9",
        "outbound_proxy_configured": False,
    }
    families = {(p["kind"], p["family"]): p["outcome"] for p in verification["probes"]}
    # IPv4, IPv6 and DNS are separate probes, each with its own outcome.
    assert families[("tcp", "v4")] == families[("tcp", "v6")] == "blocked"
    assert families[("dns", "v4")] == families[("dns", "v6")] == "blocked"
    assert egress_verification.lock_enforced(record) is True


@pytest.mark.parametrize(
    ("leaks", "leaked"),
    [
        (("tcp6",), {"tcp6_cloudflare", "tcp6_google"}),
        (("tcp4",), {"tcp4_cloudflare", "tcp4_google"}),
        (("dns",), {"dns_a", "dns_aaaa"}),
        (("tcp6_google",), {"tcp6_google"}),
    ],
)
def test_any_leak_blocks_the_agent_phase_and_names_the_path(
    tmp_path: Path, leaks: tuple[str, ...], leaked: set[str]
) -> None:
    env, trial_dir = _locked_env(tmp_path, _network(leaks=leaks))
    env._egress_lock_due = True
    with pytest.raises(RuntimeError, match="post-lock verification failed"):
        asyncio.run(env.exec("agent-first-command"))
    assert env._egress_locked is False
    record = _record_of(trial_dir)
    # The provider acknowledged; the block did not hold. Both facts are kept.
    assert (record["applied"], record["verified"]) == (True, False)
    assert "post-lock verification failed" in record["error"]
    reachable = {p["name"] for p in record["verification"]["probes"] if p["outcome"] == "reachable"}
    assert reachable == leaked
    assert all(name in record["error"] for name in leaked)
    assert egress_verification.lock_enforced(record) is False
    # Fail closed: the infra classification the agent-phase refusal produces.
    counts = classify_counts(
        reward=None,
        scored=False,
        exception={"exception_type": "RuntimeError", "exception_message": record["error"]},
    )
    assert counts["verdict"] == "excluded" and "infra" in counts["reasons"]


def test_failed_verification_is_sticky_and_never_reprobes(tmp_path: Path) -> None:
    calls: list[str] = []
    env, _ = _locked_env(tmp_path, _network(leaks=("tcp6",), calls=calls))
    env._egress_lock_due = True
    for _ in range(2):
        with pytest.raises(RuntimeError, match="post-lock verification failed"):
            asyncio.run(env.exec("agent-command"))
    # One probe plan only: a later command cannot pass a retry into the agent phase.
    assert len(calls) == len(egress_verification.PROBES)


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        # Probe tool missing: the bash/timeout control fails too.
        (
            {"control_loopback_tcp": _result(127, "", "bash: timeout: command not found")},
            "controls failed",
        ),
        ({"control_local_dns": _result(127, "", "getent: not found")}, "controls failed"),
        # Only one network probe cannot run: no block is established for it.
        ({"dns_aaaa": _result(127, "", "getent: not found")}, "dns_aaaa"),
        # bash built without /dev/tcp: exit 1, but not a network failure message.
        (
            {"tcp6_google": _result(1, "", "bash: /dev/tcp/::1/443: No such file or directory")},
            "tcp6_google",
        ),
        ({"tcp4_google": RuntimeError("sandbox exec channel closed")}, "tcp4_google"),
        ({"tcp4_cloudflare": _result(None)}, "tcp4_cloudflare"),
    ],
)
def test_unusable_probe_leaves_the_lock_unverified(
    tmp_path: Path, broken: dict[str, Any], expected: str
) -> None:
    env, trial_dir = _locked_env(tmp_path, _network(broken=broken))
    with pytest.raises(RuntimeError, match="post-lock verification failed"):
        asyncio.run(env._lock_egress())
    record = _record_of(trial_dir)
    assert record["verified"] is False and expected in record["verification"]["reason"]
    outcomes = {p["name"]: p["outcome"] for p in record["verification"]["probes"]}
    assert "reachable" not in outcomes.values()  # an error, not a claimed leak
    assert env._egress_locked is False


def test_probe_that_never_returns_times_out_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang() -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(egress_verification, "EXEC_SLACK_SEC", 0)
    monkeypatch.setattr(egress_verification, "OUTER_SLACK_SEC", 0.02)
    quick = tuple(
        dataclasses.replace(probe, inner_timeout_sec=0) for probe in egress_verification.PROBES
    )
    monkeypatch.setattr("evallab.harbor_daytona.PROBES", quick)
    env, trial_dir = _locked_env(tmp_path, _network(blocked=True, broken={"tcp6_google": hang}))
    with pytest.raises(RuntimeError, match="tcp6_google"):
        asyncio.run(env._lock_egress())
    probes = {p["name"]: p for p in _record_of(trial_dir)["verification"]["probes"]}
    assert probes["tcp6_google"]["outcome"] == "error"
    assert "deadline" in probes["tcp6_google"]["detail"]
    assert probes["tcp6_cloudflare"]["outcome"] == "blocked"


def test_provider_refusal_records_no_verification(tmp_path: Path) -> None:
    calls: list[str] = []
    env, trial_dir = _locked_env(tmp_path, _network(calls=calls))

    async def refuse(**kwargs: Any) -> None:
        raise RuntimeError("Daytona refused the lock")

    env._sandbox.update_network_settings = refuse
    with pytest.raises(RuntimeError, match="Daytona refused"):
        asyncio.run(env._lock_egress())
    record = _record_of(trial_dir)
    assert (record["applied"], record["verified"], record["verification"]) == (False, None, None)
    assert calls == []
    assert egress_verification.lock_enforced(record) is False


def test_lock_enforced_never_reads_an_acknowledgement_as_a_block() -> None:
    enforced = egress_verification.lock_enforced
    daytona = "daytona update_network_settings(network_block_all=True)"
    # Daytona record from before HAR-205: acknowledged, never verified.
    assert enforced({"applied": True, "network_block_all": True, "mechanism": daytona}) is False
    assert enforced({"applied": True, "verified": None, "mechanism": daytona}) is False
    assert enforced({"applied": True, "verified": False, "mechanism": daytona}) is False
    assert enforced({"applied": True, "verified": True, "mechanism": daytona}) is True
    assert enforced({"applied": True, "verified": True, "error": "late failure"}) is False
    assert enforced({"applied": False, "verified": True}) is False
    # Backends that record their own enforcement observation keep their meaning.
    assert enforced({"applied": True, "mechanism": "docker-compose main network_mode=none"}) is True
    assert enforced({"applied": True}) is True
    assert enforced(None) is False and enforced([]) is False


def test_acknowledged_only_trial_reads_open_in_fingerprint_and_posture(tmp_path: Path) -> None:
    from evallab.interpretation.trial_posture import _observed_egress_lock
    from evallab.setup_fingerprint import trial_fingerprint

    daytona = "daytona update_network_settings(network_block_all=True)"
    for name, record, enforced in (
        ("acked", {"applied": True, "mechanism": daytona, "network_block_all": True}, False),
        ("leaky", {"applied": True, "verified": False, "mechanism": daytona}, False),
        ("proved", {"applied": True, "verified": True, "mechanism": daytona}, True),
    ):
        trial = tmp_path / name
        trial.mkdir()
        (trial / "egress-lock.json").write_text(json.dumps(record), encoding="utf-8")
        lock = trial_fingerprint({"lock": {"mode": "locked"}}, trial)["lock"]
        assert lock["mode"] == ("locked" if enforced else "open")
        assert lock["observed_applied"] is True
        assert lock["observed_enforced"] is enforced
        assert _observed_egress_lock(trial) is enforced


# --- admission reservation is released once Daytona's async delete lands -----


class _DeletingGuard:
    """Daytona answers for the sandbox a few polls after ``delete``, then 404s."""

    def __init__(self, polls_until_gone: int | None) -> None:
        self.polls_until_gone = polls_until_gone
        self.polls = 0
        self.released: list[str] = []

    def sandbox_missing(self, sandbox_id: str) -> bool:
        self.polls += 1
        return self.polls_until_gone is not None and self.polls > self.polls_until_gone

    def release(self, name: str) -> None:
        self.released.append(name)


def _stopping_env(tmp_path: Path, guard: _DeletingGuard) -> BoundedDaytonaEnvironment:
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir()
    env = _env(trial_dir, locked=False)
    env._daytona_monitor = None
    env._daytona_guard = guard
    env._daytona_sandbox_name = "evallab-res"
    env._daytona_usage = {"schema_version": 1}
    env._sandbox = SimpleNamespace(id="sbx-del", network_block_all=False)
    return env


def test_stop_releases_reservation_after_async_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evallab.harbor_daytona.DAYTONA_RELEASE_POLL_SECONDS", 0)
    guard = _DeletingGuard(polls_until_gone=2)
    env = _stopping_env(tmp_path, guard)
    asyncio.run(env.stop(delete=True))
    assert guard.polls == 3
    assert guard.released == ["evallab-res"]


def test_stop_keeps_reservation_while_sandbox_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("evallab.harbor_daytona.DAYTONA_RELEASE_POLL_SECONDS", 0)
    monkeypatch.setattr("evallab.harbor_daytona.DAYTONA_RELEASE_WAIT_SECONDS", 0)
    guard = _DeletingGuard(polls_until_gone=None)
    env = _stopping_env(tmp_path, guard)
    asyncio.run(env.stop(delete=True))
    assert guard.released == []


# --- Modal lock ---------------------------------------------------------------


def _modal_task(tmp_path: Path, *, agent_network: str) -> Path:
    task = tmp_path / "modal-mimo-task"
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text(
        '[task]\nname = "mimo-v2.6-rl/format-code-task-000001"\n\n'
        "[metadata]\n"
        'source_dataset = "XiaomiMiMo/MiMo-V2.6-RL-oss"\n\n'
        f'[agent]\ntimeout_sec = 900.0\nnetwork_mode = "{agent_network}"\n',
        encoding="utf-8",
    )
    return task


def test_modal_mimoagent_lock_admitted_with_exact_upstream_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EVALLAB_MIMO_SELFHOSTED_UPSTREAM", "https://trainer.example/modal")
    request = _request(
        _modal_task(tmp_path, agent_network="no-network"),
        tmp_path,
        environment="modal",
        agent="mimoagent",
        egress_lock=True,
    )
    assert resolve_egress_lock(request) is True
    command = build_command(request)
    assert command[command.index("--allow-agent-host") + 1] == "trainer.example"
    assert "egress_lock=true" not in command
    # The lock record comes from LockedModalEnvironment, which records the
    # policy Modal actually applies into egress-lock.json.
    assert command[command.index("--env") + 1] == LOCKED_MODAL_ENVIRONMENT_IMPORT_PATH
    assert "evallab_egress_lock=true" in command


def test_modal_lock_refused_when_agent_baseline_is_public(tmp_path: Path) -> None:
    request = _request(
        _modal_task(tmp_path, agent_network="public"),
        tmp_path,
        environment="modal",
        agent="mimoagent",
        egress_lock=True,
    )
    with pytest.raises(ValueError, match="no-network"):
        build_command(request)


def test_modal_lock_refused_for_terminus(tmp_path: Path) -> None:
    request = _request(
        _modal_task(tmp_path, agent_network="no-network"),
        tmp_path,
        environment="modal",
        agent="terminus-2",
        egress_lock=True,
    )
    with pytest.raises(ValueError, match="cannot be combined with agent"):
        build_command(request)
