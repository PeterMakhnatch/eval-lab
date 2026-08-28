"""TrajectoryIR v1: Canonical intermediate representation with full ATIF fidelity.

Preserves all ATIF-v1.7 and Harbor trajectory fields without loss:
- Full reasoning_content stored in CAS blobs (cas://sha256/<hash>) and in-memory IR
- Exact reasoning tokens, prompt/completion token IDs, logprobs
- Sampling parameters (temperature, top_p, top_k, max_tokens, reasoning_effort)
- Sample index, LLM call counts, and copied context flags
- Tool definitions and invocation schemas
- Multi-step observation results, byte sizes, and digests
- Linked TrajectoryLossReport guaranteeing complete field preservation
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from evallab.evidence_store import store_blob
from evallab.trajectory_loss_manifest import TrajectoryLossReport, audit_trajectory_loss


@dataclass(frozen=True)
class SamplingParams:
    """Sampling parameters applied during generation."""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    stop: tuple[str, ...] | None = None
    seed: int | None = None
    reasoning_effort: str | float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricsRecord:
    """Detailed token usage, inference costs, and token sequences."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    reasoning_tokens: int | None = None
    prompt_token_ids: tuple[int, ...] | None = None
    completion_token_ids: tuple[int, ...] | None = None
    logprobs: tuple[Any, ...] | None = None
    prompt_token_ids_ref: str | None = None
    completion_token_ids_ref: str | None = None
    logprobs_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallRecord:
    """Typed tool invocation specification."""

    tool_call_id: str
    function_name: str
    arguments: dict[str, Any]
    arguments_raw: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservationResultRecord:
    """Typed environment observation or tool execution result."""

    source_call_id: str | None = None
    content: str | list[Any] | None = None
    content_ref: str | None = None
    content_bytes: int = 0
    content_digest: str | None = None
    subagent_trajectory_ref: tuple[dict[str, Any], ...] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepRecord:
    """Full-fidelity trajectory step record."""

    step_id: int
    source: str
    timestamp: str | None = None
    model_name: str | None = None
    message: str | list[Any] = ""
    message_sha256: str | None = None
    message_chars: int | None = None
    reasoning_content: str | None = None
    reasoning_content_ref: str | None = None
    reasoning_tokens: int | None = None
    tool_calls: tuple[ToolCallRecord, ...] = ()
    observation_results: tuple[ObservationResultRecord, ...] = ()
    metrics: MetricsRecord | None = None
    sampling_params: SamplingParams | None = None
    sample_index: int | None = None
    llm_call_count: int | None = None
    is_copied_context: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FinalMetricsRecord:
    """Overall trajectory execution metrics."""

    total_prompt_tokens: int | None = None
    total_completion_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_cost_usd: float | None = None
    total_steps: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrajectoryIR:
    """Canonical, immutable intermediate representation with lossless fidelity."""

    schema_version: str
    session_id: str | None = None
    trajectory_id: str | None = None
    agent_name: str = "unknown"
    agent_version: str | None = None
    model_name: str = "unknown"
    tool_definitions: tuple[dict[str, Any], ...] = ()
    agent_extra: dict[str, Any] = field(default_factory=dict)
    steps: tuple[StepRecord, ...] = ()
    final_metrics: FinalMetricsRecord | None = None
    notes: str | None = None
    subagent_trajectories: tuple[Any, ...] = ()
    continued_trajectory_ref: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    source_sha256: str | None = None
    loss_report: TrajectoryLossReport | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert TrajectoryIR into a comprehensive dictionary."""
        data = asdict(self)
        if self.loss_report:
            data["loss_report"] = self.loss_report.to_dict()
        return data


def _extract_reasoning_tokens(
    step_data: dict[str, Any], metrics_data: dict[str, Any]
) -> int | None:
    """Extract reasoning / thinking tokens from metrics or extra subfields."""
    extra = metrics_data.get("extra")
    if isinstance(extra, dict):
        for k in (
            "reasoning_tokens",
            "reasoning_output_tokens",
            "thinking_tokens",
            "reasoning_token_count",
        ):
            val = extra.get(k)
            if isinstance(val, int):
                return val
    direct_val = metrics_data.get("reasoning_tokens")
    if isinstance(direct_val, int):
        return direct_val
    return None


def _extract_sampling_params(
    step_data: dict[str, Any], agent_extra: dict[str, Any]
) -> SamplingParams | None:
    """Extract sampling parameters from step or agent level."""
    sp = step_data.get("sampling_params") or step_data.get("sampling_parameters")
    if not isinstance(sp, dict):
        sp = (
            step_data.get("extra", {}).get("sampling_params")
            if isinstance(step_data.get("extra"), dict)
            else None
        )
    if not isinstance(sp, dict):
        sp = agent_extra.get("sampling_params") if isinstance(agent_extra, dict) else None
    if not isinstance(sp, dict):
        direct_keys = (
            "temperature",
            "top_p",
            "top_k",
            "max_tokens",
            "presence_penalty",
            "frequency_penalty",
            "stop",
            "seed",
            "reasoning_effort",
        )
        found = {
            k: step_data[k] for k in direct_keys if k in step_data and step_data[k] is not None
        }
        if found:
            sp = found

    if not isinstance(sp, dict):
        return None

    extra_keys = {
        k: v
        for k, v in sp.items()
        if k
        not in (
            "temperature",
            "top_p",
            "top_k",
            "max_tokens",
            "presence_penalty",
            "frequency_penalty",
            "stop",
            "seed",
            "reasoning_effort",
        )
    }
    stop_val = sp.get("stop")
    stop_tuple = tuple(stop_val) if isinstance(stop_val, list) else None
    return SamplingParams(
        temperature=float(sp["temperature"])
        if "temperature" in sp and sp["temperature"] is not None
        else None,
        top_p=float(sp["top_p"]) if "top_p" in sp and sp["top_p"] is not None else None,
        top_k=int(sp["top_k"]) if "top_k" in sp and sp["top_k"] is not None else None,
        max_tokens=int(sp["max_tokens"])
        if "max_tokens" in sp and sp["max_tokens"] is not None
        else None,
        presence_penalty=float(sp["presence_penalty"])
        if "presence_penalty" in sp and sp["presence_penalty"] is not None
        else None,
        frequency_penalty=float(sp["frequency_penalty"])
        if "frequency_penalty" in sp and sp["frequency_penalty"] is not None
        else None,
        stop=stop_tuple,
        seed=int(sp["seed"]) if "seed" in sp and sp["seed"] is not None else None,
        reasoning_effort=str(sp["reasoning_effort"])
        if "reasoning_effort" in sp and sp["reasoning_effort"] is not None
        else None,
        extra=extra_keys,
    )


def build_trajectory_ir(
    raw_data: dict[str, Any],
    store_root: Path | None = None,
    source_path: str = "",
    source_sha256: str = "",
) -> TrajectoryIR:
    """Build a lossless TrajectoryIR instance from raw ATIF / Harbor trajectory data."""
    schema_version = str(raw_data.get("schema_version") or "ATIF-v1.7")
    session_id = raw_data.get("session_id")
    trajectory_id = raw_data.get("trajectory_id") or session_id
    notes = raw_data.get("notes")

    raw_agent = raw_data.get("agent")
    agent_data: dict[str, Any] = raw_agent if isinstance(raw_agent, dict) else {}
    agent_name = str(agent_data.get("name") or "unknown")
    agent_version = (
        str(agent_data.get("version")) if agent_data.get("version") is not None else None
    )
    model_name = str(agent_data.get("model_name") or agent_data.get("model") or "unknown")
    tool_defs_raw = agent_data.get("tool_definitions") or agent_data.get("tools") or []
    tool_definitions = tuple(tool_defs_raw) if isinstance(tool_defs_raw, list) else ()
    raw_agent_extra = agent_data.get("extra")
    agent_extra: dict[str, Any] = raw_agent_extra if isinstance(raw_agent_extra, dict) else {}
    # Root final metrics
    raw_fm = raw_data.get("final_metrics")
    fm_raw: dict[str, Any] = raw_fm if isinstance(raw_fm, dict) else {}
    final_metrics = FinalMetricsRecord(
        total_prompt_tokens=fm_raw.get("total_prompt_tokens"),
        total_completion_tokens=fm_raw.get("total_completion_tokens"),
        total_cached_tokens=fm_raw.get("total_cached_tokens"),
        total_cost_usd=(
            float(fm_raw["total_cost_usd"])
            if "total_cost_usd" in fm_raw and fm_raw["total_cost_usd"] is not None
            else None
        ),
        total_steps=fm_raw.get("total_steps"),
        extra=fm_raw.get("extra", {}) if isinstance(fm_raw.get("extra"), dict) else {},
    )

    raw_steps = raw_data.get("steps")
    steps_raw: list[Any] = raw_steps if isinstance(raw_steps, list) else []
    step_records: list[StepRecord] = []

    for idx, raw_step in enumerate(steps_raw, start=1):
        if not isinstance(raw_step, dict):
            continue
        step_id = int(raw_step.get("step_id") or idx)
        source = str(raw_step.get("source") or "agent")
        timestamp = raw_step.get("timestamp")
        step_model = raw_step.get("model_name") or model_name
        message = raw_step.get("message", "")
        message_sha256 = raw_step.get("message_sha256")
        message_chars = raw_step.get("message_chars")

        # Reasoning content & CAS storage
        reasoning_content = raw_step.get("reasoning_content") or raw_step.get("thought")
        reasoning_ref = None
        if reasoning_content:
            reasoning_str = str(reasoning_content)
            if store_root is not None:
                reasoning_ref = store_blob(store_root, reasoning_str)
            else:
                digest = hashlib.sha256(reasoning_str.encode("utf-8")).hexdigest()
                reasoning_ref = f"cas://sha256/{digest}"

        # Metrics
        metrics_data = raw_step.get("metrics") if isinstance(raw_step.get("metrics"), dict) else {}
        reasoning_tokens = _extract_reasoning_tokens(raw_step, metrics_data)

        prompt_token_ids_raw = metrics_data.get("prompt_token_ids")
        prompt_token_ids = (
            tuple(prompt_token_ids_raw) if isinstance(prompt_token_ids_raw, list) else None
        )
        p_ids_ref = None
        if prompt_token_ids and store_root is not None:
            p_ids_ref = store_blob(store_root, json.dumps(prompt_token_ids).encode("utf-8"))

        comp_token_ids_raw = metrics_data.get("completion_token_ids")
        comp_token_ids = tuple(comp_token_ids_raw) if isinstance(comp_token_ids_raw, list) else None
        c_ids_ref = None
        if comp_token_ids and store_root is not None:
            c_ids_ref = store_blob(store_root, json.dumps(comp_token_ids).encode("utf-8"))

        logprobs_raw = metrics_data.get("logprobs")
        logprobs = tuple(logprobs_raw) if isinstance(logprobs_raw, list) else None
        logprobs_ref = None
        if logprobs and store_root is not None:
            logprobs_ref = store_blob(store_root, json.dumps(logprobs).encode("utf-8"))

        metrics_record = MetricsRecord(
            prompt_tokens=metrics_data.get("prompt_tokens"),
            completion_tokens=metrics_data.get("completion_tokens"),
            cached_tokens=metrics_data.get("cached_tokens"),
            cost_usd=float(metrics_data["cost_usd"])
            if "cost_usd" in metrics_data and metrics_data["cost_usd"] is not None
            else None,
            reasoning_tokens=reasoning_tokens,
            prompt_token_ids=prompt_token_ids,
            completion_token_ids=comp_token_ids,
            logprobs=logprobs,
            prompt_token_ids_ref=p_ids_ref,
            completion_token_ids_ref=c_ids_ref,
            logprobs_ref=logprobs_ref,
            extra=metrics_data.get("extra", {})
            if isinstance(metrics_data.get("extra"), dict)
            else {},
        )

        # Sampling params & sample index
        sampling_params = _extract_sampling_params(raw_step, agent_extra)
        sample_index = raw_step.get("sample_index")
        llm_call_count = raw_step.get("llm_call_count")
        is_copied_context = raw_step.get("is_copied_context")

        # Tool calls
        tool_calls_list: list[ToolCallRecord] = []
        tc_raw = raw_step.get("tool_calls")
        if isinstance(tc_raw, list):
            for tc in tc_raw:
                if isinstance(tc, dict):
                    tool_calls_list.append(
                        ToolCallRecord(
                            tool_call_id=str(tc.get("tool_call_id") or tc.get("id") or ""),
                            function_name=str(tc.get("function_name") or tc.get("name") or ""),
                            arguments=tc.get("arguments")
                            if isinstance(tc.get("arguments"), dict)
                            else {},
                            arguments_raw=tc.get("arguments_raw") or tc.get("raw_arguments"),
                            extra=tc.get("extra", {}) if isinstance(tc.get("extra"), dict) else {},
                        )
                    )

        # Observation results
        obs_list: list[ObservationResultRecord] = []
        obs_raw = raw_step.get("observation_results")
        if isinstance(obs_raw, list):
            for obs in obs_raw:
                if isinstance(obs, dict):
                    content = obs.get("content")
                    content_str = (
                        json.dumps(content)
                        if isinstance(content, list | dict)
                        else str(content or "")
                    )
                    content_bytes = len(content_str.encode("utf-8"))
                    c_digest = hashlib.sha256(content_str.encode("utf-8")).hexdigest()
                    c_ref = None
                    if store_root is not None and content:
                        c_ref = store_blob(store_root, content_str)
                    else:
                        c_ref = f"cas://sha256/{c_digest}"

                    obs_list.append(
                        ObservationResultRecord(
                            source_call_id=obs.get("source_call_id"),
                            content=content,
                            content_ref=c_ref,
                            content_bytes=content_bytes,
                            content_digest=c_digest,
                            subagent_trajectory_ref=tuple(obs["subagent_trajectory_ref"])
                            if isinstance(obs.get("subagent_trajectory_ref"), list)
                            else None,
                            extra=obs.get("extra", {})
                            if isinstance(obs.get("extra"), dict)
                            else {},
                        )
                    )

        step_records.append(
            StepRecord(
                step_id=step_id,
                source=source,
                timestamp=timestamp,
                model_name=step_model,
                message=message,
                message_sha256=message_sha256,
                message_chars=message_chars,
                reasoning_content=str(reasoning_content) if reasoning_content is not None else None,
                reasoning_content_ref=reasoning_ref,
                reasoning_tokens=reasoning_tokens,
                tool_calls=tuple(tool_calls_list),
                observation_results=tuple(obs_list),
                metrics=metrics_record,
                sampling_params=sampling_params,
                sample_index=sample_index,
                llm_call_count=llm_call_count,
                is_copied_context=is_copied_context,
                extra=raw_step.get("extra", {}) if isinstance(raw_step.get("extra"), dict) else {},
            )
        )

    # Audit loss manifest
    loss_report = audit_trajectory_loss(raw_data, source_path=source_path)

    return TrajectoryIR(
        schema_version=schema_version,
        session_id=str(session_id) if session_id else None,
        trajectory_id=str(trajectory_id) if trajectory_id else None,
        agent_name=agent_name,
        agent_version=agent_version,
        model_name=model_name,
        tool_definitions=tool_definitions,
        agent_extra=agent_extra,
        steps=tuple(step_records),
        final_metrics=final_metrics,
        notes=notes,
        subagent_trajectories=tuple(raw_data.get("subagent_trajectories", ())),
        continued_trajectory_ref=raw_data.get("continued_trajectory_ref"),
        extra=raw_data.get("extra", {}) if isinstance(raw_data.get("extra"), dict) else {},
        source_path=source_path or None,
        source_sha256=source_sha256 or None,
        loss_report=loss_report,
    )


def trajectory_ir_to_dict(ir: TrajectoryIR) -> dict[str, Any]:
    """Serialize TrajectoryIR to a JSON-compatible dictionary."""
    out = asdict(ir)
    if ir.loss_report:
        out["loss_report"] = asdict(ir.loss_report)
    return out


def trajectory_ir_from_file(
    path: Path,
    store_root: Path | None = None,
) -> TrajectoryIR:
    """Read a trajectory file and return a parsed TrajectoryIR instance."""
    if not path.is_file():
        raise FileNotFoundError(f"Trajectory file not found: {path}")
    raw_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw_json = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"Invalid JSON trajectory at {path}: {err}") from err

    if not isinstance(raw_json, dict):
        raise ValueError(
            f"Trajectory top-level element must be a JSON object, got {type(raw_json).__name__}"
        )

    return build_trajectory_ir(
        raw_data=raw_json,
        store_root=store_root,
        source_path=str(path),
        source_sha256=source_sha256,
    )


def trajectory_ir_to_outline(
    ir: TrajectoryIR,
    trial_id: str = "adhoc-trial",
    job_id: str = "adhoc-job",
    trial_name: str = "adhoc-trial",
    job_name: str = "adhoc-job",
    task_name: str = "adhoc-task",
) -> Any:
    """Convert a TrajectoryIR instance into a TrajectoryOutline."""
    from collections import Counter

    from evallab.traj import (
        SourceCitation,
        StepOutline,
        TrajectoryOutline,
        _analyze_loop_suspicion,
        _build_phases,
        _extract_command_string,
        _is_edit_action,
    )

    steps_out: list[StepOutline] = []
    tool_mix_counter: Counter[str] = Counter()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0
    total_cost_usd = 0.0

    step_to_first_tool: int | None = None
    step_to_first_edit: int | None = None
    first_step_timestamp: str | None = None

    last_was_error = False
    recovery_count = 0
    total_errors = 0

    for step in ir.steps:
        if first_step_timestamp is None and step.timestamp:
            first_step_timestamp = step.timestamp

        primary_tool_name = None
        primary_tool_cmd = None
        if step.tool_calls:
            first_call = step.tool_calls[0]
            primary_tool_name = first_call.function_name or None
            primary_tool_cmd = _extract_command_string(first_call.arguments)
            for tc in step.tool_calls:
                if tc.function_name:
                    tool_mix_counter[tc.function_name] += 1
        if primary_tool_name and step_to_first_tool is None:
            step_to_first_tool = step.step_id

        if _is_edit_action(primary_tool_name, primary_tool_cmd) and step_to_first_edit is None:
            step_to_first_edit = step.step_id

        exit_code = None
        is_error = False
        error_msg = None
        for obs in step.observation_results:
            if (
                isinstance(obs.extra, dict)
                and "exit_code" in obs.extra
                and isinstance(obs.extra["exit_code"], int)
            ):
                exit_code = obs.extra["exit_code"]
            content_str = str(obs.content or "")
            if exit_code is not None and exit_code != 0:
                is_error = True
                error_msg = error_msg or f"command exited with code {exit_code}"
            elif (
                "error" in str(obs.extra.get("type", "")).lower()
                or "error" in str(obs.extra.get("status", "")).lower()
            ):
                is_error = True
                error_msg = (
                    error_msg or content_str[:120].strip() or "tool result reported an error"
                )

        if is_error:
            total_errors += 1
            last_was_error = True
        else:
            if last_was_error and (primary_tool_name or step.source == "agent"):
                recovery_count += 1
                last_was_error = False

        m = step.metrics
        p_tokens = m.prompt_tokens if m else None
        c_tokens = m.completion_tokens if m else None
        ca_tokens = m.cached_tokens if m else None
        c_usd = m.cost_usd if m else None
        r_tokens = m.reasoning_tokens if m else None
        p_ref = m.prompt_token_ids_ref if m else None
        c_ref = m.completion_token_ids_ref if m else None
        lp_ref = m.logprobs_ref if m else None

        if p_tokens:
            total_prompt_tokens += p_tokens
        if c_tokens:
            total_completion_tokens += c_tokens
        if ca_tokens:
            total_cached_tokens += ca_tokens
        if c_usd:
            total_cost_usd += c_usd

        thought_snippet = None
        if step.reasoning_content:
            thought_snippet = str(step.reasoning_content)[:120].replace("\n", " ").strip()
        elif step.message:
            thought_snippet = str(step.message)[:120].replace("\n", " ").strip()

        steps_out.append(
            StepOutline(
                step_id=step.step_id,
                source=step.source,
                timestamp=step.timestamp,
                model_name=step.model_name,
                tool_name=primary_tool_name,
                tool_command=primary_tool_cmd,
                exit_code=exit_code,
                is_error=is_error,
                error_message=error_msg,
                prompt_tokens=p_tokens,
                completion_tokens=c_tokens,
                cached_tokens=ca_tokens,
                cost_usd=c_usd,
                thought_snippet=thought_snippet,
                is_redacted=False,
                redaction_digest=None,
                reasoning_content=step.reasoning_content,
                reasoning_content_ref=step.reasoning_content_ref,
                reasoning_tokens=r_tokens,
                prompt_token_ids_ref=p_ref,
                completion_token_ids_ref=c_ref,
                logprobs_ref=lp_ref,
                sample_index=step.sample_index,
                sampling_params=asdict(step.sampling_params) if step.sampling_params else None,
            )
        )

    phases = _build_phases(steps_out)
    loop_suspicion = _analyze_loop_suspicion(steps_out)
    citations = (
        SourceCitation(
            path=ir.source_path or "unknown",
            sha256=ir.source_sha256 or "unknown",
            step_id=None,
            kind="trajectory_ir",
        ),
    )

    return TrajectoryOutline(
        trial_id=trial_id,
        job_id=job_id,
        trial_name=trial_name,
        job_name=job_name,
        task_name=task_name,
        agent_name=ir.agent_name,
        agent_version=ir.agent_version,
        model_name=ir.model_name,
        status="featured",
        unavailable_reason=None,
        source_path=ir.source_path or "unknown",
        source_sha256=ir.source_sha256 or "unknown",
        duration_seconds=None,
        primary_reward=None,
        exception_class=None,
        total_steps=len(steps_out),
        agent_steps=sum(1 for s in steps_out if s.source == "agent"),
        system_steps=sum(1 for s in steps_out if s.source == "system"),
        user_steps=sum(1 for s in steps_out if s.source == "user"),
        total_tool_calls=sum(len(s.tool_calls) for s in ir.steps),
        total_errors=total_errors,
        recovery_count=recovery_count,
        step_to_first_tool=step_to_first_tool,
        step_to_first_edit=step_to_first_edit,
        time_to_first_tool_seconds=None,
        time_to_first_edit_seconds=None,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        total_cached_tokens=total_cached_tokens,
        total_cost_usd=round(total_cost_usd, 6),
        loop_suspicion=loop_suspicion,
        phases=tuple(phases),
        steps=tuple(steps_out),
        citations=citations,
        tool_mix=dict(tool_mix_counter),
    )
