from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._bespoke_adapter import (
    BESPOKE_BOARD_PATHS,
    BESPOKE_BOOKKEEPING_DIRS,
    BESPOKE_BUNDLE_PATHS,
    BESPOKE_PROXY_PATHS,
    BespokeExtraContext,
    is_regular_dir,
    read_bespoke_members,
    safe_iterdir,
)
from ._multiagent_io import (
    CostStatus,
    GradeMarkRecord,
    MemberRecord,
    MemberUsage,
    NormalizedAtifStep,
    NormalizedObservation,
    NormalizedToolCall,
    TrajectoryStatus,
    TrialFormat,
    as_dict,
    as_nonneg_int,
    as_str,
    extract_rewards,
    parse_timestamp_epoch,
    read_json_object,
    safe_fsum,
    safe_regular_file,
)
from ._olam_members import (
    _is_no_model_agent,
    _read_olam_members,
    _read_single_agent_member,
    _resolve_member_usage,
)

__all__ = [
    "CostStatus",
    "GradeMarkRecord",
    "MemberRecord",
    "MemberUsage",
    "NormalizedAtifStep",
    "NormalizedMultiAgentTrial",
    "NormalizedObservation",
    "NormalizedToolCall",
    "TrajectoryStatus",
    "TrialFormat",
    "_is_no_model_agent",
    "_resolve_member_usage",
    "detect_trial_format",
    "discover_trial_dirs",
    "is_job_dir",
    "is_trial_dir",
    "parse_timestamp_epoch",
    "read_multiagent_trial",
]


class NormalizedMultiAgentTrial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.multiagent-trial/v1"
    trial_dir: str
    trial_name: str
    trial_id: str | None = None
    task_name: str | None = None
    task_checksum: str | None = None
    ecosystem: TrialFormat
    orchestration: str | None = None
    end_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    agent_execution_started_at: str | None = None
    agent_execution_finished_at: str | None = None
    rewards: dict[str, float] | None = None
    reward_valid: bool | None = None
    reward_source: str | None = None
    reward_file_mismatch: bool = False
    reward_problems: list[str] = Field(default_factory=list)
    trial_exception_type: str | None = None
    trial_exception_message: str | None = None
    members: list[MemberRecord] = Field(default_factory=list)
    member_steps: dict[str, list[NormalizedAtifStep]] = Field(default_factory=dict)
    total_input_tokens: int | None = None
    total_cache_tokens: int | None = None
    total_output_tokens: int | None = None
    total_cost_usd: float | None = None
    known_cost_usd: float | None = None
    cost_complete: bool = False
    cost_source: str | None = None
    unauthenticated_proxy_requests: int = 0
    member_aliases: dict[str, str] = Field(default_factory=dict)
    grade_marks: list[GradeMarkRecord] = Field(default_factory=list)
    board_policy: str | None = None
    comms_disabled: bool = False
    resumed_from: str | None = None
    restored_usd: float | None = None
    anomalies: list[str] = Field(default_factory=list)


def _is_job_result(data: dict[str, Any] | None) -> bool:
    return bool(data and ("n_total_trials" in data or "stats" in data))


def _is_trial_result(data: dict[str, Any] | None) -> bool:
    return bool(data and ("trial_name" in data or "seat_results" in data))


def is_job_dir(path: Path) -> bool:
    data, err = read_json_object(safe_regular_file(path, "result.json"))
    return err is None and _is_job_result(data)


def _merged_agent_meta(result: dict[str, Any] | None) -> dict[str, Any]:
    raw_meta = as_dict(as_dict(result.get("agent_result")).get("metadata")) if result else {}
    nested_ma = as_dict(raw_meta.get("multi_agent"))
    return {**nested_ma, **raw_meta} if nested_ma else raw_meta


def is_trial_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    data, err = read_json_object(safe_regular_file(path, "result.json"))
    if err is None and _is_job_result(data):
        return False
    if err is None and _is_trial_result(data):
        return True
    if is_regular_dir(path / "seats"):
        return True
    if any(safe_regular_file(path, *parts) is not None for parts in BESPOKE_BOARD_PATHS):
        return True
    if any(safe_regular_file(path, *parts) is not None for parts in BESPOKE_PROXY_PATHS):
        return True
    if any(
        safe_regular_file(path, "agent", fname) is not None
        for fname in ("proxy-usage.json", "subscription-usage.json", "trajectory.json")
    ):
        return True
    if any(safe_regular_file(path, *parts) is not None for parts in BESPOKE_BUNDLE_PATHS):
        return True
    agent_dir = path / "agent"
    if is_regular_dir(agent_dir):
        for child in safe_iterdir(agent_dir):
            if is_regular_dir(child) and safe_regular_file(child, "trajectory.json") is not None:
                return True
    return is_regular_dir(path / "verifier") and (
        is_regular_dir(path / "agent") or safe_regular_file(path, "config.json") is not None
    )


def discover_trial_dirs(path: Path) -> tuple[list[Path], str | None, list[str]]:
    root = path.resolve()
    if not root.is_dir():
        raise ValueError(f"path is not a directory: {path}")
    if is_trial_dir(root):
        return [root], None, []

    issues: list[str] = []
    job_id: str | None = None
    top_res, top_err = read_json_object(safe_regular_file(root, "result.json"))
    if top_err is None and _is_job_result(top_res):
        assert top_res is not None
        job_id = as_str(top_res.get("id"))
        trials = [
            child
            for child in safe_iterdir(root)
            if not child.name.startswith(".") and is_regular_dir(child) and is_trial_dir(child)
        ]
        expected = as_nonneg_int(top_res.get("n_total_trials"))
        if expected is not None and len(trials) != expected:
            issues.append(f"job_trial_count_mismatch:expected={expected},found={len(trials)}")
        return trials, job_id, issues

    discovered: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    visited_dirs = 0
    while stack and visited_dirs < 50_000:
        directory, depth = stack.pop()
        visited_dirs += 1
        if depth >= 16:
            continue
        children = [
            c for c in safe_iterdir(directory) if not c.name.startswith(".") and is_regular_dir(c)
        ]
        pending: list[tuple[Path, int]] = []
        for child in children:
            if is_trial_dir(child):
                discovered.append(child)
            else:
                pending.append((child, depth + 1))
        for item in reversed(pending):
            stack.append(item)
    return discovered, job_id, issues


def _detect_trial_format_loaded(
    root: Path,
    result: dict[str, Any] | None,
    config: dict[str, Any] | None,
    lock: dict[str, Any] | None,
) -> TrialFormat:
    if result and isinstance(result.get("seat_results"), list) and result["seat_results"]:
        return "olam_seats"
    for source in (as_dict(result.get("config")) if result else {}, config or {}, lock or {}):
        if isinstance(source.get("participants"), list) and source["participants"]:
            return "olam_seats"
    seats_dir = root / "seats"
    if is_regular_dir(seats_dir):
        for child in safe_iterdir(seats_dir):
            if is_regular_dir(child) and is_regular_dir(child / "agent"):
                return "olam_seats"

    if any(safe_regular_file(root, *parts) is not None for parts in BESPOKE_BOARD_PATHS):
        return "bespoke_team"
    if any(safe_regular_file(root, *parts) is not None for parts in BESPOKE_PROXY_PATHS):
        return "bespoke_team"
    if any(
        safe_regular_file(root, "agent", fname) is not None
        for fname in ("proxy-usage.json", "subscription-usage.json")
    ):
        return "bespoke_team"
    if any(safe_regular_file(root, *parts) is not None for parts in BESPOKE_BUNDLE_PATHS):
        return "bespoke_team"
    if result:
        agent_info = as_dict(result.get("agent_info"))
        agent_cfg = as_dict(as_dict(result.get("config")).get("agent"))
        markers = (
            as_str(agent_info.get("name")) or "",
            as_str(agent_cfg.get("import_path")) or "",
        )
        if any("teamagent" in m.lower() or "multi_agent_harness" in m.lower() for m in markers):
            return "bespoke_team"
        meta = _merged_agent_meta(result)
        raw_agents = meta.get("agents")
        has_agents = bool(raw_agents) and isinstance(raw_agents, (dict, list))
        if has_agents and any(
            k in meta
            for k in ("orchestration", "cost_source", "agent_names", "harness", "board_policy")
        ):
            return "bespoke_team"
    agent_dir = root / "agent"
    if is_regular_dir(agent_dir):
        member_trajs = [
            c
            for c in safe_iterdir(agent_dir)
            if is_regular_dir(c)
            and c.name not in BESPOKE_BOOKKEEPING_DIRS
            and safe_regular_file(c, "trajectory.json") is not None
        ]
        if member_trajs and safe_regular_file(root, "agent", "trajectory.json") is None:
            return "bespoke_team"
    return "single_agent"


def detect_trial_format(trial_dir: Path) -> TrialFormat:
    root = trial_dir.resolve()
    result, _ = read_json_object(safe_regular_file(root, "result.json"))
    config, _ = read_json_object(safe_regular_file(root, "config.json"))
    lock, _ = read_json_object(safe_regular_file(root, "lock.json"))
    return _detect_trial_format_loaded(root, result, config, lock)


def _roll_up_usage(
    members: list[MemberRecord], anomalies: list[str]
) -> tuple[int | None, int | None, int | None, float | None, float | None, bool]:
    model_m = [m for m in members if m.usage.cost_status != "zero_no_model"]
    in_vals = [m.usage.n_input_tokens for m in members if m.usage.n_input_tokens is not None]
    ca_vals = [m.usage.n_cache_tokens for m in members if m.usage.n_cache_tokens is not None]
    ou_vals = [m.usage.n_output_tokens for m in members if m.usage.n_output_tokens is not None]
    costs = [m.usage.cost_usd for m in members if m.usage.cost_usd is not None]
    raw_sum = safe_fsum(costs) if costs else None
    if costs and raw_sum is None:
        anomalies.append("cost_sum_overflow")
    known_cost = round(raw_sum, 9) if raw_sum is not None else None
    complete = (
        bool(members)
        and (not costs or raw_sum is not None)
        and all(m.usage.cost_status in ("recorded", "zero_no_model") for m in members)
    )
    total_cost: float | None = (known_cost if known_cost is not None else 0.0) if complete else None
    in_ok = bool(in_vals) and all(m.usage.n_input_tokens is not None for m in model_m)
    ca_ok = bool(ca_vals) and all(m.usage.n_cache_tokens is not None for m in model_m)
    ou_ok = bool(ou_vals) and all(m.usage.n_output_tokens is not None for m in model_m)
    return (
        sum(in_vals) if in_ok else None,
        sum(ca_vals) if ca_ok else None,
        sum(ou_vals) if ou_ok else None,
        total_cost,
        known_cost,
        complete,
    )


def _load_ecosystem_members(
    ecosystem: TrialFormat,
    root: Path,
    result: dict[str, Any] | None,
    config: dict[str, Any] | None,
    lock: dict[str, Any] | None,
    anomalies: list[str],
) -> tuple[list[MemberRecord], dict[str, list[NormalizedAtifStep]], BespokeExtraContext]:
    if ecosystem == "olam_seats":
        members, steps = _read_olam_members(root, result, config, lock, anomalies)
        return members, steps, BespokeExtraContext()
    if ecosystem == "bespoke_team":
        return read_bespoke_members(root, result, anomalies)
    members, steps = _read_single_agent_member(root, result)
    return members, steps, BespokeExtraContext()


def read_multiagent_trial(trial_dir: Path) -> NormalizedMultiAgentTrial:
    root = trial_dir.resolve()
    if not root.is_dir():
        raise ValueError(f"trial directory does not exist: {trial_dir}")
    anomalies: list[str] = []
    result, res_err = read_json_object(safe_regular_file(root, "result.json"))
    if res_err is not None:
        anomalies.append(f"result_json:{res_err}")
    config, _ = read_json_object(safe_regular_file(root, "config.json"))
    lock, _ = read_json_object(safe_regular_file(root, "lock.json"))
    ecosystem = _detect_trial_format_loaded(root, result, config, lock)
    members, member_steps, bespoke_extra = _load_ecosystem_members(
        ecosystem, root, result, config, lock, anomalies
    )
    rewards, reward_valid, reward_source, reward_mismatch, reward_problems = extract_rewards(
        root, result
    )
    t_in, t_ca, t_ou, t_cost, known_cost, cost_complete = _roll_up_usage(members, anomalies)
    if (
        ecosystem == "bespoke_team"
        and not cost_complete
        and bespoke_extra.fallback_total_cost_usd is not None
        and all(m.usage.cost_status != "partial" for m in members)
    ):
        t_cost = known_cost = round(bespoke_extra.fallback_total_cost_usd, 9)
        cost_complete = True
    meta = _merged_agent_meta(result)
    exc = as_dict(result.get("exception_info")) if result else {}
    agent_exec = as_dict(result.get("agent_execution")) if result else {}
    cost_sources = sorted({m.usage.cost_source for m in members if m.usage.cost_source})
    res_map = result or {}
    return NormalizedMultiAgentTrial(
        trial_dir=root.as_posix(),
        trial_name=as_str(res_map.get("trial_name")) or root.name,
        trial_id=as_str(res_map.get("id")),
        task_name=as_str(res_map.get("task_name")),
        task_checksum=as_str(res_map.get("task_checksum")),
        ecosystem=ecosystem,
        orchestration=as_str(meta.get("orchestration")),
        end_reason=as_str(meta.get("end_reason")),
        started_at=as_str(res_map.get("started_at")),
        finished_at=as_str(res_map.get("finished_at")),
        agent_execution_started_at=as_str(agent_exec.get("started_at")),
        agent_execution_finished_at=as_str(agent_exec.get("finished_at")),
        rewards=rewards,
        reward_valid=reward_valid,
        reward_source=reward_source,
        reward_file_mismatch=reward_mismatch,
        reward_problems=reward_problems,
        trial_exception_type=as_str(exc.get("exception_type")),
        trial_exception_message=as_str(exc.get("exception_message")),
        members=members,
        member_steps=member_steps,
        total_input_tokens=t_in,
        total_cache_tokens=t_ca,
        total_output_tokens=t_ou,
        total_cost_usd=t_cost,
        known_cost_usd=known_cost,
        cost_complete=cost_complete,
        cost_source=as_str(meta.get("cost_source"))
        or ("+".join(cost_sources) if cost_sources else None),
        unauthenticated_proxy_requests=bespoke_extra.unauthenticated_proxy_requests,
        member_aliases=bespoke_extra.member_aliases,
        grade_marks=bespoke_extra.grade_marks,
        board_policy=bespoke_extra.board_policy,
        comms_disabled=bespoke_extra.comms_disabled,
        resumed_from=bespoke_extra.resumed_from,
        restored_usd=bespoke_extra.restored_usd,
        anomalies=sorted(set(anomalies)),
    )
