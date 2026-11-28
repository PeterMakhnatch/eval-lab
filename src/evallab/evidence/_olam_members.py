from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any, cast

from ._bespoke_adapter import is_regular_dir, safe_iterdir
from ._multiagent_io import (
    CostStatus,
    MemberRecord,
    MemberUsage,
    NormalizedAtifStep,
    as_dict,
    as_nonneg_float,
    as_nonneg_int,
    as_str,
    parse_atif_file,
    parse_timestamp_epoch,
    safe_regular_file,
    usage_mismatches,
    valid_member_name,
)

_NO_MODEL_AGENTS = frozenset({"oracle", "nop", "script", "scripted"})
_ASCII_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_STALL_GAP_SEC = 300.0


def _is_no_model_agent(agent_name: str | None) -> bool:
    if agent_name is None:
        return False
    lowered = agent_name.lower()
    return lowered in _NO_MODEL_AGENTS or lowered.endswith(("scriptagent", "scriptedagent"))


def _resolve_member_usage(
    agent_res: dict[str, Any],
    f_metrics: dict[str, Any],
    *,
    primary_source: str,
    is_known_no_model: bool,
) -> MemberUsage:
    n_in = as_nonneg_int(agent_res.get("n_input_tokens"))
    if n_in is None:
        n_in = as_nonneg_int(f_metrics.get("total_prompt_tokens"))
    n_ca = as_nonneg_int(agent_res.get("n_cache_tokens"))
    if n_ca is None:
        n_ca = as_nonneg_int(f_metrics.get("total_cached_tokens"))
    n_ou = as_nonneg_int(agent_res.get("n_output_tokens"))
    if n_ou is None:
        n_ou = as_nonneg_int(f_metrics.get("total_completion_tokens"))
    cost = as_nonneg_float(agent_res.get("cost_usd"))
    cost_source: str | None = primary_source if cost is not None else None
    if cost is None:
        fb_cost = as_nonneg_float(f_metrics.get("total_cost_usd"))
        if fb_cost is not None:
            cost, cost_source = fb_cost, "trajectory_fallback"
    if cost is not None:
        cost_status: CostStatus = "recorded"
    elif is_known_no_model:
        cost_status, cost_source = "zero_no_model", "none_no_model"
    else:
        cost_status = "missing"
    return MemberUsage(
        n_input_tokens=n_in,
        n_cache_tokens=n_ca,
        n_output_tokens=n_ou,
        cost_usd=cost,
        cost_status=cost_status,
        cost_source=cost_source,
        usage_mismatches=usage_mismatches(agent_res, f_metrics),
    )


def _collect_olam_declared_maps(
    result: dict[str, Any] | None,
    config: dict[str, Any] | None,
    lock: dict[str, Any] | None,
    anomalies: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    participants_map: dict[str, dict[str, Any]] = {}
    saw_primary_source = False
    for source in (as_dict(result.get("config")) if result else {}, config or {}, lock or {}):
        raw_parts = source.get("participants")
        if isinstance(raw_parts, list) and raw_parts:
            seen_in_src: set[str] = set()
            for item in raw_parts:
                if not isinstance(item, dict):
                    if not saw_primary_source:
                        anomalies.append("invalid_participant_entry")
                    continue
                seat = valid_member_name(item.get("seat"))
                if seat is None:
                    if not saw_primary_source:
                        anomalies.append("invalid_participant_seat")
                    continue
                if seat in seen_in_src:
                    if not saw_primary_source:
                        anomalies.append(f"duplicate_participant:{seat}")
                    continue
                seen_in_src.add(seat)
                if seat not in participants_map:
                    participants_map[seat] = cast(dict[str, Any], item)
                else:
                    merged = {**item, **participants_map[seat]}
                    participants_map[seat] = cast(dict[str, Any], merged)
            saw_primary_source = True

    seat_results_map: dict[str, dict[str, Any]] = {}
    raw_results = result.get("seat_results") if result else None
    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                anomalies.append("invalid_seat_result_entry")
                continue
            seat = valid_member_name(item.get("seat"))
            if seat is None:
                anomalies.append("invalid_seat_result_name")
                continue
            if seat in seat_results_map:
                anomalies.append(f"duplicate_seat_result:{seat}")
                continue
            seat_results_map[seat] = cast(dict[str, Any], item)
    return participants_map, seat_results_map


def _resolve_olam_roster(
    root: Path,
    participants_map: dict[str, dict[str, Any]],
    seat_results_map: dict[str, dict[str, Any]],
    anomalies: list[str],
    *,
    has_result_doc: bool = False,
) -> list[str]:
    roster = sorted(set(participants_map) | set(seat_results_map))
    for seat in participants_map:
        if (seat_results_map or has_result_doc) and seat not in seat_results_map:
            anomalies.append(f"missing_seat_result:{seat}")
    for seat in seat_results_map:
        if participants_map and seat not in participants_map:
            anomalies.append(f"undeclared_seat_result:{seat}")

    seats_dir = root / "seats"
    had_declared = bool(roster)
    if is_regular_dir(seats_dir):
        for child in safe_iterdir(seats_dir):
            if not is_regular_dir(child):
                continue
            valid = valid_member_name(child.name)
            if valid is None:
                anomalies.append(f"unsafe_seat_dir:{child.name}")
            elif had_declared and valid not in roster:
                anomalies.append(f"undeclared_seat_dir:{valid}")
            elif not had_declared:
                roster.append(valid)
                anomalies.append(f"seat_from_dir_only:{valid}")
        roster = sorted(set(roster))
    return roster


def _inspect_olam_exit_code(
    root: Path, seat: str, anomalies: list[str]
) -> tuple[int | None, str | None]:
    ec_path = root / "seats" / seat / "agent" / "exit-code.txt"
    try:
        st = ec_path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError:
        anomalies.append(f"invalid_seat_exit_code:{seat}")
        return None, "NonZeroExitCode(invalid)"
    if not stat.S_ISREG(st.st_mode) or st.st_size > 1024:
        anomalies.append(f"invalid_seat_exit_code:{seat}")
        return None, "NonZeroExitCode(invalid)"
    try:
        txt = ec_path.read_text(encoding="utf-8").strip()
        if not _ASCII_INT_RE.fullmatch(txt):
            raise ValueError("non-ascii-int")
        code = int(txt)
    except (OSError, UnicodeDecodeError, ValueError):
        anomalies.append(f"invalid_seat_exit_code:{seat}")
        return None, "NonZeroExitCode(invalid)"
    if code != 0:
        anomalies.append(f"nonzero_seat_exit:{seat}:{code}")
        return code, f"NonZeroExitCode({code})"
    return 0, None


def _check_olam_stall(
    seat: str,
    raw_exc: str | None,
    is_stall_eligible_model: bool,
    steps: list[NormalizedAtifStep],
    agent_exec: dict[str, Any],
    trial_end_ep: float | None,
    trial_fb_ep: float | None,
    anomalies: list[str],
) -> None:
    if raw_exc != "AgentTimeoutError" or not is_stall_eligible_model:
        return
    ae_ep = parse_timestamp_epoch(agent_exec.get("finished_at"))
    end_ep = (
        trial_end_ep if trial_end_ep is not None else (ae_ep if ae_ep is not None else trial_fb_ep)
    )
    step_eps = [s.ts_epoch for s in steps if s.ts_epoch is not None]
    if end_ep is None or not step_eps or (end_ep - max(step_eps)) >= _STALL_GAP_SEC:
        anomalies.append(f"stalled_seat_timeout:{seat}")


def _resolve_olam_identity(
    agent_info: dict[str, Any], part: dict[str, Any], traj_model: str | None
) -> tuple[str | None, str | None, str | None]:
    model_info = as_dict(agent_info.get("model_info"))
    part_agent = as_dict(part.get("agent"))
    agent_name = (
        as_str(agent_info.get("name"))
        or as_str(part.get("name"))
        or as_str(part_agent.get("name"))
        or as_str(part.get("import_path"))
        or as_str(part_agent.get("import_path"))
    )
    model_name = (
        as_str(model_info.get("name"))
        or as_str(part.get("model_name"))
        or as_str(part_agent.get("model_name"))
        or traj_model
    )
    model_provider = as_str(model_info.get("provider"))
    if model_provider is None and model_name and "/" in model_name:
        model_provider = model_name.split("/", 1)[0]
    return agent_name, model_name, model_provider


def _build_olam_member(
    root: Path,
    seat: str,
    sr: dict[str, Any],
    part: dict[str, Any],
    anomalies: list[str],
    trial_end_ep: float | None,
    trial_fb_ep: float | None,
) -> tuple[MemberRecord, list[NormalizedAtifStep]]:
    agent_res = as_dict(sr.get("agent_result"))
    agent_exec = as_dict(sr.get("agent_execution"))
    exc = as_dict(sr.get("exception_info"))
    traj_file = safe_regular_file(root, "seats", seat, "agent", "trajectory.json")
    oracle_present = safe_regular_file(root, "seats", seat, "agent", "oracle.txt") is not None
    t_status, t_rel, t_issues, t_ver, steps, f_metrics, traj_model = parse_atif_file(
        seat, traj_file, root
    )
    agent_name, model_name, model_provider = _resolve_olam_identity(
        as_dict(sr.get("agent_info")), part, traj_model
    )
    has_model = bool(model_name or model_provider)
    is_known_no_model = agent_name is not None and _is_no_model_agent(agent_name) and not has_model
    ran_model = has_model or (agent_name is not None and not _is_no_model_agent(agent_name))
    usage = _resolve_member_usage(
        agent_res, f_metrics, primary_source="seat_result", is_known_no_model=is_known_no_model
    )
    exit_code, ec_exc = _inspect_olam_exit_code(root, seat, anomalies)
    raw_exc = as_str(exc.get("exception_type"))
    is_stall_eligible = not is_known_no_model and (ran_model or not oracle_present)
    _check_olam_stall(
        seat, raw_exc, is_stall_eligible, steps, agent_exec, trial_end_ep, trial_fb_ep, anomalies
    )
    under_test, logs_dl = sr.get("under_test"), sr.get("logs_downloaded")
    n_tc = sum(len(s.tool_calls) for s in steps) if t_status == "present" else None
    record = MemberRecord(
        member_id=seat,
        under_test=under_test if isinstance(under_test, bool) else None,
        agent_name=agent_name,
        model_name=model_name,
        model_provider=model_provider,
        ran_model=ran_model,
        logs_downloaded=logs_dl if isinstance(logs_dl, bool) else None,
        trajectory_path=t_rel,
        trajectory_status=t_status,
        trajectory_issues=t_issues,
        atif_schema_version=t_ver,
        n_steps=len(steps) if t_status == "present" else None,
        n_tool_calls=n_tc,
        oracle_log_present=oracle_present,
        started_at=as_str(agent_exec.get("started_at")),
        finished_at=as_str(agent_exec.get("finished_at")),
        exception_type=raw_exc or ec_exc,
        exception_message=as_str(exc.get("exception_message")),
        exit_code=exit_code,
        usage=usage,
    )
    return record, steps


def _read_olam_members(
    root: Path,
    result: dict[str, Any] | None,
    config: dict[str, Any] | None,
    lock: dict[str, Any] | None,
    anomalies: list[str],
) -> tuple[list[MemberRecord], dict[str, list[NormalizedAtifStep]]]:
    participants_map, seat_results_map = _collect_olam_declared_maps(
        result, config, lock, anomalies
    )
    roster = _resolve_olam_roster(
        root, participants_map, seat_results_map, anomalies, has_result_doc=result is not None
    )
    t_end = (
        parse_timestamp_epoch(as_dict(result.get("agent_execution")).get("finished_at"))
        if result
        else None
    )
    t_fb = parse_timestamp_epoch(result.get("finished_at")) if result else None
    members: list[MemberRecord] = []
    member_steps: dict[str, list[NormalizedAtifStep]] = {}
    for seat in roster:
        rec, steps = _build_olam_member(
            root,
            seat,
            seat_results_map.get(seat, {}),
            participants_map.get(seat, {}),
            anomalies,
            t_end,
            t_fb,
        )
        members.append(rec)
        member_steps[seat] = steps
    return members, member_steps


def _read_single_agent_member(
    root: Path, result: dict[str, Any] | None
) -> tuple[list[MemberRecord], dict[str, list[NormalizedAtifStep]]]:
    agent_info = as_dict(result.get("agent_info")) if result else {}
    model_info = as_dict(agent_info.get("model_info"))
    agent_res = as_dict(result.get("agent_result")) if result else {}
    agent_exec = as_dict(result.get("agent_execution")) if result else {}
    exc = as_dict(result.get("exception_info")) if result else {}
    agent_name = as_str(agent_info.get("name"))
    model_name = as_str(model_info.get("name"))
    model_provider = as_str(model_info.get("provider"))

    traj_file = safe_regular_file(root, "agent", "trajectory.json")
    oracle_present = safe_regular_file(root, "agent", "oracle.txt") is not None
    t_status, t_rel, t_issues, t_ver, steps, f_metrics, traj_model = parse_atif_file(
        "agent", traj_file, root
    )
    if model_name is None and traj_model is not None:
        model_name = traj_model
    has_model = bool(model_name or model_provider)
    is_known_no_model = agent_name is not None and _is_no_model_agent(agent_name) and not has_model
    ran_model = has_model or (agent_name is not None and not _is_no_model_agent(agent_name))
    usage = _resolve_member_usage(
        agent_res, f_metrics, primary_source="agent_result", is_known_no_model=is_known_no_model
    )
    member = MemberRecord(
        member_id="agent",
        under_test=True,
        agent_name=agent_name,
        model_name=model_name,
        model_provider=model_provider,
        ran_model=ran_model,
        trajectory_path=t_rel,
        trajectory_status=t_status,
        trajectory_issues=t_issues,
        atif_schema_version=t_ver,
        n_steps=len(steps) if t_status == "present" else None,
        n_tool_calls=sum(len(s.tool_calls) for s in steps) if t_status == "present" else None,
        oracle_log_present=oracle_present,
        started_at=as_str(agent_exec.get("started_at")),
        finished_at=as_str(agent_exec.get("finished_at")),
        exception_type=as_str(exc.get("exception_type")),
        exception_message=as_str(exc.get("exception_message")),
        usage=usage,
    )
    return [member], {"agent": steps}
