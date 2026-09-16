"""Opt-in mini-swe-agent 2.4.6 Chat Completions context policy.

Install these modules in the *agent's* Python environment, then select
``model.model_class=evallab.mini_observation_masking.LastNObservationModel``.
This does not alter Harbor's runner, default model class, or retained history.
Responses-API models are deliberately not claimed as supported by this adapter.
"""

from __future__ import annotations

from typing import Any

# Optional agent-runtime dependency: not required for Lab's offline policy CLI.
from minisweagent.models.litellm_model import (  # ty: ignore[unresolved-import]
    LitellmModel,
    LitellmModelConfig,
)
from pydantic import Field

from evallab.observation_masking import LastNObservations


class LastNObservationModelConfig(LitellmModelConfig):
    observation_masking_keep_last: int = Field(default=10, gt=0, strict=True)


class LastNObservationModel(LitellmModel):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(config_class=LastNObservationModelConfig, **kwargs)
        self._observation_policy = LastNObservations(self.config.observation_masking_keep_last)
        self._observation_policy_identity = self._observation_policy.identity()

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        # Keep upstream metadata removal, thinking ordering and cache handling.
        prepared = super()._prepare_messages_for_api(messages)
        return self._observation_policy.apply(prepared)

    def serialize(self) -> dict:
        result = super().serialize()
        result["info"]["context_policy"] = self._observation_policy_identity
        return result
