"""Lab-owned Harbor agent running dspy.RLM under a named harness policy.

Why this exists: Harbor 0.21.0 (and upstream ``main`` as of 2026-09-16) builds
``dspy.RLM(..., max_iterations=...)`` while dspy 3.3.1 takes ``max_iters``, so
the registered ``dspy-rlm`` agent raises ``TypeError`` before its first model
call. This adapter keeps Harbor's environment tool bridge (the part that works)
and replaces the RLM construction with ``evallab.rlm.harness.LabRlm`` so every
trial records exactly which policy ran.

Credential transport: the runner materialises the Z.ai coding-plan key into a
0400 file and passes only its path as ``EVALLAB_ZAI_SECRET_FILE``. The key is
read host-side, handed to the litellm client in memory, and never written to
the trial directory; the runner's redacting log writer masks the value should
any library echo it.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
from pathlib import Path
from typing import Any, override

from harbor.agents.base import BaseAgent  # ty: ignore[unresolved-import]
from harbor.agents.dspy_rlm import (  # ty: ignore[unresolved-import]
    EnvironmentToolBridge,
    _format_exec_result,
)
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

from evallab.execution_contracts import ZAI_SECRET_FILE_ENV, read_owner_secret_file
from evallab.rlm.harness import (
    LabRlm,
    RlmRunResult,
    build_lms,
    run_rlm,
    zai_model_id,
)
from evallab.rlm.policies import RlmPolicy, resolve_policy

ADAPTER_VERSION = "0.1.0"
AGENT_NAME = "rlm"
SIGNATURE = "instruction, file_tree -> solution"
FILE_TREE_COMMAND = "find . -maxdepth 3 -type f | head -200"


def provider_key_from_environment(environment: dict[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    secret_file = source.get(ZAI_SECRET_FILE_ENV)
    if not secret_file:
        raise RuntimeError(
            f"{ZAI_SECRET_FILE_ENV} is unset; the rlm lane requires the runner's secret file"
        )
    return read_owner_secret_file(Path(secret_file))


class ContainerPythonBridge(EnvironmentToolBridge):
    """Environment tool bridge plus ``run_python`` executed inside the task container."""

    def run_python(self, code: str, cwd: str | None = None) -> str:
        """Run a complete Python 3 script inside the task environment (not the sandbox). Returns stdout+stderr."""
        escaped = code.replace("'", "'\\''")
        return _format_exec_result(
            self._exec(f"printf '%s' '{escaped}' | python3 -", cwd),
            empty_msg="(no output)",
        )

    def get_tools(self) -> list[Any]:
        return [*super().get_tools(), self.run_python]


class LabRlmAgent(BaseAgent):
    """Harbor agent: dspy.RLM (host-side) + Harbor environment tools, under a policy."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        policy: str = "stock",
        cost_limit_usd: float = 1.0,
        tool_timeout_sec: int = 30,
        working_dir: str = "/",
        verbose: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if not model_name:
            raise ValueError("rlm agent requires an explicit model selector")
        self._policy: RlmPolicy = resolve_policy(policy)
        self._cost_limit_usd = float(cost_limit_usd)
        if self._cost_limit_usd <= 0:
            raise ValueError("cost_limit_usd must be positive")
        self._tool_timeout_sec = int(tool_timeout_sec)
        self._working_dir = working_dir
        self._verbose = verbose

    @staticmethod
    @override
    def name() -> str:
        return AGENT_NAME

    @override
    def version(self) -> str | None:
        import dspy

        return f"{ADAPTER_VERSION}+dspy-{dspy.__version__}"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        """RLM runs host-side; nothing to install in the container."""

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        loop = asyncio.get_running_loop()
        bridge_cls = ContainerPythonBridge if self._policy.container_python_tool else EnvironmentToolBridge
        bridge = bridge_cls(
            environment=environment,
            loop=loop,
            cwd=self._working_dir,
            timeout_sec=self._tool_timeout_sec,
        )
        tree_result = await environment.exec(
            command=FILE_TREE_COMMAND, cwd=self._working_dir, timeout_sec=15
        )
        file_tree = tree_result.stdout or "(empty)"

        key = provider_key_from_environment()
        root_lm, sub_lm = build_lms(
            self._policy, model_id=zai_model_id(self.model_name or ""), api_key=key
        )
        del key
        rlm = LabRlm(
            SIGNATURE,
            self._policy,
            tools=bridge.get_tools(),
            sub_lm=sub_lm,
            root_lm=root_lm,
            cost_limit_usd=self._cost_limit_usd,
            verbose=self._verbose,
        )
        self._write_policy_record()
        execute = functools.partial(
            run_rlm,
            rlm,
            root_lm,
            sub_lm,
            instruction=instruction,
            file_tree=file_tree,
        )
        result: RlmRunResult | None = None
        try:
            result = await loop.run_in_executor(None, execute)
        finally:
            if result is not None:
                self._save_logs(result)
                self._populate_context(context, result)
        if result is not None and result.error:
            raise RuntimeError(f"rlm trajectory failed: {result.error}")

    # -- artefacts ------------------------------------------------------------

    def _rlm_dir(self) -> Path:
        path = self.logs_dir / "rlm"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_policy_record(self) -> None:
        record = {
            "adapter_version": ADAPTER_VERSION,
            "policy": self._policy.to_json(),
            "policy_digest": self._policy.digest(),
            "cost_limit_usd": self._cost_limit_usd,
            "model": self.model_name,
            "signature": SIGNATURE,
        }
        (self._rlm_dir() / "policy.json").write_text(json.dumps(record, indent=2))

    def _save_logs(self, result: RlmRunResult) -> None:
        rlm_dir = self._rlm_dir()
        (rlm_dir / "solution.txt").write_text(str(result.outputs.get("solution", "")))
        (rlm_dir / "trajectory.json").write_text(
            json.dumps(result.trajectory, indent=2, default=str)
        )
        (rlm_dir / "final_reasoning.txt").write_text(result.final_reasoning)
        (rlm_dir / "usage.json").write_text(json.dumps(result.to_json(), indent=2))

    def _populate_context(self, context: AgentContext, result: RlmRunResult) -> None:
        context.n_input_tokens = result.root_usage.input_tokens + result.sub_usage.input_tokens
        context.n_output_tokens = result.root_usage.output_tokens + result.sub_usage.output_tokens
        context.cost_usd = result.cost_usd
        context.metadata = context.metadata or {}
        context.metadata.update(
            {
                "rlm_policy": self._policy.policy_id,
                "rlm_policy_digest": self._policy.digest(),
                "rlm_trajectory_steps": result.iterations,
                "rlm_root_calls": result.root_usage.calls,
                "rlm_sub_calls": result.sub_usage.calls,
                "rlm_budget_stopped": result.budget_stopped,
                "rlm_parse_failures": result.parse_failures,
                "rlm_error": result.error,
            }
        )
