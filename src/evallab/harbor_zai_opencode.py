"""Trusted-task-only Harbor adapter for the Z.ai Coding Plan via OpenCode.

SECURITY BOUNDARY — read before using this lane.

This adapter links a filtered, Z.ai-only auth document into OpenCode's XDG
auth store inside the task container for the duration of one run. The
credential is expected to arrive as a read-only mount (docker secret) at
``AUTH_SECRET_MOUNT`` and is never read, copied, or logged by host code.
Because the *agent* can read the mounted credential during its turn, this
mount-based lane is acceptable ONLY for reviewed, trusted tasks. It is NOT
proxy-grade credential isolation: for untrusted or adversarial tasks use a
credential-isolating proxy lane (see ``harbor_deepseek``) instead, and never
present runs from this adapter as enforced-isolation evidence.

What the adapter guarantees mechanically:

- OpenCode is pinned to ``PINNED_OPENCODE_VERSION`` (reproducible installs)
  unless the caller passes an explicit ``version`` override.
- The model selector must be under the ``zai-coding-plan/`` provider prefix,
  so a misconfigured run cannot point the Z.ai credential at another vendor.
- Exactly one symlink — the constant ``AUTH_LINK_PATH`` — is created before
  the run and removed in ``finally``, including when the agent run raises;
  the cleanup failure is suppressed so it cannot mask the original error.
- No secret value is ever placed in a command, environment variable, or log:
  the only strings executed are the two constant paths below.

Observed provider behavior (2026-08-29 pilot, Coding Plan subscription):

- ``zai-coding-plan/glm-5.3`` and ``zai-coding-plan/glm-5.3-flash`` run
  successfully through this pinned adapter.
- ``zai-coding-plan/glm-5.3-highspeed`` is NOT included in the current
  subscription and the provider answers HTTP 429 "current subscription plan
  does not yet include access".

Provider access failures are execution errors, never model outcomes: this
adapter deliberately adds NO retry, fallback, or model substitution, so a
429/plan-access error surfaces as a raised Harbor error class and the trial
is not scored as agent capability. Never relabel such errors as reward 0.0.
"""

from __future__ import annotations

import contextlib
from typing import Any

from harbor.agents.installed.opencode import OpenCode  # ty: ignore[unresolved-import]

ADAPTER_VERSION = "1.0.0"

#: OpenCode release every install uses unless an explicit override is passed.
PINNED_OPENCODE_VERSION = "1.18.25"

#: The only accepted provider prefix; the Z.ai credential is scoped to it.
REQUIRED_MODEL_PREFIX = "zai-coding-plan/"

#: Read-only mount of the filtered, Z.ai-only OpenCode auth document.
AUTH_SECRET_MOUNT = "/run/secrets/evallab_zai_opencode_auth.json"

#: OpenCode reads ``$XDG_DATA_HOME/opencode/auth.json``; Harbor's pinned
#: OpenCode ``run()`` exports ``XDG_DATA_HOME=/logs/agent/opencode/xdg-data``.
AUTH_LINK_DIR = "/logs/agent/opencode/xdg-data/opencode"
AUTH_LINK_PATH = f"{AUTH_LINK_DIR}/auth.json"

#: The only commands this adapter executes. Both are composed exclusively of
#: the constant paths above — never of credential material.
CREATE_AUTH_LINK_COMMAND = f"mkdir -p {AUTH_LINK_DIR} && ln -sfn {AUTH_SECRET_MOUNT} {AUTH_LINK_PATH}"
REMOVE_AUTH_LINK_COMMAND = f"rm -f {AUTH_LINK_PATH}"


def validate_model_name(model_name: str | None) -> str:
    """Return ``model_name`` when it selects a Z.ai Coding Plan model.

    Raises ``ValueError`` otherwise. ``model_name`` is a provider/model
    selector, not a secret, so the offending value may appear in the message.
    """
    if not model_name or "/" not in model_name:
        raise ValueError(
            "ZaiOpenCodeAgent requires a provider/model selector, got "
            f"{model_name!r}"
        )
    provider, _, model = model_name.partition("/")
    if f"{provider}/" != REQUIRED_MODEL_PREFIX:
        raise ValueError(
            "ZaiOpenCodeAgent only accepts models under "
            f"{REQUIRED_MODEL_PREFIX!r} (the Z.ai Coding Plan credential "
            f"lane); got {model_name!r}"
        )
    if not model:
        raise ValueError(
            "ZaiOpenCodeAgent requires a non-empty model under "
            f"{REQUIRED_MODEL_PREFIX!r}; got {model_name!r}"
        )
    return model_name


class ZaiOpenCodeAgent(OpenCode):
    """Pinned, model-guarded OpenCode agent for trusted Z.ai Coding Plan tasks.

    Trusted-task-only: see the module docstring for the exact security
    boundary (readable read-only credential mount, no proxy isolation).
    """

    def __init__(
        self,
        *args: Any,
        version: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, version=version or PINNED_OPENCODE_VERSION, **kwargs)
        # Harbor may pass the model selector at construction; when it does,
        # refuse a non-Z.ai provider before any install or exec happens.
        model_name = getattr(self, "model_name", None)
        if model_name is not None:
            validate_model_name(model_name)

    async def run(self, instruction, environment, context) -> None:  # type: ignore[no-untyped-def]
        # Re-check at run time: Harbor can also assign the model after
        # construction, and ``run`` must fail closed before the auth link is
        # created rather than after.
        validate_model_name(self.model_name)
        await self.exec_as_agent(environment, command=CREATE_AUTH_LINK_COMMAND)
        try:
            await super().run(instruction, environment, context)
        finally:
            # Remove the link even when the agent run raises. Cleanup errors
            # are suppressed so the original agent error is never masked.
            with contextlib.suppress(Exception):
                await self.exec_as_agent(environment, command=REMOVE_AUTH_LINK_COMMAND)
