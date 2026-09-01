"""MemGym C0 source-only trajectory and outcome ingestion adapter.

Parses released MemGym trajectory fixtures into canonical ContextOperationFact rows
and task outcome descriptors. Total ordering is strictly determined by msg_index,
as step restarts per side. MemGym emits no tool-call IDs and no memory write/read/use
operations; compactions omit ordered forgotten-message indices, keeping compaction
content digests and read/use linkages typed unavailable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from evallab.semantic_facts import ContextOperationFact

_SANCTIONED_SIDES = frozenset({"agent", "user"})


def _strict_non_negative_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer, got {value!r}")
    return value


def _optional_strict_non_negative_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _strict_non_negative_int(value, field_name=field_name)


def _strict_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")
    return value.strip()


def _compute_sha256(data: Mapping[str, Any] | str | bytes) -> str:
    if isinstance(data, bytes):
        encoded = data
    elif isinstance(data, str):
        encoded = data.encode("utf-8")
    else:
        encoded = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class MemGymOutcome:
    """Task-level outcome extracted from MemGym training and result records."""

    domain: str
    task_id: str
    trial_id: str
    episode_reward: float | None
    episode_outcome: str | None
    result_reward: float | None
    result_success: bool | None
    evaluation_status: str
    provenance_source: str
    source_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_context_operation_facts_from_memgym(
    training_data: Mapping[str, Any],
    *,
    source_ref: str,
    source_digest: str | None = None,
    trial_id: str | None = None,
) -> tuple[ContextOperationFact, ...]:
    """Extract canonical ContextOperationFact rows from MemGym training records.

    Total ordering is established solely by msg_index (globally unique integer
    across agent and user messages). step restarts per side and is never used as
    total ordering.
    """
    if not isinstance(training_data, Mapping):
        raise ValueError("training_data must be a mapping")

    domain = _strict_str(training_data.get("domain"), field_name="domain")
    raw_task_id = training_data.get("task_id")
    task_id = _strict_str(
        str(raw_task_id) if raw_task_id is not None else None, field_name="task_id"
    )

    tid = trial_id if trial_id is not None else f"memgym:{domain}:{task_id}"
    digest = source_digest if source_digest is not None else _compute_sha256(training_data)

    raw_steps = training_data.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str | bytes):
        raise ValueError("training_data['steps'] must be a sequence of step mappings")

    seen_msg_indices: set[int] = set()
    facts: list[ContextOperationFact] = []

    for idx, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            raise ValueError(f"Step at index {idx} must be a mapping")

        side = _strict_str(raw_step.get("side"), field_name=f"steps[{idx}].side")
        if side not in _SANCTIONED_SIDES:
            raise ValueError(
                f"Invalid side {side!r} at step {idx}; must be one of {sorted(_SANCTIONED_SIDES)}"
            )

        msg_index = _strict_non_negative_int(
            raw_step.get("msg_index"), field_name=f"steps[{idx}].msg_index"
        )
        if msg_index in seen_msg_indices:
            raise ValueError(f"Duplicate msg_index {msg_index} found at step {idx}")
        seen_msg_indices.add(msg_index)

        # Validate step number if present
        if "step" in raw_step and raw_step["step"] is not None:
            _strict_non_negative_int(raw_step["step"], field_name=f"steps[{idx}].step")

        memory = raw_step.get("memory")
        before_tokens: int | None = None
        after_tokens: int | None = None
        prompt_tokens: int | None = None
        is_compaction = False

        if isinstance(memory, Mapping):
            before_tokens = _optional_strict_non_negative_int(
                memory.get("original_tokens"), field_name=f"steps[{idx}].memory.original_tokens"
            )
            after_tokens = _optional_strict_non_negative_int(
                memory.get("filtered_tokens"), field_name=f"steps[{idx}].memory.filtered_tokens"
            )
            raw_prompt_tokens = memory.get("summarizer_prompt_tokens")
            if (
                raw_prompt_tokens is not None
                and not isinstance(raw_prompt_tokens, bool)
                and isinstance(raw_prompt_tokens, int)
                and raw_prompt_tokens > 0
            ):
                prompt_tokens = raw_prompt_tokens
            new_compaction = memory.get("new_compaction")
            if isinstance(new_compaction, bool) and new_compaction:
                is_compaction = True

        operation = "compaction" if is_compaction else "session_boundary"
        operation_id = f"memgym:{domain}:{task_id}:{side}:{msg_index}"

        fact = ContextOperationFact.model_validate(
            {
                "source_ref": f"{source_ref}#msg_{msg_index}",
                "source_digest": digest,
                "provenance_kind": "mechanical",
                "trial_id": tid,
                "session_id": side,
                "operation_id": operation_id,
                "operation": operation,
                "step_index": msg_index,
                "content_digest": None,  # MemGym compaction lacks ordered forgotten indices; typed unavailable
                "before_token_count": before_tokens,
                "after_token_count": after_tokens,
                "prompt_tokens": prompt_tokens,
                "context_position_tokens": None,
                "configured_size": None,
                "realized_size": None,
            }
        )
        facts.append(fact)

    # Sort strictly by step_index (msg_index total order) for representation independence
    facts.sort(key=lambda f: f.step_index if f.step_index is not None else -1)
    return tuple(facts)


def extract_memgym_outcome(
    training_data: Mapping[str, Any],
    result_data: Mapping[str, Any] | None = None,
    *,
    source_ref: str = "result.json",
    source_digest: str | None = None,
    trial_id: str | None = None,
) -> MemGymOutcome:
    """Extract task outcome and verification status from MemGym records.

    If result_data is provided, validates domain and task_id parity fail-closed.
    """
    if not isinstance(training_data, Mapping):
        raise ValueError("training_data must be a mapping")

    domain = _strict_str(training_data.get("domain"), field_name="domain")
    raw_task_id = training_data.get("task_id")
    task_id = _strict_str(
        str(raw_task_id) if raw_task_id is not None else None, field_name="task_id"
    )

    raw_ep_reward = training_data.get("episode_reward")
    episode_reward: float | None = None
    if raw_ep_reward is not None:
        if isinstance(raw_ep_reward, bool) or not isinstance(raw_ep_reward, int | float):
            raise ValueError(f"episode_reward must be a numeric value, got {raw_ep_reward!r}")
        episode_reward = float(raw_ep_reward)

    raw_outcome = training_data.get("episode_outcome")
    episode_outcome = str(raw_outcome) if raw_outcome is not None else None

    result_reward: float | None = None
    result_success: bool | None = None
    evaluation_status = "unavailable"

    if result_data is not None:
        if not isinstance(result_data, Mapping):
            raise ValueError("result_data must be a mapping")

        res_domain = _strict_str(result_data.get("domain"), field_name="result_data.domain")
        res_raw_task_id = result_data.get("task_id")
        res_task_id = _strict_str(
            str(res_raw_task_id) if res_raw_task_id is not None else None,
            field_name="result_data.task_id",
        )

        if res_domain != domain or res_task_id != task_id:
            raise ValueError(
                f"Task identity mismatch: training has ({domain}, {task_id}) but result has ({res_domain}, {res_task_id})"
            )

        raw_res_reward = result_data.get("reward")
        if raw_res_reward is not None:
            if isinstance(raw_res_reward, bool) or not isinstance(raw_res_reward, int | float):
                raise ValueError(f"result reward must be numeric, got {raw_res_reward!r}")
            result_reward = float(raw_res_reward)

        raw_res_success = result_data.get("success")
        if raw_res_success is not None:
            if not isinstance(raw_res_success, bool):
                raise ValueError(f"result success must be a boolean, got {raw_res_success!r}")
            result_success = raw_res_success

        evaluation = result_data.get("evaluation")
        if isinstance(evaluation, Mapping):
            eval_fields = (
                evaluation.get("db_check"),
                evaluation.get("env_assertions"),
                evaluation.get("action_checks"),
                evaluation.get("nl_assertions"),
                evaluation.get("communicate_checks"),
                evaluation.get("reward_basis"),
                evaluation.get("reward_breakdown"),
            )
            if any(f is not None for f in eval_fields):
                evaluation_status = "observed"

    tid = trial_id if trial_id is not None else f"memgym:{domain}:{task_id}"
    digest = (
        source_digest
        if source_digest is not None
        else _compute_sha256(result_data or training_data)
    )

    return MemGymOutcome(
        domain=domain,
        task_id=task_id,
        trial_id=tid,
        episode_reward=episode_reward,
        episode_outcome=episode_outcome,
        result_reward=result_reward,
        result_success=result_success,
        evaluation_status=evaluation_status,
        provenance_source=source_ref,
        source_digest=digest,
    )


__all__ = [
    "MemGymOutcome",
    "extract_context_operation_facts_from_memgym",
    "extract_memgym_outcome",
]
