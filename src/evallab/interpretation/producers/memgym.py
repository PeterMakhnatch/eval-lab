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
from pathlib import Path
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


def _validate_identity_str(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) == 0:
        raise ValueError(f"{field_name} must be a non-empty string, got {value!r}")
    return value


def _validate_task_id(value: Any, *, field_name: str) -> str | int:
    if isinstance(value, bool) or (not isinstance(value, str) and not isinstance(value, int)):
        raise ValueError(
            f"{field_name} must be a non-empty string or non-negative integer, got {value!r}"
        )
    if isinstance(value, str):
        if len(value) == 0:
            raise ValueError(f"{field_name} string must be non-empty")
        return value
    if value < 0:
        raise ValueError(f"{field_name} integer must be non-negative, got {value}")
    return value


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _compute_trial_id(domain: str, task_id: str | int) -> str:
    payload = {"domain": domain, "task_id": task_id}
    digest = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return f"memgym:trial:{digest}"


def _compute_operation_id(trial_id: str, side: str, msg_index: int) -> str:
    payload = {"msg_index": msg_index, "side": side, "trial_id": trial_id}
    digest = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return f"memgym:op:{digest}"


def _load_raw_bytes(data: bytes | str | Path, *, field_name: str) -> tuple[bytes, str]:
    if isinstance(data, bytes):
        return data, "<raw_bytes>"
    if isinstance(data, Path):
        return data.read_bytes(), str(data)
    if isinstance(data, str):
        path = Path(data)
        if path.is_file():
            return path.read_bytes(), str(path)
        return data.encode("utf-8"), "<string_payload>"
    raise TypeError(
        f"{field_name} must be bytes, str, or Path, got {type(data).__name__}; "
        "parsed mappings cannot guarantee exact-byte provenance"
    )


def _parse_strict_json(raw_bytes: bytes, *, field_name: str) -> dict[str, Any]:
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError(f"{field_name} is not valid UTF-8: {err}") from err
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as err:
        raise ValueError(f"{field_name} is not valid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} JSON root must be an object, got {type(parsed).__name__}")
    return parsed


@dataclass(frozen=True)
class MemGymOutcome:
    """Task-level outcome extracted from MemGym training and result records."""

    domain: str
    task_id: str | int
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
    training_bytes: bytes | str | Path,
    *,
    source_ref: str | None = None,
    expected_source_digest: str | None = None,
    expected_trial_id: str | None = None,
) -> tuple[ContextOperationFact, ...]:
    """Extract canonical ContextOperationFact rows from MemGym training records.

    Total ordering is established solely by msg_index (globally unique integer
    across agent and user messages). step restarts per side and is never used as
    total ordering.
    """
    raw_bytes, default_ref = _load_raw_bytes(training_bytes, field_name="training_bytes")
    digest = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    if expected_source_digest is not None and expected_source_digest != digest:
        raise ValueError(
            f"Expected source digest {expected_source_digest!r} does not match computed byte digest {digest!r}"
        )
    effective_ref = source_ref if source_ref is not None else default_ref

    training_data = _parse_strict_json(raw_bytes, field_name="training_bytes")

    domain = _validate_identity_str(training_data.get("domain"), field_name="domain")
    task_id = _validate_task_id(training_data.get("task_id"), field_name="task_id")

    tid = _compute_trial_id(domain, task_id)
    if expected_trial_id is not None and expected_trial_id != tid:
        raise ValueError(
            f"Derived trial_id {tid!r} does not match expected_trial_id {expected_trial_id!r}"
        )

    raw_steps = training_data.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, str | bytes):
        raise ValueError("training_data['steps'] must be a sequence of step mappings")

    seen_msg_indices: set[int] = set()
    facts: list[ContextOperationFact] = []

    for idx, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            raise ValueError(f"Step at index {idx} must be a mapping")

        raw_side = raw_step.get("side")
        if not isinstance(raw_side, str) or raw_side not in _SANCTIONED_SIDES:
            raise ValueError(
                f"steps[{idx}].side must be an exact string member of {sorted(_SANCTIONED_SIDES)}, got {raw_side!r}"
            )
        side = raw_side

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
            if raw_prompt_tokens is not None:
                prompt_tokens = _strict_non_negative_int(
                    raw_prompt_tokens,
                    field_name=f"steps[{idx}].memory.summarizer_prompt_tokens",
                )

            was_compacted = memory.get("was_compacted")
            if was_compacted is not None and type(was_compacted) is not bool:
                raise ValueError(
                    f"steps[{idx}].memory.was_compacted must be a boolean or null, got {was_compacted!r} of type {type(was_compacted).__name__}"
                )

            new_compaction = memory.get("new_compaction")
            if new_compaction is not None:
                if type(new_compaction) is not bool:
                    raise ValueError(
                        f"steps[{idx}].memory.new_compaction must be a boolean or null, got {new_compaction!r} of type {type(new_compaction).__name__}"
                    )
                is_compaction = new_compaction

        operation = "compaction" if is_compaction else "session_boundary"
        operation_id = _compute_operation_id(tid, side, msg_index)

        fact = ContextOperationFact.model_validate(
            {
                "source_ref": f"{effective_ref}#msg_{msg_index}",
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
    training_bytes: bytes | str | Path,
    result_bytes: bytes | str | Path | None = None,
    *,
    training_source_ref: str | None = None,
    result_source_ref: str | None = None,
    expected_source_digest: str | None = None,
    expected_trial_id: str | None = None,
) -> MemGymOutcome:
    """Extract task outcome and verification status from MemGym records.

    If result_bytes is provided, validates domain and task_id parity fail-closed.
    """
    t_raw, default_t_ref = _load_raw_bytes(training_bytes, field_name="training_bytes")
    t_digest = f"sha256:{hashlib.sha256(t_raw).hexdigest()}"
    t_ref = training_source_ref if training_source_ref is not None else default_t_ref

    training_data = _parse_strict_json(t_raw, field_name="training_bytes")

    domain = _validate_identity_str(training_data.get("domain"), field_name="domain")
    task_id = _validate_task_id(training_data.get("task_id"), field_name="task_id")

    tid = _compute_trial_id(domain, task_id)
    if expected_trial_id is not None and expected_trial_id != tid:
        raise ValueError(
            f"Derived trial_id {tid!r} does not match expected_trial_id {expected_trial_id!r}"
        )

    raw_ep_reward = training_data.get("episode_reward")
    episode_reward: float | None = None
    if raw_ep_reward is not None:
        if isinstance(raw_ep_reward, bool) or not isinstance(raw_ep_reward, int | float):
            raise ValueError(f"episode_reward must be a numeric value, got {raw_ep_reward!r}")
        episode_reward = float(raw_ep_reward)

    raw_outcome = training_data.get("episode_outcome")
    episode_outcome: str | None = None
    if raw_outcome is not None:
        if not isinstance(raw_outcome, str):
            raise ValueError(
                f"episode_outcome must be a string or null, got {raw_outcome!r} of type {type(raw_outcome).__name__}"
            )
        episode_outcome = raw_outcome
    result_reward: float | None = None
    result_success: bool | None = None
    evaluation_status = "unavailable"

    if result_bytes is not None:
        r_raw, default_r_ref = _load_raw_bytes(result_bytes, field_name="result_bytes")
        r_digest = f"sha256:{hashlib.sha256(r_raw).hexdigest()}"
        r_ref = result_source_ref if result_source_ref is not None else default_r_ref

        if expected_source_digest is not None and expected_source_digest != r_digest:
            raise ValueError(
                f"Expected source digest {expected_source_digest!r} does not match computed result digest {r_digest!r}"
            )

        result_data = _parse_strict_json(r_raw, field_name="result_bytes")

        res_domain = _validate_identity_str(
            result_data.get("domain"), field_name="result_data.domain"
        )
        res_task_id = _validate_task_id(
            result_data.get("task_id"), field_name="result_data.task_id"
        )

        if (
            type(res_domain) is not type(domain)
            or res_domain != domain
            or type(res_task_id) is not type(task_id)
            or res_task_id != task_id
        ):
            raise ValueError(
                f"Task identity mismatch: training has (domain={domain!r}, task_id={task_id!r}) but result has (domain={res_domain!r}, task_id={res_task_id!r})"
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

        provenance_source = r_ref
        source_digest = r_digest
    else:
        if expected_source_digest is not None and expected_source_digest != t_digest:
            raise ValueError(
                f"Expected source digest {expected_source_digest!r} does not match computed training digest {t_digest!r}"
            )
        provenance_source = t_ref
        source_digest = t_digest

    return MemGymOutcome(
        domain=domain,
        task_id=task_id,
        trial_id=tid,
        episode_reward=episode_reward,
        episode_outcome=episode_outcome,
        result_reward=result_reward,
        result_success=result_success,
        evaluation_status=evaluation_status,
        provenance_source=provenance_source,
        source_digest=source_digest,
    )


__all__ = [
    "MemGymOutcome",
    "extract_context_operation_facts_from_memgym",
    "extract_memgym_outcome",
]
