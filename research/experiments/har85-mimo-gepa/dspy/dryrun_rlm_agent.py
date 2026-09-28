"""$0-only dry-run agent for the HAR-85 DSPy GEPA arm.

``DryRunRlmAgent`` subclasses the shipped :class:`evallab.harbor_rlm.LabRlmAgent`
and differs in exactly one place: the LM factory returns a
``dspy.utils.DummyLM`` with a fixed two-action script (``ls /app``, then
``SUBMIT``) instead of the paid endpoint. Unlike
``scripted_rlm_agent:ScriptedRlmAgent`` (fixed ``policy=stock``), this agent
honours ``--ak policy=<catalog-id|policy-JSON>`` through the shipped
``resolve_agent_policy``, so a dry-run trial proves the GEPA candidate-policy
transport end to end: the winning instruction file reaches ``LabRlm``, and the
trial metadata records its ``rlm_policy_digest``.

NEVER use with a paid model. NEVER approve spend on trials using this agent.
"""

from __future__ import annotations

from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

import evallab.harbor_rlm as upstream
from evallab.harbor_rlm import LabRlmAgent

try:
    from dspy.utils import DummyLM
except ImportError:  # pragma: no cover - lane venv always has dspy
    DummyLM = None  # type: ignore[assignment, misc]

#: Generic two-action probe valid against any MiMo terminal checkout.
DRYRUN_ANSWERS: list[dict[str, str]] = [
    {
        "reasoning": "Dry-run step 1/2: list the container workdir.",
        "code": "print(exec_command('ls /app'))",
    },
    {
        "reasoning": "Dry-run step 2/2: exploration only; submit a dummy solution.",
        "code": "SUBMIT(solution='har85-dryrun-dummy')",
    },
    # Spare: consumed only if the loop ever reaches the extract fallback.
    {
        "reasoning": "Fallback spare, unused when SUBMIT fires.",
        "code": "SUBMIT(solution='har85-dryrun-dummy-spare')",
    },
]


def build_dryrun_lms(policy, **kwargs):
    """Drop-in for ``evallab.harbor_rlm.build_lms``: $0 DummyLM, no sub-LM."""
    if DummyLM is None:
        raise RuntimeError("dspy is required for the dry-run agent")
    return DummyLM(list(DRYRUN_ANSWERS)), None


class DryRunRlmAgent(LabRlmAgent):
    """LabRlmAgent with $0 scripted LMs and full candidate-policy support."""

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        real_build_lms = upstream.build_lms
        upstream.build_lms = build_dryrun_lms  # type: ignore[assignment]
        try:
            await super().run(instruction, environment, context)
        finally:
            upstream.build_lms = real_build_lms
