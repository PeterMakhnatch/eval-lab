"""Antigravity host-to-container auth bridge tests.

These tests exercise the installed Harbor adapter through its own interpreter,
not the pytest venv, because Harbor is a standalone CLI tool rather than a
package dependency of eval-lab.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import textwrap

import pytest


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
    """The repo-owned AntigravityCliCapture reuses the installed adapter's OAuth
    token staging and scrub.  No token material leaks into command strings.
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
            await agent._seed_oauth_token(env)
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
    assert fake_secret not in commands
    assert fake_secret not in json.dumps(result)
