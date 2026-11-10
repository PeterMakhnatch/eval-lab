"""Antigravity host-to-container auth bridge tests.

One test exercises the installed Harbor adapter through Harbor's own
interpreter. The rest import ``evallab.harbor_antigravity`` in process with a
fake environment, so coverage sees the adapter; they skip when Harbor is absent
and run in CI's native Harbor step. No test starts a real ``agy`` binary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_REMOTE_TOKEN = "$HOME/.gemini/antigravity-cli/antigravity-oauth-token"
_AGY_RUN = "$HOME/.local/bin/agy --dangerously-skip-permissions"
_AGY_MODELS = "$HOME/.local/bin/agy models"


def _harbor_interpreter() -> pathlib.Path | None:
    executable = shutil.which("harbor")
    if not executable:
        return None
    interpreter = pathlib.Path(executable).resolve().parent / "python"
    if not interpreter.exists():
        return None
    try:
        check = subprocess.run(
            [
                str(interpreter),
                "-c",
                "from harbor.models.agent.context import AgentContext",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return interpreter if check.returncode == 0 else None
    except Exception:
        return None


def test_subscription_environment_forces_agy_auth():
    """Even without a pre-set AGY_FORCE_AUTH_JSON, runner.py must opt in to the
    default host token so the installed Antigravity adapter stages it.
    """
    from evallab.runner import subscription_environment

    env = subscription_environment({"HOME": "/home/x"})
    assert env["AGY_FORCE_AUTH_JSON"] == "1"
    assert "AGY_AUTH_JSON_PATH" not in env


def test_antigravity_cli_capture_stages_and_scrubs_token(tmp_path: pathlib.Path) -> None:
    """The repo-owned AntigravityCliCapture stages the host OAuth token where agy
    reads it and scrubs token material from the recorded commands.  No token
    material leaks into command strings.
    """
    interpreter = _harbor_interpreter()
    if interpreter is None:
        pytest.skip("harbor is not installed; cannot test adapter staging")

    token_path = tmp_path / "antigravity-oauth-token"
    fake_secret = "agy-test-refresh-token-12345"
    token_path.write_text(json.dumps({"refresh_token": fake_secret}), encoding="utf-8")
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    script = textwrap.dedent(
        """
        import asyncio
        import json
        import os
        import pathlib
        from types import SimpleNamespace

        from harbor.models.agent.context import AgentContext
        from evallab.harbor_antigravity import AntigravityCliCapture


        class FakeEnv:
            default_user = "runner"

            def __init__(self):
                self.uploaded = []
                self.commands = []

            async def upload_file(self, source, target):
                self.uploaded.append((source, target))

            async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
                self.commands.append({"command": command, "user": user, "env": env})
                if "$HOME/.local/bin/agy" in command:
                    raise RuntimeError("would run agy")
                return SimpleNamespace(return_code=0, stdout="", stderr="")


        async def main():
            logs = pathlib.Path(os.environ["AGY_LOGS_DIR"])
            agent = AntigravityCliCapture(
                logs_dir=logs,
                model_name="google/gemini-3.7-flash-low",
            )
            env = FakeEnv()
            try:
                await agent.run("Reply with exactly: BRIDGE_OK", env, AgentContext())
            except RuntimeError:
                pass
            return {"uploaded": env.uploaded, "commands": [c["command"] for c in env.commands]}


        print(json.dumps(asyncio.run(main())))
        """
    )

    completed = subprocess.run(
        [str(interpreter), "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1] / "src"),
            "AGY_AUTH_JSON_PATH": str(token_path),
            "AGY_LOGS_DIR": str(logs_dir),
        },
    )
    if completed.returncode != 0:
        if "ModuleNotFoundError" in completed.stderr or "No module named" in completed.stderr:
            pytest.skip(f"harbor installation is incomplete: {completed.stderr.strip()}")
        raise AssertionError(
            f"harbor probe failed:\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    result = json.loads(completed.stdout)
    assert [str(token_path), "/tmp/.agy-auth.json"] in result["uploaded"]

    commands = "\\n".join(result["commands"])
    assert "$HOME/.gemini/antigravity-cli/antigravity-oauth-token" in commands
    assert 'mkdir -p "$HOME/.gemini/antigravity-cli"' in commands
    assert "chmod 600" in commands
    assert 'rm -f "$HOME/.gemini/antigravity-cli/antigravity-oauth-token"' in commands
    assert "rm -f /tmp/.agy-auth.json" in commands
    assert fake_secret not in commands
    assert fake_secret not in json.dumps(result)


class _FakeEnvironment:
    """Record uploads and execs; ``handler`` decides each exec's outcome."""

    default_user = "runner"

    def __init__(self, handler: Callable[[str], Any] | None = None) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.commands: list[dict[str, Any]] = []
        self._handler = handler

    async def upload_file(self, source: str, target: str) -> None:
        self.uploaded.append((source, target))

    async def exec(
        self,
        command: str,
        user: str | int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        self.commands.append({"command": command, "env": env, "timeout_sec": timeout_sec})
        if self._handler is not None:
            outcome = self._handler(command)
            if outcome is not None:
                return outcome
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    def ran(self, fragment: str) -> list[str]:
        return [c["command"] for c in self.commands if fragment in c["command"]]


def _listing(*slugs: str) -> SimpleNamespace:
    rows = "\n".join(f"{slug}\t{slug} display" for slug in slugs)
    return SimpleNamespace(return_code=0, stdout=f"Fetching models...\n{rows}\n", stderr="")


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    pytest.importorskip(
        "harbor.agents.installed.antigravity_cli",
        reason="Harbor is not installed; the Antigravity adapter cannot import",
    )
    import evallab.harbor_antigravity as module

    for name in ("AGY_AUTH_JSON_PATH", "AGY_FORCE_AUTH_JSON"):
        monkeypatch.delenv(name, raising=False)
    return module


def _agent(adapter: ModuleType, logs_dir: pathlib.Path, **kwargs: Any) -> Any:
    logs_dir.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("model_name", "google/gemini-3.7-flash-low")
    return adapter.AntigravityCliCapture(logs_dir=logs_dir, **kwargs)


def _token(tmp_path: pathlib.Path) -> pathlib.Path:
    token = tmp_path / "host-token.json"
    token.write_text(json.dumps({"refresh_token": "host-secret"}), encoding="utf-8")
    return token


def test_explicit_token_path_is_staged(
    adapter: ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _token(tmp_path)
    monkeypatch.setenv("AGY_AUTH_JSON_PATH", str(token))
    agent = _agent(adapter, tmp_path / "logs")
    assert agent._host_oauth_token() == token


def test_missing_explicit_token_path_warns_and_stages_nothing(
    adapter: ModuleType,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("AGY_AUTH_JSON_PATH", str(tmp_path / "absent-token"))
    # An explicit path wins over the opt-in default, even when the default exists.
    monkeypatch.setenv("AGY_FORCE_AUTH_JSON", "1")
    home = tmp_path / "home"
    (home / adapter._HOST_TOKEN).parent.mkdir(parents=True)
    (home / adapter._HOST_TOKEN).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    agent = _agent(adapter, tmp_path / "logs")
    with caplog.at_level(logging.WARNING):
        assert agent._host_oauth_token() is None
    assert "AGY_AUTH_JSON_PATH names a missing file" in caplog.text


@pytest.mark.parametrize(
    ("force", "default_exists", "expected"),
    [
        (None, True, False),
        ("no", True, False),
        ("1", False, False),
        (" TRUE ", True, True),
        ("on", True, True),
    ],
)
def test_default_token_is_staged_only_on_opt_in(
    adapter: ModuleType,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    force: str | None,
    default_exists: bool,
    expected: bool,
) -> None:
    home = tmp_path / "home"
    default = home / adapter._HOST_TOKEN
    if default_exists:
        default.parent.mkdir(parents=True)
        default.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    if force is not None:
        monkeypatch.setenv("AGY_FORCE_AUTH_JSON", force)
    agent = _agent(adapter, tmp_path / "logs")
    assert agent._host_oauth_token() == (default if expected else None)


def test_token_is_seeded_once_and_upload_copy_removed(
    adapter: ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _token(tmp_path)
    monkeypatch.setenv("AGY_AUTH_JSON_PATH", str(token))
    agent = _agent(adapter, tmp_path / "logs")
    env = _FakeEnvironment()

    async def seed_twice() -> None:
        await agent._seed_oauth_token(env)
        await agent._seed_oauth_token(env)

    asyncio.run(seed_twice())
    assert env.uploaded == [(str(token), "/tmp/.agy-auth.json")]
    [command] = [c["command"] for c in env.commands]
    assert f'cp /tmp/.agy-auth.json "{_REMOTE_TOKEN}"' in command
    assert f'chmod 600 "{_REMOTE_TOKEN}"' in command
    assert command.endswith("rm -f /tmp/.agy-auth.json")
    assert "host-secret" not in command


def test_staged_token_is_removed_when_agy_run_raises(
    adapter: ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGY_AUTH_JSON_PATH", str(_token(tmp_path)))
    agent = _agent(adapter, tmp_path / "logs")

    def handler(command: str) -> Any:
        if _AGY_RUN in command:
            raise RuntimeError("agy exploded")
        return None

    env = _FakeEnvironment(handler)
    with pytest.raises(RuntimeError, match="agy exploded"):
        asyncio.run(agent.run("do the task", env, SimpleNamespace(metadata=None)))

    commands = [c["command"] for c in env.commands]
    agy_index = next(i for i, c in enumerate(commands) if _AGY_RUN in c)
    assert commands[agy_index + 1 :] == [f'set -o pipefail; rm -f "{_REMOTE_TOKEN}"']


def test_token_cleanup_failure_does_not_mask_the_run_error(
    adapter: ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGY_AUTH_JSON_PATH", str(_token(tmp_path)))
    agent = _agent(adapter, tmp_path / "logs")

    def handler(command: str) -> Any:
        if _AGY_RUN in command:
            raise RuntimeError("agy exploded")
        if f'rm -f "{_REMOTE_TOKEN}"' in command:
            raise OSError("sandbox gone")
        return None

    env = _FakeEnvironment(handler)
    with pytest.raises(RuntimeError, match="agy exploded"):
        asyncio.run(agent.run("do the task", env, SimpleNamespace(metadata=None)))
    assert env.ran(f'rm -f "{_REMOTE_TOKEN}"')


def test_run_without_token_stages_and_removes_nothing(
    adapter: ModuleType, tmp_path: pathlib.Path
) -> None:
    agent = _agent(adapter, tmp_path / "logs", skills_dir="/skills")
    env = _FakeEnvironment(lambda c: _listing("gemini-3.7-flash-low") if _AGY_MODELS in c else None)
    asyncio.run(agent.run("say 'hi'", env, SimpleNamespace(metadata=None)))

    assert env.uploaded == []
    assert not env.ran(_REMOTE_TOKEN)
    assert env.ran("cp -r /skills/* ~/.gemini/antigravity-cli/skills/")
    [run] = [c for c in env.commands if _AGY_RUN in c["command"]]
    assert run["env"]["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert "--model gemini-3.7-flash-low " in run["command"]
    assert "--prompt='say '\"'\"'hi'\"'\"''" in run["command"]
    assert "| python3 -c " in run["command"]
    assert "> /logs/agent/antigravity-cli.stream.jsonl" in run["command"]
    [listing] = [c for c in env.commands if _AGY_MODELS in c["command"]]
    assert listing["timeout_sec"] == agent._MODEL_LIST_TIMEOUT_SEC


@pytest.mark.parametrize("model_name", ["gemini-3.7-flash-low", None])
def test_run_rejects_model_without_provider_prefix(
    adapter: ModuleType, tmp_path: pathlib.Path, model_name: str | None
) -> None:
    agent = _agent(adapter, tmp_path / "logs", model_name=model_name)
    env = _FakeEnvironment()
    with pytest.raises(ValueError, match="provider/model_name"):
        asyncio.run(agent.run("task", env, SimpleNamespace(metadata=None)))
    assert env.commands == [] and env.uploaded == []


@pytest.mark.parametrize(
    ("listing", "model", "register"),
    [
        (_listing("gemini-3.7-flash-low", "gemini-3.7-pro"), "gemini-3.7-pro", False),
        # The gate also accepts a bare base name of an effort-suffixed slug.
        (_listing("gemini-3.7-flash-low"), "gemini-3.7-flash", False),
        (_listing("gemini-3.7-flash-low"), "custom-tuned-model", True),
        # An unparseable or empty listing registers the model.
        (SimpleNamespace(return_code=0, stdout=None, stderr=""), "gemini-3.7-pro", True),
    ],
)
def test_model_is_registered_only_when_agy_does_not_list_it(
    adapter: ModuleType,
    tmp_path: pathlib.Path,
    listing: SimpleNamespace,
    model: str,
    register: bool,
) -> None:
    agent = _agent(adapter, tmp_path / "logs")
    env = _FakeEnvironment(lambda c: listing)
    decision = asyncio.run(agent._register_model_only_when_unknown(env, model, {"K": "v"}))
    assert decision is register
    assert env.commands[0]["env"] == {"K": "v"}
    assert env.commands[0]["command"].endswith("agy models </dev/null")


def test_failed_model_listing_registers_the_model(
    adapter: ModuleType, tmp_path: pathlib.Path
) -> None:
    agent = _agent(adapter, tmp_path / "logs")

    def handler(command: str) -> Any:
        raise TimeoutError("agy models hung")

    env = _FakeEnvironment(handler)
    assert asyncio.run(agent._register_model_only_when_unknown(env, "gemini-3.7-pro", {})) is True


@pytest.mark.parametrize(
    ("model_name", "registered"),
    [("google/gemini-3.7-pro", False), ("google/custom-tuned-model", True)],
)
def test_run_writes_settings_from_the_registration_choice(
    adapter: ModuleType, tmp_path: pathlib.Path, model_name: str, registered: bool
) -> None:
    agent = _agent(adapter, tmp_path / "logs", model_name=model_name)
    env = _FakeEnvironment(lambda c: _listing("gemini-3.7-pro") if _AGY_MODELS in c else None)
    asyncio.run(agent.run("task", env, SimpleNamespace(metadata=None)))
    [settings] = env.ran("antigravity-cli/settings.json")
    assert ("customModelsConfig" in settings) is registered


def test_install_adds_python3_for_the_stream_sanitizer(
    adapter: ModuleType, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    async def upstream_install(self: Any, environment: Any) -> None:
        calls.append("upstream")

    async def ensure(self: Any, environment: Any, dependencies: tuple[str, ...]) -> None:
        calls.append(dependencies)

    monkeypatch.setattr(adapter.AntigravityCli, "install", upstream_install)
    monkeypatch.setattr(adapter.AntigravityCli, "ensure_system_dependencies", ensure)
    asyncio.run(_agent(adapter, tmp_path / "logs").install(_FakeEnvironment()))
    assert calls == ["upstream", ("python3",)]


def test_stream_sanitizer_script_redacts_credentials(adapter: ModuleType) -> None:
    lines = [
        json.dumps(
            {
                "event": "init",
                "access_token": "tok-1",
                "nested": {"api_key": "key-1", "API-KEY": "key-2", "Authorization": "x"},
                "items": [{"refresh_token": "tok-2"}, "Bearer abc.DEF-123/+="],
                "text": "curl -H 'Authorization: bearer s3cr3t.tok' https://x",
                "count": 3,
            }
        ),
        "agy: not json at all",
        json.dumps(["a", "list", "record"]),
        # Integer usage counts survive; a string under a count-shaped key does not.
        json.dumps(
            {
                "event": "result",
                "response": "plain text stays",
                "usage": {"input_tokens": 7, "totalTokens": 9, "tokens": "tok-3", "id_token": 4},
            }
        ),
    ]
    completed = subprocess.run(
        [sys.executable, "-c", adapter.AntigravityCliCapture._stream_sanitizer()],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    assert records == [
        {
            "access_token": "<redacted>",
            "count": 3,
            "event": "init",
            "items": [{"refresh_token": "<redacted>"}, "Bearer <redacted>"],
            "nested": {
                "API-KEY": "<redacted>",
                "Authorization": "<redacted>",
                "api_key": "<redacted>",
            },
            "text": "curl -H 'Authorization: Bearer <redacted>' https://x",
        },
        {
            "event": "result",
            "response": "plain text stays",
            "usage": {
                "id_token": "<redacted>",
                "input_tokens": 7,
                "tokens": "<redacted>",
                "totalTokens": 9,
            },
        },
    ]
    for secret in ("tok-1", "tok-2", "tok-3", "key-1", "key-2", "abc.DEF", "s3cr3t"):
        assert secret not in completed.stdout


def _stream_fixture(*, usage: bool = True) -> str:
    result: dict[str, Any] = {"conversation_id": "agy-1", "status": "SUCCESS", "response": "Done."}
    if usage:
        result["usage"] = {"input_tokens": 12, "output_tokens": 5, "cache_read_tokens": 3}
    records = [
        {"event": "init", "init": {"conversation_id": "agy-1", "model": "gemini-3.7-flash-low"}},
        {
            "event": "step_update",
            "step_update": {
                "step_index": 0,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": "Working. Authorization: Bearer leaked.token",
                "api_key": "leaked-key",
            },
        },
        {"event": "result", "result": result},
    ]
    return "\n".join(json.dumps(record) for record in records) + "\nstderr noise\n"


def test_post_run_writes_trajectory_and_token_counts(
    adapter: ModuleType, tmp_path: pathlib.Path
) -> None:
    trial_dir = tmp_path / "job-7" / "trial-3"
    trial_dir.mkdir(parents=True)
    (trial_dir / "lock.json").write_text("{}", encoding="utf-8")
    agent = _agent(adapter, trial_dir / "agent")
    stream = agent.logs_dir / "antigravity-cli.stream.jsonl"
    stream.write_text(_stream_fixture(), encoding="utf-8")
    context = SimpleNamespace(
        metadata={"trial_id": "meta-trial"},
        n_input_tokens=None,
        n_output_tokens=None,
        n_cache_tokens=None,
    )

    agent.populate_context_post_run(context)

    assert (context.n_input_tokens, context.n_output_tokens, context.n_cache_tokens) == (12, 5, 3)
    rewritten = stream.read_text(encoding="utf-8")
    assert "stderr noise" not in rewritten
    assert "leaked" not in rewritten
    trajectory = json.loads((agent.logs_dir / "trajectory.json").read_text(encoding="utf-8"))
    assert "leaked" not in json.dumps(trajectory)
    assert trajectory["session_id"] == "agy-1"
    assert trajectory["extra"]["raw_source"] == "antigravity-cli.stream.jsonl"
    # Metadata wins over the lock.json path identity; the path fills the gap.
    assert trajectory["extra"]["identity"]["job_id"] == "job-7"
    assert trajectory["extra"]["identity"]["trial_id"] == "meta-trial"
    assert trajectory["agent"] == {
        "name": "antigravity-cli",
        "version": "unknown",
        "model_name": "google/gemini-3.7-flash-low",
    }
    assert trajectory["final_metrics"]["total_prompt_tokens"] == 12


def test_post_run_without_usage_keeps_token_counts_unset(
    adapter: ModuleType, tmp_path: pathlib.Path
) -> None:
    agent = _agent(adapter, tmp_path / "logs")
    (agent.logs_dir / "antigravity-cli.stream.jsonl").write_text(
        _stream_fixture(usage=False), encoding="utf-8"
    )
    context = SimpleNamespace(metadata="not-a-dict", n_input_tokens=None)
    agent.populate_context_post_run(context)
    trajectory = json.loads((agent.logs_dir / "trajectory.json").read_text(encoding="utf-8"))
    assert "final_metrics" not in trajectory
    assert trajectory["extra"]["identity"]["job_id"] == "unknown"
    assert context.n_input_tokens is None


def test_post_run_without_stream_file_writes_nothing(
    adapter: ModuleType, tmp_path: pathlib.Path
) -> None:
    agent = _agent(adapter, tmp_path / "logs")
    context = SimpleNamespace(metadata={}, n_input_tokens=None)
    agent.populate_context_post_run(context)
    assert list(agent.logs_dir.iterdir()) == []
    assert context.n_input_tokens is None


def test_post_run_without_steps_writes_no_trajectory(
    adapter: ModuleType, tmp_path: pathlib.Path
) -> None:
    agent = _agent(adapter, tmp_path / "logs")
    stream = agent.logs_dir / "antigravity-cli.stream.jsonl"
    stream.write_text('{"event": "init", "token": "t"}\nnoise\n', encoding="utf-8")
    context = SimpleNamespace(metadata={}, n_input_tokens=None)
    agent.populate_context_post_run(context)
    assert not (agent.logs_dir / "trajectory.json").exists()
    assert "noise" not in stream.read_text(encoding="utf-8")
    assert context.n_input_tokens is None


def test_path_identity_reads_the_trial_with_lock_json(
    adapter: ModuleType, tmp_path: pathlib.Path
) -> None:
    trial_dir = tmp_path / "jobs" / "job-a" / "trial-b"
    (trial_dir / "agent" / "deep").mkdir(parents=True)
    (trial_dir / "lock.json").write_text("{}", encoding="utf-8")
    assert adapter._path_identity(trial_dir / "agent" / "deep") == ("job-a", "trial-b")
    assert adapter._path_identity(tmp_path / "elsewhere") == (None, None)
