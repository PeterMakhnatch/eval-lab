from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

FIXTURE = Path(__file__).resolve().parent / "fixtures/dsh/session.v3.jsonl"
SECRET_SENTINEL = "sk-live-must-not-reach-evidence"


class _Context:
    def __init__(self) -> None:
        self.n_input_tokens: int | None = None
        self.n_output_tokens: int | None = None
        self.n_cache_tokens: int | None = None


class _BaseInstalledAgent:
    """Stand-in for Harbor's base so the adapter is testable without Docker."""

    def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any) -> None:
        del kwargs
        self.logs_dir = Path(logs_dir)
        self.model_name = model_name
        self.root_calls: list[str] = []
        self.agent_calls: list[tuple[str, dict[str, str] | None]] = []
        self.dependencies: tuple[str, ...] = ()

    @staticmethod
    def name() -> str:
        return "stub"

    def version(self) -> str | None:
        return "0.1.5-rc.1"

    async def ensure_system_dependencies(
        self, environment: Any, names: tuple[str, ...]
    ) -> None:
        del environment
        self.dependencies = names

    async def exec_as_root(self, environment: Any, command: str, **kwargs: Any) -> str:
        del environment, kwargs
        self.root_calls.append(command)
        return "ok"

    async def exec_as_agent(
        self,
        environment: Any,
        command: str,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> str:
        del environment, kwargs
        self.agent_calls.append((command, env))
        return "ok"


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _package(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []  # type: ignore[attr-defined]
    return module


def _identity_decorator(function: Any) -> Any:
    return function


@pytest.fixture
def adapter_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    for name in (
        "harbor",
        "harbor.agents",
        "harbor.agents.installed",
        "harbor.environments",
        "harbor.models",
        "harbor.models.agent",
    ):
        monkeypatch.setitem(sys.modules, name, _package(name))
    monkeypatch.setitem(
        sys.modules,
        "harbor.agents.installed.base",
        _module(
            "harbor.agents.installed.base",
            BaseInstalledAgent=_BaseInstalledAgent,
            with_prompt_template=_identity_decorator,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.environments.base",
        _module("harbor.environments.base", BaseEnvironment=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "harbor.models.agent.context",
        _module("harbor.models.agent.context", AgentContext=_Context),
    )
    sys.modules.pop("evallab.harbor_dsh", None)
    try:
        yield importlib.import_module("evallab.harbor_dsh")
    finally:
        sys.modules.pop("evallab.harbor_dsh", None)


def _session_home(tmp_path: Path, text: str | None = None) -> Path:
    """Lay out a DSH home containing one session, as a container would."""
    home = tmp_path / "dsh-home"
    session_dir = home / "sessions/--workspace--/session-0f1e2d3c"
    session_dir.mkdir(parents=True)
    (session_dir / "session.v3.jsonl").write_text(
        text if text is not None else FIXTURE.read_text(encoding="utf-8")
    )
    return home


def test_settings_pin_both_the_model_and_the_permission_preset(adapter_module: ModuleType) -> None:
    settings = adapter_module.build_settings_yaml(
        provider="deepseek-official", model="deepseek-v4-flash"
    )

    assert "defaultPreset: danger-full-access" in settings
    assert "provider: deepseek-official" in settings
    assert "model: deepseek-v4-flash" in settings


def test_model_selector_splits_into_dsh_provider_and_model(adapter_module: ModuleType) -> None:
    assert adapter_module.split_model_selector("deepseek/deepseek-v4-flash") == (
        "deepseek-official",
        "deepseek-v4-flash",
    )
    assert adapter_module.split_model_selector("deepseek-v4-pro") == (
        "deepseek-official",
        "deepseek-v4-pro",
    )
    with pytest.raises(ValueError):
        adapter_module.split_model_selector(None)


def test_agent_reports_the_registered_name(adapter_module: ModuleType, tmp_path: Path) -> None:
    agent = adapter_module.DeepSeekHarnessAgent(logs_dir=tmp_path, model_name="deepseek/x")

    assert agent.name() == "deepseek-harness"
    assert agent.get_version_command() == "dsh --version"


def test_install_installs_the_package_and_writes_settings(
    adapter_module: ModuleType, tmp_path: Path
) -> None:
    import asyncio

    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path,
        model_name="deepseek/deepseek-v4-flash",
        dsh_home=str(tmp_path / "home"),
    )

    asyncio.run(agent.install(object()))

    assert "npm install -g @deepseek-ai/dsh" in agent.root_calls[0]
    settings_call = agent.agent_calls[0][0]
    assert "settings.yaml" in settings_call
    assert "model: deepseek-v4-flash" in settings_call
    assert agent.dependencies == ("curl", "bash", "git")


def test_run_quotes_the_instruction_and_boots_with_full_access(
    adapter_module: ModuleType, tmp_path: Path
) -> None:
    import asyncio

    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path,
        model_name="deepseek/deepseek-v4-flash",
        dsh_home="/logs/agent/dsh-home",
    )
    # A task that would run `rm -rf /` if the instruction were interpolated.
    hostile = "fix it; rm -rf / # `whoami` $(id)"

    asyncio.run(agent.run(hostile, object(), _Context()))

    command, env = agent.agent_calls[0]
    assert command.count("--profile headless") == 1
    # The hostile text is carried, but as one quoted word rather than as syntax.
    assert hostile in command
    assert f"'{hostile}'" in command or f'"{hostile}"' in command
    assert env is not None
    assert env["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert env["DSH_HOME"] == "/logs/agent/dsh-home"
    assert env["DEEPSEEK_API_KEY"] and "sk-" not in env["DEEPSEEK_API_KEY"]


def test_run_command_survives_a_static_shell_parse(
    adapter_module: ModuleType, tmp_path: Path
) -> None:
    """The instruction must arrive as one argv word, not as shell syntax."""
    import asyncio
    import shlex

    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path, model_name="deepseek/deepseek-v4-flash"
    )
    hostile = "run `touch /tmp/pwned` and $(touch /tmp/pwned2)"

    asyncio.run(agent.run(hostile, object(), _Context()))
    command, _env = agent.agent_calls[0]

    tokens = shlex.split(command)
    assert hostile in tokens
    assert "`touch /tmp/pwned`" not in " ".join(
        token for token in tokens if token != hostile
    )


def test_populate_prefers_the_session_over_the_final_message(
    adapter_module: ModuleType, tmp_path: Path
) -> None:
    home = _session_home(tmp_path)
    (tmp_path / "dsh-final-message.txt").write_text("only the last line\n")
    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path, model_name="deepseek/deepseek-v4-flash", dsh_home=str(home)
    )
    context = _Context()

    agent.populate_context_post_run(context)

    payload = json.loads((tmp_path / "trajectory.json").read_text())
    assert payload["extra"]["transport"] == "session.v3.jsonl"
    assert len(payload["steps"]) > 1
    assert context.n_input_tokens == 4050
    assert context.n_output_tokens == 180


def test_populate_falls_back_to_the_final_message_and_says_so(
    adapter_module: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "dsh-final-message.txt").write_text("42 events.\n")
    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path,
        model_name="deepseek/deepseek-v4-flash",
        dsh_home=str(tmp_path / "empty-home"),
    )

    agent.populate_context_post_run(_Context())

    payload = json.loads((tmp_path / "trajectory.json").read_text())
    assert payload["extra"]["degraded"] is True
    assert payload["steps"][0]["message"] == "42 events."


def test_populate_writes_nothing_when_there_is_nothing_to_read(
    adapter_module: ModuleType, tmp_path: Path
) -> None:
    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path,
        model_name="deepseek/deepseek-v4-flash",
        dsh_home=str(tmp_path / "empty-home"),
    )

    agent.populate_context_post_run(_Context())

    assert not (tmp_path / "trajectory.json").exists()


def test_secrets_never_reach_the_written_trajectory(
    adapter_module: ModuleType, tmp_path: Path
) -> None:
    """A key that a tool call read must not survive into durable evidence."""
    poisoned = FIXTURE.read_text(encoding="utf-8") + json.dumps(
        {
            "type": "tool/call",
            "seq": 99,
            "time": "2026-01-01T00:00:10.000Z",
            "data": {
                "turn": 2,
                "step": 1,
                "callId": "call-secret",
                "name": "bash",
                "arguments": json.dumps({"apiKey": SECRET_SENTINEL}),
            },
        }
    )
    home = _session_home(tmp_path, poisoned)
    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path, model_name="deepseek/deepseek-v4-flash", dsh_home=str(home)
    )

    agent.populate_context_post_run(_Context())

    trajectory_text = (tmp_path / "trajectory.json").read_text()
    assert SECRET_SENTINEL not in trajectory_text
    # The collected session is rewritten too, not just the projection.
    session_file = next(home.rglob("session.v3.jsonl"))
    assert SECRET_SENTINEL not in session_file.read_text()


def test_unreadable_session_falls_back_instead_of_losing_the_trial(
    adapter_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _session_home(tmp_path)
    (tmp_path / "dsh-final-message.txt").write_text("recovered answer\n")
    agent = adapter_module.DeepSeekHarnessAgent(
        logs_dir=tmp_path, model_name="deepseek/deepseek-v4-flash", dsh_home=str(home)
    )

    def explode(_path: Path) -> str:
        raise RuntimeError("decoder unavailable")

    monkeypatch.setattr(adapter_module, "read_session_file", explode)

    agent.populate_context_post_run(_Context())

    payload = json.loads((tmp_path / "trajectory.json").read_text())
    assert payload["extra"]["degraded"] is True
    assert payload["steps"][0]["message"] == "recovered answer"
