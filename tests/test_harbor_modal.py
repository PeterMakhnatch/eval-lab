"""Native Modal interpreter boundaries and truthful egress-lock records.

The executable smoke uses real Harbor lifecycle/exec code with an inert Modal
SDK boundary and local temporary files only. It runs no provider, Docker,
model, or service calls. The image's task Python has pytest; the modern
setup-only interpreter deliberately has no site-packages.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _install_modal_stub() -> None:
    if "harbor.environments.modal" in sys.modules:
        return
    try:
        importlib.import_module("harbor.environments.modal")
        return
    except ModuleNotFoundError as error:
        if error.name != "harbor" and not (error.name or "").startswith("harbor."):
            raise
    harbor = sys.modules.get("harbor") or types.ModuleType("harbor")
    envs = sys.modules.get("harbor.environments") or types.ModuleType(
        "harbor.environments"
    )
    modal_mod = types.ModuleType("harbor.environments.modal")

    class ModalEnvironment:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.logger = logging.getLogger("test-modal")
            self.trial_paths = SimpleNamespace(trial_dir=Path("."))
            self.session_id = "sess"
            self.network_policy = SimpleNamespace(
                network_mode="public", allowed_hosts=[]
            )
            self._sandbox: Any = None

        async def _create_sandbox(self, **kwargs: Any) -> Any:
            self._sandbox = SimpleNamespace(object_id="sb-test")
            return self._sandbox

        async def _apply_network_policy(self, network_policy: Any) -> None:
            return None

    modal_mod.ModalEnvironment = ModalEnvironment  # type: ignore[attr-defined]
    harbor.environments = envs  # type: ignore[attr-defined]
    envs.modal = modal_mod  # type: ignore[attr-defined]
    sys.modules.update(
        {
            "harbor": harbor,
            "harbor.environments": envs,
            "harbor.environments.modal": modal_mod,
        }
    )


_install_modal_stub()

from evallab.execution_contracts import RunRequest, build_command  # noqa: E402
from evallab.harbor_modal import (  # noqa: E402
    LockedModalEnvironment,
    ModalEnvironment,
    RegradeModalEnvironment,
    TaskModalEnvironment,
)

# --- RegradeModalEnvironment: the entry must actually import -----------------


def test_regrade_entry_actually_imports_as_modal_subclass() -> None:
    module = importlib.import_module("evallab.harbor_modal")
    resolved = getattr(module, "RegradeModalEnvironment", None)
    assert resolved is not None
    assert issubclass(resolved, TaskModalEnvironment)


def test_regrade_preserves_caller_environment_and_bounds_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_init(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(ModalEnvironment, "__init__", fake_init)
    persistent = {"OTHER": "kept", "PATH": "/task/bin:/usr/bin"}
    RegradeModalEnvironment(sandbox_timeout_secs=86400, persistent_env=persistent)
    assert captured["sandbox_timeout_secs"] == 3600
    assert captured["persistent_env"] == persistent
    assert persistent == {"OTHER": "kept", "PATH": "/task/bin:/usr/bin"}

    captured.clear()
    RegradeModalEnvironment(sandbox_timeout_secs=600)
    assert captured["sandbox_timeout_secs"] == 600
    assert "persistent_env" not in captured


# --- LockedModalEnvironment: record from effective sent policy ---------------


def _env(tmp_path: Path) -> LockedModalEnvironment:
    env = LockedModalEnvironment.__new__(LockedModalEnvironment)
    env._evallab_egress_lock = True
    env.trial_paths = SimpleNamespace(trial_dir=tmp_path)
    env.session_id = "sess-1"
    env.logger = logging.getLogger("test-modal")
    env._sandbox = SimpleNamespace(object_id="sb-123")
    return env


def _record(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "egress-lock.json").read_text())


def test_public_creation_records_open_not_applied(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_application(
        source="creation", mode="public", hosts=[], sandbox=env._sandbox
    )
    record = _record(tmp_path)
    assert record["requested"] is True
    assert record["applied"] is False
    assert record["mechanism"] is None
    assert record["sandbox_id"] == "sb-123"
    assert record["network_mode"] == "public"


def test_allowlist_application_records_applied_with_exact_host(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    env._record_application(
        source="creation", mode="public", hosts=[], sandbox=env._sandbox
    )
    env._record_application(
        source="apply", mode="allowlist", hosts=["trainer.example"], sandbox=env._sandbox
    )
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["mechanism"] == "modal-agent-phase-outbound-allowlist"
    assert record["allowed_hosts"] == ["trainer.example"]
    assert record["network_block_all"] is False
    assert record["locked_at"] is not None
    assert [a["source"] for a in record["applications"]] == ["creation", "apply"]


def test_no_network_baseline_records_block_all(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_application(
        source="creation", mode="no-network", hosts=[], sandbox=env._sandbox
    )
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["network_block_all"] is True


def test_open_policy_never_downgrades_applied_record(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env._record_application(
        source="apply", mode="allowlist", hosts=["trainer.example"], sandbox=env._sandbox
    )
    env._record_application(
        source="apply", mode="public", hosts=[], sandbox=env._sandbox
    )
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["allowed_hosts"] == ["trainer.example"]


def test_effective_creation_policy_override_wins() -> None:
    """The block_network override, not the static baseline, is recorded."""
    effective = LockedModalEnvironment._effective_creation_policy
    no_network = SimpleNamespace(
        network_policy=SimpleNamespace(network_mode="no-network", allowed_hosts=[]),
        _dynamic_network=False,
        _network_disabled=True,
        _network_is_allowlist=False,
    )
    # DinD-style override: public networking is sent despite the baseline.
    assert effective(no_network, False) == ("public", [])
    # No override: the no-network baseline is what is sent.
    assert effective(no_network, None) == ("no-network", [])
    assert effective(no_network, True) == ("no-network", [])

    allowlist = SimpleNamespace(
        network_policy=SimpleNamespace(
            network_mode="allowlist", allowed_hosts=["trainer.example"]
        ),
        _dynamic_network=False,
        _network_disabled=False,
        _network_is_allowlist=True,
    )
    assert effective(allowlist, None) == ("allowlist", ["trainer.example"])
    # Dynamic networking sends the startup policy's outbound kwargs.
    dynamic = SimpleNamespace(
        network_policy=SimpleNamespace(
            network_mode="allowlist", allowed_hosts=["trainer.example"]
        ),
        _dynamic_network=True,
        _network_disabled=False,
        _network_is_allowlist=True,
    )
    assert effective(dynamic, False) == ("allowlist", ["trainer.example"])


def test_apply_wrapper_records_after_modal_accepts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    seen: list[Any] = []

    async def fake_apply(self: Any, network_policy: Any) -> None:
        seen.append(network_policy)

    monkeypatch.setattr(
        "evallab.harbor_modal.ModalEnvironment._apply_network_policy",
        fake_apply,
    )
    asyncio.run(
        env._apply_network_policy(
            SimpleNamespace(network_mode="allowlist", allowed_hosts=["trainer.example"])
        )
    )
    assert len(seen) == 1
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["sandbox_id"] == "sb-123"


def _real_harbor_modal() -> bool:
    module = sys.modules.get("harbor.environments.modal")
    return bool(
        module is not None
        and getattr(module, "_HAS_MODAL", False)
        and hasattr(module, "Sandbox")
    )


@pytest.mark.skipif(
    not _real_harbor_modal(),
    reason="requires the real Harbor ModalEnvironment with an inert SDK boundary",
)
def test_real_creation_records_effective_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Harbor creation code, fake Modal SDK: both override states.

    With a static no-network baseline, ``block_network=False`` (Harbor's
    DinD path) sends public networking and must record open; with no
    override the baseline blocks all and must record applied.
    """
    import harbor.environments.modal as modal_mod
    from harbor.models.task.config import NetworkMode, NetworkPolicy

    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(object_id="sb-fake")

    monkeypatch.setattr(
        modal_mod, "Sandbox", SimpleNamespace(create=SimpleNamespace(aio=fake_create))
    )
    for name, value in (
        ("_cpu_config", None),
        ("_memory_config", None),
        ("_gpu_config", None),
        ("_sandbox_labels", {}),
        ("_secrets_config", []),
        ("_volumes_config", {}),
    ):
        monkeypatch.setattr(
            modal_mod.ModalEnvironment, name, lambda self, _v=value: _v
        )
    monkeypatch.setattr(modal_mod.ModalEnvironment, "stream_enabled", False)

    def make_env() -> LockedModalEnvironment:
        env = LockedModalEnvironment.__new__(LockedModalEnvironment)
        env._evallab_egress_lock = True
        env.trial_paths = SimpleNamespace(trial_dir=tmp_path)
        env.session_id = "sess-1"
        env.logger = logging.getLogger("test-modal")
        env._network_policy = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
        env._dynamic_network = False
        env._app = object()
        env._image = object()
        env._sandbox_timeout = 60
        env._sandbox_idle_timeout = None
        env._kwargs = {}
        env._sandbox = None
        return env

    asyncio.run(make_env()._create_sandbox(block_network=False))
    assert captured["block_network"] is False
    record = _record(tmp_path)
    assert record["applied"] is False
    assert record["network_mode"] == "public"
    assert record["sandbox_id"] == "sb-fake"

    (tmp_path / "egress-lock.json").unlink()
    asyncio.run(make_env()._create_sandbox())
    assert captured["block_network"] is True
    record = _record(tmp_path)
    assert record["applied"] is True
    assert record["network_block_all"] is True
    assert record["sandbox_id"] == "sb-fake"


@pytest.fixture
def native_modal_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real Harbor, fake remote SDK, and real local shell/Python execution."""
    if not _real_harbor_modal():
        pytest.skip("requires the pinned Harbor Modal runtime")
    import harbor.environments.modal as modal_mod
    from harbor.cli.utils import parse_kwargs, resolve_environment_spec
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.task.config import (
        EnvironmentConfig,
        HealthcheckConfig,
        NetworkMode,
        NetworkPolicy,
    )
    from harbor.models.trial.config import EnvironmentConfig as TrialEnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    import evallab.harbor_modal as wrapper

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    tests_dir = tmp_path / "image-tests"
    state_dir = tmp_path / "image-state"
    grade_dir = tmp_path / "image-grade"
    package = tmp_path / "package"
    environment_dir = package / "environment"
    setup_dir = environment_dir / "setup"
    setup_dir.mkdir(parents=True)
    (environment_dir / "Dockerfile").write_text("FROM fixture\n")
    (package / "task.toml").write_text(
        '[agent]\nnetwork_mode = "no-network"\n'
        '[environment]\nnetwork_mode = "public"\n'
    )
    hidden_dir = package / "tests"
    bundle = hidden_dir / wrapper._SETUP_BUNDLE_NAME
    bundle.mkdir(parents=True)
    (hidden_dir / "Dockerfile").write_text("FROM fixture\n")
    (hidden_dir / "test.patch").write_text("hidden assertion sentinel\n")
    (hidden_dir / "test.sh").write_text("# hidden verifier sentinel\n")
    setup = f"""#!/bin/bash
set -eu
mkdir -p {shlex.quote(str(state_dir))}
[ ! -f {shlex.quote(str(state_dir / "ready"))} ] || exit 0
python3 - <<'PY'
from __future__ import annotations
import os
assert os.environ["EVALLAB_SMOKE_INTERPRETER"] == "helper"
print("supported helper ran cleanup")
PY
rm -rf {shlex.quote(str(tests_dir))}
printf "VALUE = 'baseline'\\n" > {shlex.quote(str(workdir / "task_dependency.py"))}
touch {shlex.quote(str(state_dir / "ready"))}
"""
    (setup_dir / "setup.sh").write_text(setup)
    (bundle / "setup.sh").write_text(setup)

    paths: dict[str, str] = {}
    for name, isolated in (("task", False), ("helper", True)):
        binary_dir = tmp_path / f"{name}-bin"
        binary_dir.mkdir()
        python = binary_dir / "python3"
        python.write_text(
            "#!/bin/sh\n"
            f"export EVALLAB_SMOKE_INTERPRETER={name}\n"
            f"exec {shlex.quote(sys.executable)} {'-S ' if isolated else ''}\"$@\"\n"
        )
        python.chmod(0o755)
        paths[name] = f"{binary_dir}:/usr/bin:/bin"
    monkeypatch.setattr(wrapper, "MODAL_PATH_PIN", paths["helper"])
    monkeypatch.setattr(wrapper, "_TESTS_DIR", str(tests_dir))
    monkeypatch.setattr(wrapper, "MIMO_STATE_DIR", str(state_dir))
    monkeypatch.setattr(wrapper, "V2_GRADE_DIR", str(grade_dir))
    monkeypatch.setattr(modal_mod, "_cached_env_secret", lambda env: dict(env))
    # There is no remote OS user in this local transport. All commands stay
    # unprivileged, while the native public exec/environment merge still runs.
    monkeypatch.setattr(ModalEnvironment, "_resolve_user", lambda self, user: None)

    creations: list[dict[str, Any]] = []
    policies: list[dict[str, Any]] = []
    exec_envs: list[dict[str, str]] = []

    async def local_exec(*args: str, **kwargs: Any) -> Any:
        image_env = dict(os.environ)
        image_env.pop("PYTHONPATH", None)
        image_env.update(PATH=paths["task"], PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        for secret in creations[-1]["secrets"] + kwargs["secrets"]:
            image_env.update(secret)
        exec_envs.append(image_env)
        completed = subprocess.run(
            args,
            cwd=kwargs.get("workdir") or workdir,
            env=image_env,
            capture_output=True,
            text=True,
            timeout=kwargs.get("timeout") or 30,
            check=False,
        )

        async def stdout() -> str:
            return completed.stdout

        async def stderr() -> str:
            return completed.stderr

        async def wait() -> int:
            return completed.returncode

        return SimpleNamespace(
            stdout=SimpleNamespace(read=SimpleNamespace(aio=stdout)),
            stderr=SimpleNamespace(read=SimpleNamespace(aio=stderr)),
            wait=SimpleNamespace(aio=wait),
        )

    async def apply_policy(**kwargs: Any) -> None:
        policies.append(kwargs)

    async def refuse_upload(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the bundled-image lifecycle must not upload hidden inputs")

    sandbox = SimpleNamespace(
        object_id="sb-local",
        exec=SimpleNamespace(aio=local_exec),
        filesystem=SimpleNamespace(copy_from_local=SimpleNamespace(aio=refuse_upload)),
        _experimental_set_outbound_network_policy=SimpleNamespace(aio=apply_policy),
    )

    async def create(*args: Any, **kwargs: Any) -> Any:
        creations.append(kwargs)
        return sandbox

    async def app_lookup(**kwargs: Any) -> Any:
        return object()

    monkeypatch.setattr(
        modal_mod,
        "Image",
        SimpleNamespace(
            from_registry=lambda *args, **kwargs: object(),
            from_dockerfile=lambda *args, **kwargs: object(),
        ),
    )
    monkeypatch.setattr(
        modal_mod, "App", SimpleNamespace(lookup=SimpleNamespace(aio=app_lookup))
    )
    monkeypatch.setattr(
        modal_mod, "Sandbox", SimpleNamespace(create=SimpleNamespace(aio=create))
    )

    def make(lane: str, *, separate: bool) -> Any:
        request = RunRequest(
            task=package,
            agent="oracle",
            name=f"modal-smoke-{lane}",
            jobs_dir=tmp_path / "jobs",
            environment="modal",
            allow_billable=True,
            egress_lock=lane == "locked",
            modal_sandbox_timeout_seconds=600,
            modal_resource_policy="limit",
            modal_app_name="mimo-clean-census-modal-smoke",
        )
        command = build_command(request, setup_fingerprint="fixture")
        selector = command[command.index("--env") + 1]
        env_kwargs = parse_kwargs(
            [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--environment-kwarg"
            ]
        )
        if lane == "regrade":
            selector = "evallab.harbor_modal:RegradeModalEnvironment"
        env_type, import_path = resolve_environment_spec(selector)
        policy = NetworkPolicy(network_mode=NetworkMode.PUBLIC)
        denied = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
        if separate:
            shutil.copytree(hidden_dir, tests_dir)
        return EnvironmentFactory.create_environment_from_config(
            config=TrialEnvironmentConfig(
                type=env_type, import_path=import_path, kwargs=env_kwargs
            ),
            environment_dir=hidden_dir if separate else environment_dir,
            environment_name="modal-smoke",
            session_id=f"modal-smoke-{lane}",
            trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
            task_env_config=EnvironmentConfig(
                workdir=str(workdir),
                cpus=2,
                memory_mb=8192,
                env={"TASK_SETTING": "preserved"},
                healthcheck=(
                    None
                    if separate
                    else HealthcheckConfig(
                        command=f"bash {shlex.quote(str(setup_dir / 'setup.sh'))}",
                        retries=1,
                    )
                ),
            ),
            enable_environment_dir_upload=not separate,
            network_policy=policy,
            phase_network_policies=[denied],
        )

    return SimpleNamespace(
        make=make,
        paths=paths,
        workdir=workdir,
        tests_dir=tests_dir,
        state_dir=state_dir,
        grade_dir=grade_dir,
        hidden_dir=hidden_dir,
        creations=creations,
        policies=policies,
        exec_envs=exec_envs,
    )


def _seed_task_submission(runtime: Any) -> None:
    """Seed submitted bytes only after the native startup boundary."""
    (runtime.workdir / "task_dependency.py").write_text("VALUE = 'submitted'\n")
    (runtime.workdir / "test_runtime.py").write_text(
        "import os\n"
        "import task_dependency\n"
        "def test_original_task_interpreter_and_dependencies():\n"
        "    assert os.environ['EVALLAB_SMOKE_INTERPRETER'] == 'task'\n"
        "    assert os.environ['TASK_SETTING'] == 'preserved'\n"
        "    assert task_dependency.VALUE == 'submitted'\n"
    )


async def _assert_task_pytest(env: Any) -> None:
    """Execute actual pytest without rewriting or repairing the submitted bytes."""
    result = await env.exec("python3 -m pytest -q -p no:cacheprovider test_runtime.py")
    assert result.return_code == 0, (result.stdout, result.stderr)
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("lane", ["open", "locked", "regrade"])
def test_native_modal_task_python_survives_verifier_setup_boundary(
    native_modal_runtime: Any, lane: str,
) -> None:
    """Cheap executable smoke: native setup -> seed -> repeated task pytest."""
    runtime = native_modal_runtime
    env = runtime.make(lane, separate=True)
    original_tests = {
        path.relative_to(runtime.tests_dir): path.read_bytes()
        for path in runtime.tests_dir.rglob("*") if path.is_file()
    }

    async def exercise() -> None:
        await env.start(force_build=False)
        assert (runtime.state_dir / "ready").is_file()
        assert {
            path.relative_to(runtime.tests_dir): path.read_bytes()
            for path in runtime.tests_dir.rglob("*") if path.is_file()
        } == original_tests
        assert not list(runtime.grade_dir.glob("modal-tests.*"))
        assert env._persistent_env == {"TASK_SETTING": "preserved"}
        with env.scoped_exec_env({"PATH": runtime.paths["helper"]}):
            missing = await env.exec("python3 -c 'import pytest'")
        assert missing.return_code != 0
        assert "No module named" in missing.stderr
        _seed_task_submission(runtime)
        await _assert_task_pytest(env)
        # The unchanged v6 verifier invokes setup again. Its existing ready
        # marker must preserve submitted bytes and the original interpreter.
        repeated_setup = await env.exec(
            f"bash {shlex.quote(str(runtime.state_dir / 'setup.sh'))}"
        )
        assert repeated_setup.return_code == 0
        await _assert_task_pytest(env)
        from harbor.models.task.config import NetworkMode, NetworkPolicy
        await env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))

    asyncio.run(exercise())
    assert len(runtime.creations) == 1
    sent = runtime.creations[0]
    assert sent["timeout"] == 600
    assert sent["cpu"] == (0.125, 2)
    assert sent["memory"] == (128, 8192)
    assert runtime.policies == [
        {"outbound_cidr_allowlist": [], "outbound_domain_allowlist": []}
    ]
    evidence = next(env.trial_paths.trial_dir.glob("modal-setup-*.log")).read_text()
    assert "supported helper ran cleanup" in evidence
    assert "ready marker observed" in evidence
    assert "return_code=0" in evidence
    if lane == "locked":
        record = _record(env.trial_paths.trial_dir)
        assert [item["network_mode"] for item in record["applications"]] == [
            "public", "no-network"
        ]
        assert record["applied"] is True


@pytest.mark.parametrize("lane", ["open", "locked", "regrade"])
def test_native_modal_primary_setup_uses_helper_without_hidden_inputs(
    native_modal_runtime: Any, lane: str,
) -> None:
    runtime = native_modal_runtime
    env = runtime.make(lane, separate=False)

    async def exercise() -> None:
        await env.start(force_build=False)
        assert not runtime.tests_dir.exists()
        assert not (runtime.state_dir / "ready").exists()
        assert not list(env.trial_paths.trial_dir.glob("modal-setup-*.log"))
        await env.run_healthcheck()
        assert (runtime.state_dir / "ready").is_file()
        assert not runtime.tests_dir.exists()
        assert not (runtime.workdir / "test.patch").exists()
        assert not (runtime.workdir / "test.sh").exists()
        _seed_task_submission(runtime)
        await _assert_task_pytest(env)
        assert env._persistent_env == {"TASK_SETTING": "preserved"}

    asyncio.run(exercise())
    assert len(runtime.creations) == 1


def test_native_modal_setup_failure_keeps_tests_and_task_path(
    native_modal_runtime: Any,
) -> None:
    runtime = native_modal_runtime
    source = runtime.hidden_dir / "_verifier-setup" / "setup.sh"
    source.write_text(source.read_text().replace(
        'print("supported helper ran cleanup")',
        'print("cleanup failure evidence", flush=True)\nraise SystemExit(17)',
    ))
    env = runtime.make("open", separate=True)
    patch_before = (runtime.tests_dir / "test.patch").read_bytes()

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="Modal verifier setup failed"):
            await env.start(force_build=False)
        assert not (runtime.state_dir / "ready").exists()
        assert (runtime.tests_dir / "test.patch").read_bytes() == patch_before
        _seed_task_submission(runtime)
        await _assert_task_pytest(env)

    asyncio.run(exercise())
    evidence = next(env.trial_paths.trial_dir.glob("modal-setup-*.log")).read_text()
    assert "cleanup failure evidence" in evidence
    assert "return_code=17" in evidence


def test_native_modal_failed_healthcheck_does_not_leak_helper_path(
    native_modal_runtime: Any,
) -> None:
    from harbor.environments.base import HealthcheckError
    from harbor.models.task.config import HealthcheckConfig

    runtime = native_modal_runtime
    env = runtime.make("open", separate=False)
    program = (
        "import os; assert os.environ['EVALLAB_SMOKE_INTERPRETER'] == 'helper';"
        " raise SystemExit(17)"
    )
    env.task_env_config.healthcheck = HealthcheckConfig(
        command=f"python3 -c {shlex.quote(program)}", retries=1
    )

    async def exercise() -> None:
        await env.start(force_build=False)
        with pytest.raises(HealthcheckError):
            await env.run_healthcheck()
        _seed_task_submission(runtime)
        await _assert_task_pytest(env)

    asyncio.run(exercise())


@pytest.mark.parametrize("has_setup_bundle", [False, True])
def test_native_modal_ordinary_healthcheck_keeps_task_dependencies(
    native_modal_runtime: Any, has_setup_bundle: bool,
) -> None:
    from harbor.models.task.config import HealthcheckConfig

    runtime = native_modal_runtime
    env = runtime.make("open", separate=False)
    if not has_setup_bundle:
        (env.environment_dir / "setup" / "setup.sh").unlink()

    async def exercise() -> None:
        await env.start(force_build=False)
        _seed_task_submission(runtime)
        await env.run_healthcheck(HealthcheckConfig(
            command="python3 -m pytest -q -p no:cacheprovider test_runtime.py",
            retries=1,
        ))

    asyncio.run(exercise())


def test_native_modal_missing_ready_marker_fails_startup(
    native_modal_runtime: Any,
) -> None:
    runtime = native_modal_runtime
    source = runtime.hidden_dir / "_verifier-setup" / "setup.sh"
    source.write_text(source.read_text().replace(
        f"touch {shlex.quote(str(runtime.state_dir / 'ready'))}", ":"
    ))
    env = runtime.make("open", separate=True)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="Modal verifier setup failed"):
            await env.start(force_build=False)
        assert not (runtime.state_dir / "ready").exists()
        assert (runtime.tests_dir / "test.patch").read_bytes() == (
            runtime.hidden_dir / "test.patch"
        ).read_bytes()

    asyncio.run(exercise())
    evidence = next(env.trial_paths.trial_dir.glob("modal-setup-*.log")).read_text()
    assert "supported helper ran cleanup" in evidence
    assert "ready marker observed" not in evidence
    assert "return_code=1" in evidence
