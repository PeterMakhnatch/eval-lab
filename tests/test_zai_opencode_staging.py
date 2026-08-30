"""Tests for the Z.ai/OpenCode adapter and the host task staging helper.

Adapter tests stub the ``harbor`` package exactly like ``test_harbor_deepseek``
(the lab venv does not install Harbor). Staging tests build fixture packages
shaped like the three benchmark families the Z.ai lane executes: Action
Memory (compose + internal MCP sidecar), FuncDAG (the real committed
single-container package), and Recovery (compose + separate verifier with a
secret fixture file).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET_SENTINEL = "zai-secret-must-not-reach-exec"

DARWIN_POLICY = None  # built lazily to avoid importing harbor_network first


def _darwin_policy():  # type: ignore[no-untyped-def]
    from evallab.harbor_network import HarborNetworkPolicy

    return HarborNetworkPolicy(
        network_mode="public",
        network_isolation_enforced=False,
        network_isolation_reason="darwin-docker-cannot-enforce-no-network",
    )


def _linux_policy():  # type: ignore[no-untyped-def]
    from evallab.harbor_network import HarborNetworkPolicy

    return HarborNetworkPolicy(
        network_mode="no-network",
        network_isolation_enforced=True,
        network_isolation_reason=None,
    )


# --------------------------------------------------------------------------
# Harbor stubs (adapter under test)
# --------------------------------------------------------------------------


@dataclass
class _StubConnection:
    provider: str | None = None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    configured_base_url: str | None = None
    env: dict[str, str] = field(default_factory=dict, repr=False)


class _StubOpenCode:
    """Minimal stand-in for Harbor's installed OpenCode agent."""

    def __init__(
        self,
        *args: Any,
        version: str | None = None,
        model_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.model_name = model_name
        self.received_version = version
        self.exec_commands: list[str] = []
        self.run_calls = 0
        self.run_error: Exception | None = None
        del args, kwargs

    async def exec_as_agent(self, environment, command, env=None, **kwargs):  # type: ignore[no-untyped-def]
        del environment, env, kwargs
        self.exec_commands.append(command)
        return "ok"

    async def run(self, instruction, environment, context) -> None:  # type: ignore[no-untyped-def]
        del instruction, context
        self.run_calls += 1
        self.exec_commands.append("<parent-run>")
        if self.run_error is not None:
            raise self.run_error


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


@pytest.fixture
def adapter_module(monkeypatch: pytest.MonkeyPatch):
    for name in ("harbor", "harbor.agents", "harbor.agents.installed", "harbor.environments"):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.opencode",
        _module("harbor.agents.installed.opencode", OpenCode=_StubOpenCode),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.model_connection",
        _module("harbor.agents.model_connection", ResolvedModelConnection=_StubConnection),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.environments.base",
        _module("harbor.environments.base", BaseEnvironment=object),
    )
    sys.modules.pop("evallab.harbor_zai_opencode", None)
    try:
        yield importlib.import_module("evallab.harbor_zai_opencode")
    finally:
        sys.modules.pop("evallab.harbor_zai_opencode", None)


def _make_agent(adapter_module, model_name: str, **kwargs: Any):  # type: ignore[no-untyped-def]
    return adapter_module.ZaiOpenCodeAgent(model_name=model_name, **kwargs)


# --------------------------------------------------------------------------
# Adapter tests
# --------------------------------------------------------------------------


def test_adapter_pins_opencode_version_by_default(adapter_module) -> None:
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    assert agent.received_version == adapter_module.PINNED_OPENCODE_VERSION
    assert agent.received_version == "1.18.25"


def test_adapter_accepts_explicit_version_override(adapter_module) -> None:
    agent = _make_agent(
        adapter_module, "zai-coding-plan/glm-5.3-flash", version="1.19.0"
    )
    assert agent.received_version == "1.19.0"


def test_adapter_model_guard_accepts_zai_coding_plan(adapter_module) -> None:
    for model in ("zai-coding-plan/glm-5.3", "zai-coding-plan/glm-5.3-flash"):
        assert (
            adapter_module.validate_model_name(model) == model
        )


@pytest.mark.parametrize(
    "model_name",
    [
        "openai/gpt-5.2",
        "zai/glm-5.3",
        "glm-5.3-flash",
        "",
        None,
    ],
)
def test_adapter_model_guard_rejects_other_providers(
    adapter_module, model_name: str | None
) -> None:
    with pytest.raises(ValueError, match="only accepts models under|provider/model"):
        adapter_module.validate_model_name(model_name)


def test_adapter_model_guard_rejects_bare_provider_prefix(adapter_module) -> None:
    """``zai-coding-plan/`` with no model suffix must fail closed."""
    with pytest.raises(ValueError, match="non-empty model"):
        adapter_module.validate_model_name("zai-coding-plan/")


def test_adapter_model_guard_rejects_wrong_provider_at_construction(
    adapter_module,
) -> None:
    with pytest.raises(ValueError):
        _make_agent(adapter_module, "openai/gpt-5.2")


def test_adapter_link_run_cleanup_order(adapter_module) -> None:
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    asyncio.run(agent.run("do the task", environment=object(), context=object()))
    assert agent.exec_commands == [
        adapter_module.CREATE_AUTH_LINK_COMMAND,
        "<parent-run>",
        adapter_module.REMOVE_AUTH_LINK_COMMAND,
    ]
    assert agent.run_calls == 1


def test_adapter_cleanup_runs_on_failure_and_preserves_original_error(
    adapter_module,
) -> None:
    class AgentBlewUp(RuntimeError):
        pass

    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    agent.run_error = AgentBlewUp("provider stream died")
    with pytest.raises(AgentBlewUp, match="provider stream died"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))
    # Cleanup still executed, after the failing parent run.
    assert agent.exec_commands == [
        adapter_module.CREATE_AUTH_LINK_COMMAND,
        "<parent-run>",
        adapter_module.REMOVE_AUTH_LINK_COMMAND,
    ]


def test_adapter_cleanup_failure_does_not_mask_original_error(adapter_module) -> None:
    class AgentBlewUp(RuntimeError):
        pass

    class CleanupBlewUp(RuntimeError):
        pass

    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    agent.run_error = AgentBlewUp("original failure")

    async def failing_cleanup(environment, command, env=None, **kwargs):  # type: ignore[no-untyped-def]
        del environment, env, kwargs
        if command.startswith("rm -f"):
            raise CleanupBlewUp("rm failed")
        agent.exec_commands.append(command)
        return "ok"

    agent.exec_as_agent = failing_cleanup  # type: ignore[method-assign]

    with pytest.raises(AgentBlewUp, match="original failure"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))


def test_adapter_commands_and_env_never_contain_secret_values(
    adapter_module,
) -> None:
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    captured: list[dict[str, Any]] = []

    async def spying_exec(environment, command, env=None, **kwargs):  # type: ignore[no-untyped-def]
        del environment, kwargs
        captured.append({"command": command, "env": env})
        agent.exec_commands.append(command)
        return "ok"

    agent.exec_as_agent = spying_exec  # type: ignore[method-assign]
    asyncio.run(agent.run("do the task", environment=object(), context=object()))

    for record in captured:
        blob = json.dumps(record, default=str)
        assert SECRET_SENTINEL not in blob
        assert "api_key" not in blob
    # The two adapter commands are composed only of the constant paths.
    assert (
        f"mkdir -p {adapter_module.AUTH_LINK_DIR} && "
        f"ln -sfn {adapter_module.AUTH_SECRET_MOUNT} {adapter_module.AUTH_LINK_PATH}"
    ) == adapter_module.CREATE_AUTH_LINK_COMMAND
    assert f"rm -f {adapter_module.AUTH_LINK_PATH}" == adapter_module.REMOVE_AUTH_LINK_COMMAND


def test_adapter_run_refuses_wrong_provider_assigned_after_construction(
    adapter_module,
) -> None:
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-flash")
    # A wrong provider assigned after construction must fail closed BEFORE
    # the auth link is created.
    agent.model_name = "openai/gpt-5.2"
    with pytest.raises(ValueError, match="only accepts models under"):
        asyncio.run(agent.run("do the task", environment=object(), context=object()))
    assert agent.exec_commands == []


def test_adapter_does_not_gate_plan_limited_models(adapter_module) -> None:
    """glm-5.3-highspeed passes the prefix guard; the provider answers 429.

    Observed provider fact (2026-08-29): the Coding Plan subscription does
    not yet include glm-5.3-highspeed and the provider returns HTTP 429. The
    adapter must NOT block the selector — the access failure has to surface
    as the provider's own error, distinctly from model outcomes, with no
    retry or fallback added here.
    """
    assert (
        adapter_module.validate_model_name("zai-coding-plan/glm-5.3-highspeed")
        == "zai-coding-plan/glm-5.3-highspeed"
    )
    agent = _make_agent(adapter_module, "zai-coding-plan/glm-5.3-highspeed")
    asyncio.run(agent.run("do the task", environment=object(), context=object()))
    # The link/run/cleanup sequence executed normally; no retry loop exists.
    assert agent.exec_commands == [
        adapter_module.CREATE_AUTH_LINK_COMMAND,
        "<parent-run>",
        adapter_module.REMOVE_AUTH_LINK_COMMAND,
    ]


# --------------------------------------------------------------------------
# Fixture packages (staging under test)
# --------------------------------------------------------------------------

INTERNAL_NETWORK = "workbench-internal"
SIDECAR = "mcp-service"
PINNED_BASE = "python:3.12.11-slim@sha256:" + "a" * 64


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sidecar_task_toml(name: str) -> str:
    return f'''schema_version = "1.4"
artifacts = ["/app/output/benchmark-events.jsonl"]

[task]
name = "{name}"
version = "1.0.0"

[[task.authors]]
name = "Eval Lab"
email = "eval-lab@example.invalid"

[agent]
timeout_sec = 600.0

[verifier]
timeout_sec = 120.0
environment_mode = "separate"

[verifier.environment]
network_mode = "no-network"

[environment]
network_mode = "no-network"
build_timeout_sec = 120.0
os = "linux"
cpus = 1
memory_mb = 512
storage_mb = 1024

[[environment.mcp_servers]]
name = "memory_mcp"
transport = "streamable-http"
url = "http://{SIDECAR}:8080/mcp"
'''


def _compose_document() -> dict[str, Any]:
    return {
        "services": {
            "main": {
                "build": ".",
                "networks": [INTERNAL_NETWORK],
                "volumes": ["evidence-volume:/app/output:ro"],
            },
            SIDECAR: {
                "build": "./mcp-server",
                "networks": [INTERNAL_NETWORK],
                "volumes": ["evidence-volume:/app/output"],
            },
        },
        "networks": {INTERNAL_NETWORK: {"internal": True}},
        "volumes": {"evidence-volume": None},
    }


def _make_action_memory_like(root: Path) -> Path:
    task = root / "action-memory-like"
    _write(task / "task.toml", _sidecar_task_toml("evallab/action-memory-like-seed42"))
    _write(task / "instruction.md", "# Action memory task\nUse the MCP server.\n")
    _write(
        task / "environment" / "docker-compose.yaml",
        yaml.safe_dump(_compose_document(), sort_keys=False),
    )
    _write(task / "environment" / "Dockerfile", f"FROM {PINNED_BASE}\nWORKDIR /app\n")
    _write(
        task / "environment" / "mcp-server" / "Dockerfile",
        f"FROM {PINNED_BASE}\nWORKDIR /srv\n",
    )
    _write(task / "tests" / "Dockerfile", f"FROM {PINNED_BASE}\nWORKDIR /app\n")
    _write(task / "tests" / "verify.py", "print('verify')\n")
    return task


def _make_recovery_like(root: Path, *, with_symlink: bool = False) -> Path:
    task = root / "recovery-like"
    _write(task / "task.toml", _sidecar_task_toml("evallab/recovery-like-seed42"))
    _write(task / "instruction.md", "# Recovery task\nRecover from the fault.\n")
    _write(
        task / "environment" / "docker-compose.yaml",
        yaml.safe_dump(_compose_document(), sort_keys=False),
    )
    _write(task / "environment" / "Dockerfile", f"FROM {PINNED_BASE}\nWORKDIR /app\n")
    _write(
        task / "tests" / "Dockerfile",
        f"FROM {PINNED_BASE}\nWORKDIR /app\nCOPY wheelhouse /wheelhouse\n",
    )
    _write(task / "tests" / "verify.py", "print('verify')\n")
    _write(task / "tests" / "fixtures" / "secret_key.txt", "deadbeef" * 8 + "\n")
    if with_symlink:
        (task / "tests" / "fixtures" / "escape.txt").symlink_to("/etc/passwd")
    return task


FUNCDAG_TASK = REPO_ROOT / "library" / "tasks" / "experimental" / "syn-funcdag-easy"


def _tree_digest(root: Path) -> dict[str, str]:
    import hashlib

    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


# --------------------------------------------------------------------------
# Staging tests: the three fixture shapes
# --------------------------------------------------------------------------


def test_stage_action_memory_shape() -> None:
    import tempfile

    from evallab.host_task_staging import stage_task_for_host

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        before = _tree_digest(source)
        destination = root / "staged-action"

        manifest = stage_task_for_host(
            source,
            destination,
            host_policy=_darwin_policy(),
            pin_platform=True,
            attach_agent_egress=True,
        )

        # task.toml adaptation recorded: canonical no-network -> public.
        assert manifest.task_toml_adapted is True
        assert manifest.requested_agent_network == "no-network"
        assert manifest.effective_agent_network == "public"
        assert manifest.requested_verifier_network == "no-network"
        assert manifest.effective_verifier_network == "public"
        assert manifest.network_isolation_enforced is False
        assert manifest.network_isolation_reason == "darwin-docker-cannot-enforce-no-network"

        # main keeps internal network and gains public egress; sidecar stays internal-only.
        compose = yaml.safe_load(
            (destination / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        )
        assert compose["services"]["main"]["networks"] == [INTERNAL_NETWORK, "default"]
        assert compose["services"][SIDECAR]["networks"] == [INTERNAL_NETWORK]
        assert manifest.main_networks == (INTERNAL_NETWORK, "default")
        assert manifest.sidecar_networks == (INTERNAL_NETWORK,)
        assert manifest.agent_public_egress is True

        # every compose service pinned; verifier and environment Dockerfiles pinned.
        assert compose["services"]["main"]["platform"] == "linux/amd64"
        assert compose["services"][SIDECAR]["platform"] == "linux/amd64"
        for rel in ("environment/Dockerfile", "tests/Dockerfile"):
            first_line = (destination / rel).read_text(encoding="utf-8").splitlines()[0]
            assert first_line == f"FROM --platform=linux/amd64 {PINNED_BASE}"
        assert {pin.target for pin in manifest.platform_pins} == {
            "service:main",
            f"service:{SIDECAR}",
            "dockerfile:environment/Dockerfile",
            "dockerfile:tests/Dockerfile",
        }
        assert manifest.platform_reason is not None
        assert "wheel manifest" in manifest.platform_reason

        # digests and manifest wiring: the staged copy differs from the
        # canonical source (adapted bytes), and both digests are recorded.
        assert manifest.staged_payload_digest != manifest.source_payload_digest
        assert manifest.adapter_version
        assert manifest.adapter_digest.startswith("sha256:")
        written = json.loads(
            (destination / "run_manifest.json").read_text(encoding="utf-8")
        )
        assert written["effective_agent_network"] == "public"
        assert "task.toml" in written["modified_paths"]

        # source untouched.
        assert _tree_digest(source) == before


def test_stage_funcdag_real_registered_package() -> None:
    import tempfile

    from evallab.host_task_staging import stage_task_for_host

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        destination = root / "staged-funcdag"
        before = _tree_digest(FUNCDAG_TASK)

        manifest = stage_task_for_host(
            FUNCDAG_TASK,
            destination,
            host_policy=_darwin_policy(),
            pin_platform=True,
            attach_agent_egress=True,
        )

        # Single-container package: no compose; egress is the task.toml network mode.
        assert manifest.compose_present is False
        assert manifest.agent_public_egress is True
        assert manifest.effective_agent_network == "public"
        assert manifest.requested_verifier_network == "no-network"
        assert manifest.effective_verifier_network == "public"
        assert manifest.main_networks is None and manifest.sidecar_networks is None

        # verifier Dockerfile pinned; environment Dockerfile pinned.
        staged_env = (destination / "environment" / "Dockerfile").read_text(encoding="utf-8")
        staged_tests = (destination / "tests" / "Dockerfile").read_text(encoding="utf-8")
        assert staged_env.splitlines()[0].startswith("FROM --platform=linux/amd64 ")
        assert staged_tests.splitlines()[0].startswith("FROM --platform=linux/amd64 ")

        # Nothing outside the intended files changed vs the committed package.
        source_env = (FUNCDAG_TASK / "environment" / "Dockerfile").read_text(encoding="utf-8")
        source_tests = (FUNCDAG_TASK / "tests" / "Dockerfile").read_text(encoding="utf-8")
        assert staged_env.replace("--platform=linux/amd64 ", "") == source_env
        assert staged_tests.replace("--platform=linux/amd64 ", "") == source_tests

        assert _tree_digest(FUNCDAG_TASK) == before


def test_stage_recovery_shape_preserves_secret_fixture_and_source() -> None:
    import tempfile

    from evallab.host_task_staging import stage_task_for_host

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_recovery_like(root)
        before = _tree_digest(source)
        destination = root / "staged-recovery"

        manifest = stage_task_for_host(
            source,
            destination,
            host_policy=_darwin_policy(),
            pin_platform=True,
            attach_agent_egress=True,
        )

        assert manifest.compose_present is True
        assert manifest.sidecar_networks == (INTERNAL_NETWORK,)
        secret = (destination / "tests" / "fixtures" / "secret_key.txt").read_text(
            encoding="utf-8"
        )
        assert secret == "deadbeef" * 8 + "\n"
        verifier_first = (
            (destination / "tests" / "Dockerfile").read_text(encoding="utf-8").splitlines()[0]
        )
        assert verifier_first.startswith("FROM --platform=linux/amd64 ")
        assert _tree_digest(source) == before


def test_stage_is_deterministic_and_records_no_source_mutation() -> None:
    import tempfile

    from evallab.host_task_staging import stage_task_for_host

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        first = root / "staged-1"
        second = root / "staged-2"
        manifest_one = stage_task_for_host(
            source, first, host_policy=_darwin_policy(), pin_platform=True, attach_agent_egress=True
        )
        manifest_two = stage_task_for_host(
            source, second, host_policy=_darwin_policy(), pin_platform=True, attach_agent_egress=True
        )
        assert manifest_one == manifest_two
        assert (first / "run_manifest.json").read_bytes() == (second / "run_manifest.json").read_bytes()
        assert _tree_digest(first) == _tree_digest(second)


# --------------------------------------------------------------------------
# Staging negative tests
# --------------------------------------------------------------------------


def _stage(*args, **kwargs):  # type: ignore[no-untyped-def]
    from evallab.host_task_staging import stage_task_for_host

    return stage_task_for_host(*args, **kwargs)


def test_stage_refuses_symlinked_source() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_recovery_like(root, with_symlink=True)
        with pytest.raises(ValueError, match="symlink"):
            _stage(source, root / "staged", host_policy=_darwin_policy())


def test_stage_refuses_source_equals_destination() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        with pytest.raises(ValueError, match="onto itself"):
            _stage(source, source, host_policy=_darwin_policy())


def test_stage_refuses_destination_inside_source() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        with pytest.raises(ValueError, match="path escape"):
            _stage(source, source / "nested-staging", host_policy=_darwin_policy())


def test_stage_refuses_pre_existing_destination() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        destination = root / "already-there"
        destination.mkdir()
        with pytest.raises(ValueError, match="already exists"):
            _stage(source, destination, host_policy=_darwin_policy())


def test_stage_refuses_unknown_compose_service() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        compose_path = source / "environment" / "docker-compose.yaml"
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        data["services"]["rogue-proxy"] = {"image": "evil:latest"}
        compose_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown compose services|admits at most"):
            _stage(source, root / "staged", host_policy=_darwin_policy())


def test_stage_refuses_unknown_compose_top_level_key() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        compose_path = source / "environment" / "docker-compose.yaml"
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        data["configs"] = {"x": {"file": "x"}}
        compose_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match="unknown compose top-level keys"):
            _stage(source, root / "staged", host_policy=_darwin_policy())


def test_stage_refuses_existing_compose_platform_pin_without_request() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        compose_path = source / "environment" / "docker-compose.yaml"
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        data["services"]["main"]["platform"] = "linux/amd64"
        compose_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match="pin_platform was not"):
            _stage(source, root / "staged", host_policy=_darwin_policy())


def test_stage_refuses_existing_dockerfile_platform_pin_without_request() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        dockerfile = source / "tests" / "Dockerfile"
        dockerfile.write_text(
            f"FROM --platform=linux/amd64 {PINNED_BASE}\nWORKDIR /app\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="pin_platform was not requested"):
            _stage(source, root / "staged", host_policy=_darwin_policy())


def test_stage_refuses_conflicting_compose_platform_pin() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        compose_path = source / "environment" / "docker-compose.yaml"
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        data["services"]["main"]["platform"] = "linux/arm64"
        compose_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match="already declares platform"):
            _stage(
                source, root / "staged", host_policy=_darwin_policy(), pin_platform=True
            )


def test_stage_refuses_equal_pre_pinned_compose_service_with_request() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        compose_path = source / "environment" / "docker-compose.yaml"
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        # A pin EQUAL to the requested platform must also be refused: the
        # minimality proof cannot distinguish it from a staging-added pin.
        data["services"]["main"]["platform"] = "linux/amd64"
        compose_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match="already declares platform"):
            _stage(
                source, root / "staged", host_policy=_darwin_policy(), pin_platform=True
            )
        assert not (root / "staged").exists()


def test_stage_refuses_equal_pre_pinned_dockerfile_with_request() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        dockerfile = source / "tests" / "Dockerfile"
        dockerfile.write_text(
            f"FROM --platform=linux/amd64 {PINNED_BASE}\nWORKDIR /app\n", encoding="utf-8"
        )
        with pytest.raises(ValueError, match="already declares platform"):
            _stage(
                source, root / "staged", host_policy=_darwin_policy(), pin_platform=True
            )
        assert not (root / "staged").exists()


def test_stage_refuses_symlinked_source_root() -> None:
    """A symlink passed as the SOURCE argument itself must be refused."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_action_memory_like(root)
        link = root / "source-link"
        link.symlink_to(root / "action-memory-like", target_is_directory=True)
        with pytest.raises(ValueError, match="source task directory is a symlink"):
            _stage(link, root / "staged", host_policy=_darwin_policy())
        assert not (root / "staged").exists()


def test_stage_refuses_destination_won_by_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing the destination mkdir race refuses and never deletes the winner."""
    import tempfile

    import evallab.host_task_staging as staging

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        raced = root / "staged"
        raced.mkdir()
        (raced / "not-yours.txt").write_text("another process owns this\n", encoding="utf-8")

        def _race_window(src, dest):  # type: ignore[no-untyped-def]
            # Simulate the destination appearing AFTER validation passed.
            return src, dest

        monkeypatch.setattr(staging, "_assert_source_and_destination", _race_window)
        with pytest.raises(ValueError, match="destination already exists"):
            _stage(source, raced, host_policy=_darwin_policy())
        # The winning directory is untouched.
        assert (raced / "not-yours.txt").read_text(encoding="utf-8") == (
            "another process owns this\n"
        )


def test_stage_cleans_partial_destination_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after the copy removes only the destination this call created."""
    import tempfile

    import evallab.host_task_staging as staging

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        destination = root / "staged"

        def _boom(path):  # type: ignore[no-untyped-def]
            raise RuntimeError("compose exploded")

        monkeypatch.setattr(staging, "_load_compose", _boom)
        with pytest.raises(RuntimeError, match="compose exploded"):
            _stage(source, destination, host_policy=_darwin_policy())
        assert not destination.exists()


def test_stage_rejects_toctou_symlink_appearing_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source symlink created after validation is copied as a link and refused."""
    import tempfile

    import evallab.host_task_staging as staging

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        destination = root / "staged"

        real_assert = staging._assert_source_and_destination

        def _validated_then_plant_symlink(src, dest):  # type: ignore[no-untyped-def]
            resolved_src, resolved_dest = real_assert(src, dest)
            # TOCTOU: plant a symlink in the source after validation.
            (resolved_src / "environment" / "planted-link").symlink_to("/etc/passwd")
            return resolved_src, resolved_dest

        monkeypatch.setattr(
            staging, "_assert_source_and_destination", _validated_then_plant_symlink
        )
        with pytest.raises(ValueError, match="staged task copy contains a symlink"):
            _stage(source, destination, host_policy=_darwin_policy())
        assert not destination.exists()
        # The planted link was never dereferenced: no /etc/passwd bytes copied.


def test_stage_payload_digests_recomputable_from_completed_tree() -> None:
    import tempfile

    from evallab.host_task_staging import stage_task_for_host, task_payload_digest

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        destination = root / "staged"
        manifest = stage_task_for_host(
            source,
            destination,
            host_policy=_darwin_policy(),
            pin_platform=True,
            attach_agent_egress=True,
        )
        # The manifest file exists in the completed staged tree...
        assert (destination / "run_manifest.json").is_file()
        # ...yet the recorded digests recompute exactly from both trees.
        assert manifest.source_payload_digest == task_payload_digest(source)
        assert manifest.staged_payload_digest == task_payload_digest(destination)
        assert manifest.staged_payload_digest != manifest.source_payload_digest


def test_cli_prints_typed_platform_pins() -> None:
    """CLI JSON must carry platform_pins as objects, never stringified records."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        destination = root / "staged"
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "stage_host_task.py"),
                str(source),
                str(destination),
                "--pin-platform",
                "--attach-agent-egress",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
        payload = json.loads(result.stdout)
        assert payload["source_payload_digest"].startswith("sha256:")
        assert payload["staged_payload_digest"].startswith("sha256:")
        assert payload["platform_pins"], "expected platform pins in CLI output"
        for pin in payload["platform_pins"]:
            assert isinstance(pin, dict)
            assert isinstance(pin["target"], str)
            assert pin["platform"] == "linux/amd64"


def test_stage_refuses_egress_when_single_container_network_not_public() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        # Turn the sidecar package into a single-container package.
        (source / "environment" / "docker-compose.yaml").unlink()
        with pytest.raises(ValueError, match="effective network"):
            _stage(
                source,
                root / "staged",
                host_policy=_linux_policy(),
                attach_agent_egress=True,
            )


def test_stage_linux_policy_needs_no_adaptation_for_enforced_package() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _make_action_memory_like(root)
        manifest = _stage(source, root / "staged", host_policy=_linux_policy())
        assert manifest.task_toml_adapted is False
        assert manifest.effective_agent_network == "no-network"
        assert manifest.network_isolation_enforced is True
        assert manifest.platform_pins == ()
        assert manifest.agent_public_egress is False
