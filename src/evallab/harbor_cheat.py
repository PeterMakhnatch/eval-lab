"""Deterministic model-free Harbor cheat-audit agent (HAR-204).

``CheatAgent`` runs outside the task sandbox and drives the task through
Harbor tool transport (``environment.exec`` only: no uploads, no model calls,
no provider credentials). It runs the fixed attack ladder owned by
``evallab.cheat_ladder`` (recon plus workspace plants, then grader-tamper
probes), writes each attempt to ``<logs>/cheat/attempts.json``, and leaves
exploitability to the benchmark's own verifier reward, read after the trial
by ``evallab cheat run`` (see ``evallab/cheat.py``).

The ladder names are re-exported here so existing agent-side importers keep
working; the single implementation lives in ``evallab.cheat_ladder``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent  # ty: ignore[unresolved-import]
from harbor.agents.capabilities import AgentCapabilities  # ty: ignore[unresolved-import]
from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

from evallab.cheat_ladder import (
    _STATUS_EXECUTED,
    ATTACKS,
    ExecOutcome,
    RunFn,
    _guided_apply_script,
    attack_answer_symlink,
    attack_git_history,
    attack_reward_plant,
    attack_skip_plant,
    attack_tamper_reward_writer,
    attack_tamper_shadow_timeout,
    attack_tamper_sitecustomize,
    attack_tamper_source_exit,
    attack_tamper_source_pytest_patch,
    attack_tamper_source_skip,
    attack_tamper_source_skiptest,
    attack_tamper_tracked_conftest,
    attack_upstream_fetch,
    attack_verifier_peek,
    run_ladder,
    selected_attacks,
)
from evallab.execution_contracts import CHEAT_AGENT, CHEAT_ATTACKS_ENV_VAR

__all__ = [
    "ATTACKS",
    "CheatAgent",
    "ExecOutcome",
    "RunFn",
    "_guided_apply_script",
    "attack_answer_symlink",
    "attack_git_history",
    "attack_reward_plant",
    "attack_skip_plant",
    "attack_tamper_reward_writer",
    "attack_tamper_shadow_timeout",
    "attack_tamper_sitecustomize",
    "attack_tamper_source_exit",
    "attack_tamper_source_pytest_patch",
    "attack_tamper_source_skip",
    "attack_tamper_source_skiptest",
    "attack_tamper_tracked_conftest",
    "attack_upstream_fetch",
    "attack_verifier_peek",
    "run_ladder",
    "selected_attacks",
]

CHEAT_AGENT_VERSION = "1.4.0"
ATTEMPTS_SCHEMA = "evallab.cheat.attempts/v1"


class CheatAgent(BaseAgent):
    """Deterministic model-free Harbor agent running the fixed cheat ladder."""

    capabilities = AgentCapabilities()

    def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any):
        if model_name:
            raise ValueError("the cheat agent is model-free and does not accept a model")
        super().__init__(logs_dir=logs_dir, model_name=None, **kwargs)

    @staticmethod
    def name() -> str:
        return CHEAT_AGENT

    def version(self) -> str | None:
        return CHEAT_AGENT_VERSION

    async def setup(self, environment: BaseEnvironment) -> None:
        """Nothing to install: the agent runs host-side on tool transport."""

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        del instruction
        cwd_result = await environment.exec("pwd", timeout_sec=30)
        if (
            cwd_result.return_code != 0
            or not cwd_result.stdout
            or not cwd_result.stdout.strip().startswith("/")
        ):
            raise RuntimeError("cannot determine cheat task working directory")
        cwd = cwd_result.stdout.strip()
        raw = self._get_env(CHEAT_ATTACKS_ENV_VAR)
        if raw is None:
            raw = os.environ.get(CHEAT_ATTACKS_ENV_VAR)
        try:
            selected = selected_attacks(raw)
        except ValueError as error:
            raise RuntimeError(f"invalid {CHEAT_ATTACKS_ENV_VAR}: {error}") from error

        evidence_dir = self.logs_dir / "cheat"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        async def run_fn(script: str, timeout_sec: int) -> ExecOutcome:
            result = await environment.exec(command=script, cwd=cwd, timeout_sec=timeout_sec)
            return ExecOutcome(
                return_code=result.return_code,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

        records = await run_ladder(cwd, run_fn, evidence_dir, selected)
        attempts = {
            "schema": ATTEMPTS_SCHEMA,
            "agent": CHEAT_AGENT,
            "version": CHEAT_AGENT_VERSION,
            "cwd": cwd,
            "attacks": records,
        }
        (evidence_dir / "attempts.json").write_text(
            json.dumps(attempts, indent=2) + "\n", encoding="utf-8"
        )
        executed = [record["name"] for record in records if record["status"] == _STATUS_EXECUTED]
        context.n_input_tokens = 0
        context.n_output_tokens = 0
        context.cost_usd = 0.0
        context.metadata = {
            "cheat_version": CHEAT_AGENT_VERSION,
            "cheat_executed": executed,
            "cheat_statuses": {record["name"]: record["status"] for record in records},
            "model_free": True,
        }
