"""DeepSeek Harness (``dsh``) as an agent under test.

Harbor has no DeepSeek Harness adapter, and DSH ships no Harbor or
terminal-bench integration, so this module is the join: it installs ``dsh``
inside the task container, runs one headless turn, and turns the session DSH
leaves behind into the ATIF ``agent/trajectory.json`` the rest of this lab
reads.

Two properties of DSH shape the implementation, and both come from its own
source rather than from guesswork:

``dsh --profile headless "<task>"``
    The only non-interactive entry point. It streams reasoning to stderr,
    prints the final assistant message to stdout, and exits 0 only when the
    turn completed. There is no ``--print``/``exec``/``run`` subcommand and no
    JSON output mode, so the session file — not stdout — is the trajectory.

``DSH_PERMISSION_MODE``
    Headless mounts no approval answerer, so with the default ``workspace-write``
    policy an approval-requiring call fails closed (it does not hang, which is
    why an unattended run here is safe by construction). Benchmark work needs
    to write the files it is asked to write, so the adapter boots with
    ``danger-full-access``. That is the documented switch; there is no
    ``--dangerously-skip-permissions`` flag to pass.

Verified against DSH 0.1.5-rc.1 on this workstation: the CLI surface, the
session layout (``$DSH_HOME/sessions/--<cwd>--/<session>/session.v3.jsonl.zstd``),
and the ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL`` credential interface. NOT
verified: a live Harbor trial end to end. The install step in particular is
written from the published package rather than from a container run, so the
first authorized smoke is what proves it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import (  # ty: ignore[unresolved-import]
    BaseInstalledAgent,
    with_prompt_template,
)
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

from evallab.dsh import (
    DSH_AGENT_NAME,
    create_fallback_atif_from_final_message,
    newest_session_file,
    parse_session_to_atif,
    read_session_file,
    sanitize_session,
)

#: The npm package that provides the ``dsh`` binary.
DSH_PACKAGE = "@deepseek-ai/dsh"

#: Node range DSH declares for its own repository (``^22.19.0 || >=24``).
DSH_NODE_MAJOR_MINIMUM = 22

#: Where the agent's durable artifacts are collected inside the container.
AGENT_LOGS = "/logs/agent"

#: DSH state root, deliberately inside the collected logs so the session that
#: carries the trajectory survives the container.
REMOTE_DSH_HOME = f"{AGENT_LOGS}/dsh-home"

REMOTE_FINAL_MESSAGE = f"{AGENT_LOGS}/dsh-final-message.txt"
REMOTE_REASONING = f"{AGENT_LOGS}/dsh-reasoning.txt"
REMOTE_SETTINGS = f"{REMOTE_DSH_HOME}/settings.yaml"

#: The one trajectory file the lab's quality screen and corpus reader look for.
TRAJECTORY_FILE = "trajectory.json"

#: Default provider row DSH resolves when ``settings.yaml`` names no model.
DEFAULT_PROVIDER = "deepseek-official"

#: Harbor's provider namespace is not DSH's. Harbor spells the selector
#: ``deepseek/deepseek-v4-flash``; DSH's provider row is ``deepseek-official``,
#: and writing Harbor's spelling into ``settings.yaml`` would name a provider
#: DSH does not have. Unmapped prefixes pass through so a deliberately
#: configured provider still works.
HARBOR_TO_DSH_PROVIDER: dict[str, str] = {
    "deepseek": DEFAULT_PROVIDER,
    "deepseek-official": DEFAULT_PROVIDER,
}


def build_settings_yaml(*, provider: str, model: str, reasoning_effort: str = "max") -> str:
    """Render the ``$DSH_HOME/settings.yaml`` that pins model and permission.

    DSH's headless profile takes no ``--model`` flag: the model is read from
    this file. Writing it explicitly is what makes a trial's model a recorded
    fact rather than whatever the container's DSH happened to default to.
    """
    return (
        "permission:\n"
        "  defaultPreset: danger-full-access\n"
        "agent-default-model:\n"
        f"  provider: {provider}\n"
        f"  model: {model}\n"
        f"  reasoningEffort: {reasoning_effort}\n"
    )


def split_model_selector(model_name: str | None) -> tuple[str, str]:
    """Split ``provider/model`` into DSH's ``(provider, model)`` pair.

    Harbor hands adapters a single selector. A bare name keeps the default
    provider, matching how the rest of the lab treats an unprefixed model, and
    a Harbor provider prefix is translated into DSH's own provider row.
    """
    if not model_name:
        raise ValueError("deepseek-harness requires a model selector")
    if "/" in model_name:
        provider, model = model_name.split("/", maxsplit=1)
        if provider and model:
            return HARBOR_TO_DSH_PROVIDER.get(provider, provider), model
    return DEFAULT_PROVIDER, model_name


class DeepSeekHarnessAgent(BaseInstalledAgent):
    """Run one headless DeepSeek Harness turn and preserve its session."""

    def __init__(
        self,
        *args: Any,
        permission_mode: str = "danger-full-access",
        reasoning_effort: str = "max",
        dsh_home: str = REMOTE_DSH_HOME,
        **kwargs: Any,
    ) -> None:
        self._permission_mode = permission_mode
        self._reasoning_effort = reasoning_effort
        self._dsh_home = dsh_home
        super().__init__(*args, **kwargs)

    @staticmethod
    @override
    def name() -> str:
        return DSH_AGENT_NAME

    @override
    def get_version_command(self) -> str | None:
        return "dsh --version"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Install ``dsh`` and pin the model before the task starts.

        Node is installed only when the image has none: ``ensure_system_dependencies``
        takes package specs, and the Node distribution differs per base image,
        so this asks the environment's own package manager where it can and
        falls back to NodeSource.
        """
        await self.ensure_system_dependencies(environment, ("curl", "bash", "git"))
        provider, model = split_model_selector(self.model_name)
        settings = build_settings_yaml(
            provider=provider,
            model=model,
            reasoning_effort=self._reasoning_effort,
        )
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                # Node 22.19+ is DSH's own floor. Install only when missing so a
                # base image that already ships a new enough Node is untouched.
                "if ! command -v node >/dev/null 2>&1; then "
                "  (apt-get update && apt-get install -y --no-install-recommends nodejs npm) "
                "  || (apk add --no-cache nodejs npm) "
                "  || (dnf install -y nodejs npm); "
                "fi; "
                f"npm install -g {shlex.quote(DSH_PACKAGE)}; "
                "dsh --version"
            ),
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                f"mkdir -p {shlex.quote(self._dsh_home)}; "
                f"cat > {shlex.quote(REMOTE_SETTINGS)} <<'EVALLAB_DSH_SETTINGS'\n"
                f"{settings}"
                "EVALLAB_DSH_SETTINGS\n"
                f"mkdir -p {shlex.quote(AGENT_LOGS)}; "
                f"test -s {shlex.quote(REMOTE_SETTINGS)}"
            ),
        )

    @override
    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run exactly one headless turn.

        The instruction is quoted, never interpolated, so a task containing
        shell metacharacters cannot become part of the command. stdout is the
        final message and stderr is the reasoning stream; both are kept because
        a trial that dies mid-turn is diagnosed from the reasoning, not the
        answer that never came.
        """
        quoted = shlex.quote(instruction)
        env = {
            "DSH_HOME": self._dsh_home,
            "DSH_PERMISSION_MODE": self._permission_mode,
            # DSH reads these before any config file. When the lab's credential
            # broker is in play the base URL is the internal proxy and the key
            # is a per-trial placeholder, so no provider secret reaches here.
            "DEEPSEEK_API_KEY": "evallab-proxy-placeholder",
        }
        proxy_base_url = _proxy_base_url()
        if proxy_base_url:
            env["DEEPSEEK_BASE_URL"] = proxy_base_url
        command = (
            "set -o pipefail; "
            f"dsh --profile headless {quoted} "
            f"> {shlex.quote(REMOTE_FINAL_MESSAGE)} "
            f"2> {shlex.quote(REMOTE_REASONING)}"
        )
        await self.exec_as_agent(environment, command=command, env=env, timeout_sec=None)

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Convert the session DSH left behind into ATIF.

        Order matters. The session is the real trajectory and is preferred
        whenever it exists; the final message is a one-step fallback that keeps
        the trial countable instead of quarantined. Either way the bytes are
        sanitized before they are written, because a session records the
        arguments of every tool call.
        """
        logs_dir = Path(self.logs_dir)
        trajectory_path = logs_dir / TRAJECTORY_FILE
        job_id, trial_id = _path_identity(logs_dir)
        version = self.version() or "unknown"

        payload = self._payload_from_session(
            job_id=job_id,
            trial_id=trial_id,
            version=version,
        )
        if payload is None:
            payload = self._payload_from_final_message(
                job_id=job_id,
                trial_id=trial_id,
                version=version,
            )
        if payload is None:
            return

        trajectory_path.write_text(json.dumps(payload, indent=2) + "\n")
        final_metrics = payload.get("final_metrics")
        if isinstance(final_metrics, dict):
            context.n_input_tokens = final_metrics.get("total_prompt_tokens")
            context.n_output_tokens = final_metrics.get("total_completion_tokens")
            context.n_cache_tokens = final_metrics.get("total_cached_tokens")

    def _payload_from_session(
        self,
        *,
        job_id: str | None,
        trial_id: str | None,
        version: str,
    ) -> dict[str, Any] | None:
        session_path = newest_session_file(Path(self._dsh_home) / "sessions")
        if session_path is None:
            return None
        # The container wrote this file; a decode failure is a reason to fall
        # back to the final message, not a reason to lose the trial.
        with contextlib.suppress(Exception):
            raw = read_session_file(session_path)
            sanitized = sanitize_session(raw)
            self._persist_sanitized(session_path, sanitized)
            return parse_session_to_atif(
                sanitized,
                agent_version=version,
                model_name=self.model_name,
                raw_source=session_path.name,
                job_id=job_id,
                trial_id=trial_id,
            )
        return None

    def _payload_from_final_message(
        self,
        *,
        job_id: str | None,
        trial_id: str | None,
        version: str,
    ) -> dict[str, Any] | None:
        final_path = Path(REMOTE_FINAL_MESSAGE)
        if not final_path.is_file():
            final_path = Path(self.logs_dir) / Path(REMOTE_FINAL_MESSAGE).name
        if not final_path.is_file():
            return None
        return create_fallback_atif_from_final_message(
            final_path.read_text(errors="replace"),
            agent_version=version,
            model_name=self.model_name,
            raw_source=final_path.name,
            job_id=job_id,
            trial_id=trial_id,
        )

    @staticmethod
    def _persist_sanitized(path: Path, sanitized: str) -> None:
        """Rewrite the session in place so the collected artifact is the clean one."""
        with contextlib.suppress(OSError):
            path.write_text(sanitized)


def _proxy_base_url() -> str | None:
    """Return the lab's internal DeepSeek proxy URL when the broker is running."""
    from evallab.execution_contracts import DEEPSEEK_PROXY_URL

    return DEEPSEEK_PROXY_URL if os.environ.get("EVALLAB_DEEPSEEK_PROXY_CAPABILITY") else None


def _path_identity(logs_dir: Path) -> tuple[str | None, str | None]:
    """Recover ``(job_id, trial_id)`` from the trial's own directory shape."""
    for directory in (logs_dir, *logs_dir.parents):
        if (directory / "lock.json").is_file():
            return directory.parent.name, directory.name
    return None, None
