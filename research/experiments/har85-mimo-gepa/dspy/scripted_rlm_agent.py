"""$0-only feasibility agent for HAR-85 (DSPy arm).

``ScriptedRlmAgent`` subclasses the shipped :class:`evallab.harbor_rlm.LabRlmAgent`
and overrides ONLY the language-model construction: instead of ``build_lms``
(paid Z.ai endpoint) it returns a ``dspy.utils.DummyLM`` with a fixed,
three-action script (``ls /app``, peek at the probe script, ``SUBMIT`` a dummy
solution). Everything else -- container startup, ``[environment.healthcheck]``
setup, the Harbor tool bridge, the ``LabRlm`` loop, the MiMo verifier, and the
trial layout -- is the shipped path unchanged.

NEVER use with a paid model. NEVER approve spend on trials using this agent.
The ``model_name`` Harbor requires is a billing-inert label (``scripted-dummy``);
the runner still requires ``EVALLAB_ZAI_SECRET_FILE`` to point at an owner-only
(0400/0600) file, for which any dummy content suffices because it is never used
to call anything.
"""

from __future__ import annotations

from typing import Any

from harbor.environments.base import BaseEnvironment  # ty: ignore[unresolved-import]
from harbor.models.agent.context import AgentContext  # ty: ignore[unresolved-import]

import evallab.harbor_rlm as upstream
from evallab.harbor_rlm import LabRlmAgent

try:
    from dspy.utils import DummyLM
except ImportError:  # pragma: no cover - lane venv always has dspy
    DummyLM = None  # type: ignore[assignment, misc]

#: Fixed $0 action script. Codes run against the REAL task container through
#: the REAL bridge tools (``exec_command`` / ``read_file``); the final
#: ``SUBMIT`` ends the RLM loop with a dummy solution so the verifier scores
#: the untouched checkout (expected reward 0).
SCRIPTED_ANSWERS: list[dict[str, str]] = [
    {
        "reasoning": "Feasibility step 1/3: list the container workdir.",
        "code": "print(exec_command('pwd && ls /app'))",
    },
    {
        "reasoning": "Feasibility step 2/3: peek at the probe script.",
        "code": "print(exec_command('head -30 /app/workflow_probe.py'))",
    },
    {
        "reasoning": "Feasibility step 3/3: exploration only; submit a dummy solution.",
        "code": "SUBMIT(solution='har85-feasibility-dummy')",
    },
    # Spare: consumed only if the loop ever reaches the extract fallback.
    {
        "reasoning": "Fallback spare, unused when SUBMIT fires.",
        "code": "SUBMIT(solution='har85-feasibility-dummy-spare')",
    },
]


def build_scripted_lms(policy: Any, **kwargs: Any) -> tuple[Any, None]:
    """Drop-in for ``evallab.harbor_rlm.build_lms``: $0 DummyLM, no sub-LM."""
    if DummyLM is None:
        raise RuntimeError("dspy is required for the scripted feasibility agent")
    return DummyLM(list(SCRIPTED_ANSWERS)), None


class ScriptedRlmAgent(LabRlmAgent):
    """LabRlmAgent with $0 scripted LMs. Feasibility only, never for paid runs."""

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        real_build_lms = upstream.build_lms
        upstream.build_lms = build_scripted_lms  # type: ignore[assignment]
        try:
            await super().run(instruction, environment, context)
        finally:
            upstream.build_lms = real_build_lms
