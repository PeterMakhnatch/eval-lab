"""Configure a DSPy LM on the Lab's admitted Z.ai Coding Plan route.

Mirrors ``evallab.gepa_optimizer.proposer.JournaledReflectionLM``: the key is
read from OpenCode's owner-only auth store, never from a pasted secret, and the
model must be an admitted Coding Plan selector. Subscription transport: the
binding constraint is the plan's rate window, not dollars.
"""

from __future__ import annotations

import dspy
from dspy.clients._litellm import get_litellm

from evallab.execution_contracts import (
    ZAI_OPENCODE_MODEL_SELECTORS,
    opencode_auth_path,
    read_zai_opencode_key,
)

# DSPy imports LiteLLM lazily on first use. Under dspy.Evaluate's thread pool the
# first calls race and one thread sees a half-initialised module
# ("partially initialized module 'litellm' has no attribute 'completion'").
# Materialise it once, on the main thread, before any parallel work.
get_litellm(feature="dspy.LM")

ZAI_CODING_API_BASE = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_MODEL = "zai/glm-5.3-flash"


def zai_lm(
    model: str = DEFAULT_MODEL,
    *,
    max_tokens: int = 8000,
    temperature: float = 0.0,
    cache: bool = True,
    thinking: bool = False,
    **kwargs,
) -> dspy.LM:
    """Return a ``dspy.LM`` bound to the admitted Z.ai Coding Plan endpoint.

    GLM 5.3 is a reasoning model: with hidden thinking enabled it can spend the
    whole ``max_tokens`` budget in ``reasoning_content`` and return an empty
    answer, which DSPy reports as an adapter parse failure. Hidden thinking is
    therefore off by default; ``dspy.ChainOfThought`` supplies visible reasoning.
    """
    target = "zai-coding-plan/" + model.removeprefix("zai/")
    if target not in ZAI_OPENCODE_MODEL_SELECTORS:
        raise ValueError(f"{model!r} is not an admitted Z.ai Coding Plan model")
    extra_body = dict(kwargs.pop("extra_body", {}) or {})
    extra_body.setdefault("thinking", {"type": "enabled" if thinking else "disabled"})
    return dspy.LM(
        model,
        api_base=ZAI_CODING_API_BASE,
        api_key=read_zai_opencode_key(opencode_auth_path()),
        max_tokens=max_tokens,
        temperature=temperature,
        cache=cache,
        num_retries=6,
        timeout=180,
        extra_body=extra_body,
        **kwargs,
    )


def configure(model: str = DEFAULT_MODEL, **kwargs) -> dspy.LM:
    lm = zai_lm(model, **kwargs)
    dspy.configure(lm=lm)
    return lm
