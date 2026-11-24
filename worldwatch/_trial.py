from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from worldwatch._io import (
    read_regular_text,
    read_tolerant_json_object,
    stat_regular_file,
)
from worldwatch._trial_bespoke import (
    bespoke_exec_bounds,
    classify_bespoke_seat_attrs,
    discover_bespoke_slots,
    parse_bespoke_exit,
)
from worldwatch.model import (
    MAX_SEATS,
    SEAT_NAME_RE,
    finite_float,
    is_no_model_name,
    nonneg_int,
    parse_iso_utc,
)

_EXIT_INT_RE = re.compile(r"^[+-]?\d{1,6}$")
_COMPLETE_TOOLS = frozenset({"mark_task_complete", "task_complete"})
INVALID_EXCEPTIONS = frozenset({
    "EnvironmentStartTimeoutError",
    "SandboxBuildFailedError",
    "AgentSetupTimeoutError",
    "HealthcheckError",
    "SandboxLikelyOutOfMemoryError",
    "MemoryLimitExceededError",
    "SSHAccessError",
    "RuntimeRequestError",
    "ApiError",
    "ApiRateLimitError",
    "ApiInternalServerError",
    "ApiOverloadedError",
    "ApiConnectionClosedError",
    "ApiResponseStalledError",
    "UnknownApiError",
    "NetworkConnectionError",
    "ApiConnectionError",
    "ApiUsageLimitError",
    "RateLimitError",
    "InternalServerError",
    "ServiceUnavailableError",
    "APIConnectionError",
    "APIError",
    "Timeout",
    "BadRequestError",
    "BudgetExceededError",
    "VerifierTimeoutError",
    "RewardFileNotFoundError",
    "RewardFileEmptyError",
    "VerifierOutputParseError",
    "AgentAuthenticationError",
    "ModelNotFoundError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "CancelledError",
})


@dataclass(frozen=True, slots=True)
class AtifSummary:
    path: Path
    problem: str | None = None
    agent_steps: int = 0
    llm_calls: int = 0
    last_step_at: datetime | None = None
    marked_complete: bool = False
    tool_calls: int = 0
    tool_errors: int = 0
    cost_usd: float | None = None
    model_name: str | None = None


@dataclass(frozen=True, slots=True)
class SeatInfo:
    seat: str
    facing: str
    under_test: bool
    agent_name: str | None
    model_name: str | None
    is_model: bool
    exception: str | None
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    exit_error: str | None
    in_seat_results: bool
    atif: AtifSummary | None


@dataclass(frozen=True, slots=True)
class TrialBundle:
    path: Path
    trial_name: str
    task_name: str
    ecosystem: str
    result_state: str
    result_error: str | None
    reward: float | None
    rewards: dict[str, float]
    reward_valid: bool
    exception: str | None
    not_started: str | None
    started_at: datetime | None
    finished_at: datetime | None
    agent_started_at: datetime | None
    agent_finished_at: datetime | None
    cost_usd: float | None
    seats: tuple[SeatInfo, ...]
    facing_to_seat: dict[str, str] = field(default_factory=dict)


def _as_dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _valid_seat_token(name: str) -> bool:
    return bool(SEAT_NAME_RE.match(name)) and name not in (".", "..")


def _read_exit_code(agent_dir: Path, root: Path | None = None) -> tuple[int | None, str | None]:
    target = agent_dir / "exit-code.txt"
    if not os.path.lexists(target):
        return None, None
    text, err = read_regular_text(target, max_bytes=1024, root=root)
    if err is not None or text is None:
        return None, err or "unreadable exit-code.txt"
    token = text.strip()
    if not _EXIT_INT_RE.match(token):
        return None, f"invalid exit code {token[:24]!r}"
    return int(token), None


def _is_step_result_error(item: Mapping[str, Any]) -> bool:
    if item.get("is_error") is True:
        return True
    err = item.get("error")
    if isinstance(err, str) and bool(err) and item.get("is_error") is not False:
        return True
    content = item.get("content")
    if not isinstance(content, str) or not content:
        return False
    stripped = content.lstrip().lower()
    head = stripped.split("\n\nnew terminal output:", 1)[0]
    if head.startswith("previous response had parsing errors:"):
        return True
    if head.startswith("error: no valid json found in response"):
        return True
    return head.startswith("previous response had warnings:") and (
        "should end with newline when followed by another command" in head
    )


def _count_step_tools(step: Mapping[str, Any]) -> tuple[int, int, bool]:
    if step.get("is_copied_context") is True:
        return 0, 0, False
    calls = step.get("tool_calls")
    call_list = [c for c in calls if isinstance(c, Mapping)] if isinstance(calls, list) else []
    completed = any(c.get("function_name") in _COMPLETE_TOOLS for c in call_list)
    obs = _as_dict(step.get("observation"))
    results = obs.get("results")
    res_list = [r for r in results if isinstance(r, Mapping)] if isinstance(results, list) else []
    errors = sum(1 for item in res_list if _is_step_result_error(item))
    return len(call_list), errors, completed


def read_atif_summary(traj_path: Path, root: Path | None = None) -> AtifSummary | None:
    if not os.path.lexists(traj_path):
        return None
    info, err = stat_regular_file(traj_path, root=root)
    if err is not None:
        return AtifSummary(path=traj_path, problem=err)
    if info is None:
        return None
    data, jerr = read_tolerant_json_object(traj_path, root=root)
    if jerr is not None or data is None:
        return AtifSummary(path=traj_path, problem=jerr or "unreadable trajectory")
    if not isinstance(data.get("schema_version"), str):
        return AtifSummary(path=traj_path, problem="missing schema_version")
    raw_steps = data.get("steps")
    steps = [s for s in raw_steps if isinstance(s, Mapping)] if isinstance(raw_steps, list) else []
    agent_steps = llm_calls = tool_calls = tool_errors = 0
    latest: datetime | None = None
    last_complete = False
    step_cost = 0.0
    has_step_cost = False
    for step in steps[:100_000]:
        if step.get("is_copied_context") is True:
            continue
        ts = parse_iso_utc(step.get("timestamp"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
        if step.get("source") == "agent":
            agent_steps += 1
            metrics = _as_dict(step.get("metrics"))
            declared = nonneg_int(step.get("llm_call_count"))
            if declared is not None:
                llm_calls += declared
            elif nonneg_int(metrics.get("prompt_tokens")) is not None:
                llm_calls += 1
            sc = finite_float(metrics.get("cost_usd"))
            if sc is not None and sc >= 0:
                step_cost += sc
                has_step_cost = True
            tc, te, comp = _count_step_tools(step)
            tool_calls += tc
            tool_errors += te
            last_complete = comp
        else:
            _, te, _ = _count_step_tools(step)
            tool_errors += te
    fm = _as_dict(data.get("final_metrics"))
    total_cost = finite_float(fm.get("total_cost_usd"))
    if total_cost is None and has_step_cost:
        total_cost = round(step_cost, 9)
    ag = _as_dict(data.get("agent"))
    model_nm = ag.get("model_name")
    return AtifSummary(
        path=traj_path,
        agent_steps=agent_steps,
        llm_calls=llm_calls,
        last_step_at=latest,
        marked_complete=last_complete,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        cost_usd=total_cost if total_cost is not None and total_cost >= 0 else None,
        model_name=model_nm if isinstance(model_nm, str) and model_nm else None,
    )


def _extract_rewards(trial_dir: Path, result: Mapping[str, Any]) -> tuple[dict[str, float], float | None, bool]:
    verifier = _as_dict(result.get("verifier_result"))
    raw = verifier.get("rewards")
    if not isinstance(raw, Mapping):
        single = verifier.get("reward")
        raw = {"reward": single} if single is not None else {}
    if not raw and os.path.lexists(trial_dir / "verifier" / "reward.json"):
        fallback, err = read_tolerant_json_object(trial_dir / "verifier" / "reward.json", root=trial_dir)
        if err is None and fallback is not None:
            raw = fallback
    rewards: dict[str, float] = {}
    for key, val in raw.items():
        num = finite_float(val)
        if num is not None and not (key == "reward" and num == -1.0 and result.get("reward_valid") is False):
            rewards[str(key)] = num
    primary = rewards.get("reward")
    if primary is None and len(rewards) == 1:
        primary = next(iter(rewards.values()))
    stored_valid = result.get("reward_valid")
    if stored_valid is False or not rewards or primary is None or primary == -1.0:
        return rewards, (None if primary == -1.0 else primary), False
    exc_type = _as_dict(result.get("exception_info")).get("exception_type")
    if exc_type in INVALID_EXCEPTIONS and stored_valid is not True:
        return rewards, primary, False
    for item in result.get("seat_results") or []:
        se = _as_dict(_as_dict(item).get("exception_info")).get("exception_type")
        if se in INVALID_EXCEPTIONS and stored_valid is not True:
            return rewards, primary, False
    return rewards, primary, True


def _single_olam_seat(
    trial_dir: Path,
    result: Mapping[str, Any],
    cfg_doc: Mapping[str, Any],
) -> tuple[SeatInfo, ...]:
    res_cfg = _as_dict(result.get("config"))
    ag_cfg = _as_dict(res_cfg.get("agent") or cfg_doc.get("agent"))
    ag_inf = _as_dict(result.get("agent_info"))
    ag_name = str(ag_inf.get("name") or ag_cfg.get("name") or ag_cfg.get("import_path") or "agent")
    mod_name = ag_cfg.get("model_name") or _as_dict(ag_inf.get("model_info")).get("name")
    atif = read_atif_summary(trial_dir / "agent" / "trajectory.json", root=trial_dir)
    ec, eerr = _read_exit_code(trial_dir / "agent", root=trial_dir)
    exc = _as_dict(result.get("exception_info")).get("exception_type")
    is_mod = not is_no_model_name(ag_name) and bool(mod_name or (atif and atif.llm_calls > 0))
    return (
        SeatInfo(
            seat="agent",
            facing="agent",
            under_test=True,
            agent_name=ag_name,
            model_name=str(mod_name) if isinstance(mod_name, str) else (atif.model_name if atif else None),
            is_model=is_mod,
            exception=str(exc) if isinstance(exc, str) else None,
            started_at=parse_iso_utc(_as_dict(result.get("agent_execution")).get("started_at")),
            finished_at=parse_iso_utc(_as_dict(result.get("agent_execution")).get("finished_at")),
            exit_code=ec,
            exit_error=eerr,
            in_seat_results=bool(result),
            atif=atif,
        ),
    )


def _olam_seat_entry(
    trial_dir: Path,
    name: str,
    part: Mapping[str, Any],
    rec: Mapping[str, Any],
    in_sr: bool,
) -> SeatInfo:
    ag_inf = _as_dict(rec.get("agent_info"))
    ag_res = _as_dict(rec.get("agent_result"))
    ag_name_raw = ag_inf.get("name") or part.get("name") or part.get("import_path")
    ag_name = str(ag_name_raw) if isinstance(ag_name_raw, str) and ag_name_raw else None
    mod_info = _as_dict(ag_inf.get("model_info"))
    mod_raw = part.get("model_name") or mod_info.get("name") or mod_info.get("provider")
    if _valid_seat_token(name):
        agent_dir = trial_dir / "seats" / name / "agent"
        atif = read_atif_summary(agent_dir / "trajectory.json", root=trial_dir)
        ec, eerr = _read_exit_code(agent_dir, root=trial_dir)
    else:
        atif, ec, eerr = None, None, f"invalid seat name {name[:24]!r}"
    model_str = str(mod_raw) if isinstance(mod_raw, str) and mod_raw else (atif.model_name if atif else None)
    no_mod = is_no_model_name(ag_name) or is_no_model_name(str(part.get("import_path") or ""))
    has_tok = finite_float(ag_res.get("n_input_tokens")) is not None
    is_mod = not no_mod and bool(model_str or has_tok or (ag_name and ag_name not in ("unknown", "-")))
    exc = _as_dict(rec.get("exception_info")).get("exception_type")
    timing = _as_dict(rec.get("agent_execution"))
    return SeatInfo(
        seat=name,
        facing=name,
        under_test=bool(rec.get("under_test", True)),
        agent_name=ag_name,
        model_name=model_str,
        is_model=is_mod,
        exception=str(exc) if isinstance(exc, str) else None,
        started_at=parse_iso_utc(timing.get("started_at")),
        finished_at=parse_iso_utc(timing.get("finished_at")),
        exit_code=ec,
        exit_error=eerr,
        in_seat_results=in_sr,
        atif=atif,
    )


def _build_olam_seats(
    trial_dir: Path,
    result: Mapping[str, Any],
    cfg_doc: Mapping[str, Any],
) -> tuple[SeatInfo, ...]:
    res_cfg = _as_dict(result.get("config"))
    parts_map: dict[str, Mapping[str, Any]] = {}
    for src in (cfg_doc.get("participants"), res_cfg.get("participants")):
        if isinstance(src, list):
            for item in src:
                if isinstance(item, Mapping) and isinstance(item.get("seat"), str) and item["seat"]:
                    parts_map[item["seat"]] = item
    sr_map: dict[str, Mapping[str, Any]] = {}
    raw_sr = result.get("seat_results")
    has_sr_list = isinstance(raw_sr, list)
    if has_sr_list:
        for item in raw_sr:
            if isinstance(item, Mapping) and isinstance(item.get("seat"), str) and item["seat"]:
                sr_map[item["seat"]] = item
    disk_seats: list[str] = []
    seats_root = trial_dir / "seats"
    if os.path.isdir(seats_root) and not os.path.islink(seats_root):
        try:
            disk_seats = sorted(
                e.name for e in os.scandir(seats_root)
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")
            )
        except OSError:
            disk_seats = []
    ordered_names = list(dict.fromkeys([*sr_map.keys(), *parts_map.keys(), *disk_seats]))[:MAX_SEATS]
    if not ordered_names:
        if "seat_results" in result and not has_sr_list:
            return (_olam_seat_entry(trial_dir, "seat-0", {}, {}, False),)
        return _single_olam_seat(trial_dir, result, cfg_doc)
    return tuple(
        _olam_seat_entry(
            trial_dir,
            name,
            parts_map.get(name, {}),
            sr_map.get(name, {}),
            bool(result) and has_sr_list and (name in sr_map),
        )
        for name in ordered_names
    )


def _build_bespoke_seats(
    trial_dir: Path,
    result: Mapping[str, Any],
) -> tuple[tuple[SeatInfo, ...], dict[str, str]]:
    raw_ar = result.get("agent_result")
    has_ar_map = isinstance(raw_ar, Mapping)
    meta = _as_dict(raw_ar.get("metadata")) if has_ar_map else {}
    agents_meta = _as_dict(meta.get("agents"))
    slots, alias_map, lost_set, time_up = discover_bespoke_slots(trial_dir, meta)
    facing_to_seat: dict[str, str] = {}
    seats: list[SeatInfo] = []
    ag_top = str(_as_dict(result.get("agent_info")).get("name") or meta.get("harness") or "TeamAgent")
    t_start, t_end = bespoke_exec_bounds(result)
    agent_root = trial_dir / "agent"
    for slot in slots:
        facing = alias_map.get(slot, slot)
        facing_to_seat[slot] = slot
        facing_to_seat[facing] = slot
        entry = _as_dict(agents_meta.get(slot))
        is_lost = slot in lost_set or facing in lost_set
        if _valid_seat_token(slot):
            atif = read_atif_summary(agent_root / slot / "trajectory.json", root=trial_dir)
            ec, eerr = parse_bespoke_exit(entry.get("exit"), is_lost)
            if ec is None and eerr is None:
                ec, eerr = _read_exit_code(agent_root / slot, root=trial_dir)
        else:
            atif, ec, eerr = None, None, f"invalid seat name {slot[:24]!r}"
        steps = atif.agent_steps if atif and atif.problem is None else 0
        harness, is_mod, seat_exc = classify_bespoke_seat_attrs(
            entry, ag_top, ec, eerr, is_lost, time_up, steps
        )
        mod_raw = entry.get("model")
        in_sr = (has_ar_map and (not agents_meta or slot in agents_meta)) if result else False
        seats.append(
            SeatInfo(
                seat=slot,
                facing=facing,
                under_test=True,
                agent_name=harness,
                model_name=str(mod_raw) if isinstance(mod_raw, str) else (atif.model_name if atif else None),
                is_model=is_mod,
                exception=seat_exc,
                started_at=t_start,
                finished_at=t_end,
                exit_code=ec,
                exit_error=eerr,
                in_seat_results=in_sr,
                atif=atif,
            )
        )
    return tuple(seats), facing_to_seat


def _extract_cost_usd(
    trial_dir: Path,
    ecosystem: str,
    result: Mapping[str, Any],
    seats: tuple[SeatInfo, ...],
    proxy_rows: list[dict[str, Any]] | None = None,
) -> float | None:
    if proxy_rows:
        vals = [u for r in proxy_rows if (u := finite_float(r.get("usd", r.get("cost_usd")))) is not None and u >= 0]
        if vals:
            return round(sum(vals), 9)
    for rel in ("agent/proxy-usage.json", "proxy-usage.json", "artifacts/proxy-usage.json"):
        pdoc, err = read_tolerant_json_object(trial_dir / rel, root=trial_dir)
        if err is None and pdoc is not None:
            tot = finite_float(_as_dict(pdoc.get("total")).get("usd"))
            if tot is None:
                tot = finite_float(pdoc.get("total_cost_usd"))
            if tot is not None and tot >= 0:
                return round(tot, 9)
    sub_doc, serr = read_tolerant_json_object(trial_dir / "agent" / "subscription-usage.json", root=trial_dir)
    if serr is None and sub_doc is not None and sub_doc.get("budget_enforced") is True:
        stot = finite_float(_as_dict(sub_doc.get("total")).get("usd"))
        if stot is not None and stot >= 0:
            return round(stot, 9)
    if ecosystem == "bespoke":
        meta = _as_dict(_as_dict(result.get("agent_result")).get("metadata"))
        if meta.get("billed_as") == "claude-subscription" and meta.get("budget_enforced") is not True:
            return None
        if meta.get("cost_source") == "trajectories":
            return None
        ag_m = _as_dict(meta.get("agents"))
        if ag_m:
            costs = [finite_float(_as_dict(v).get("cost_usd")) for v in ag_m.values()]
            if all(c is not None and c >= 0 for c in costs):
                return round(sum(c for c in costs if c is not None), 9)
    raw_sr = result.get("seat_results")
    if isinstance(raw_sr, list) and raw_sr:
        model_seats = {s.seat for s in seats if s.is_model}
        if not model_seats:
            return 0.0
        found: list[float] = []
        for rec in raw_sr:
            if not isinstance(rec, Mapping):
                continue
            if rec.get("seat") in model_seats:
                c = finite_float(_as_dict(rec.get("agent_result")).get("cost_usd"))
                if c is None or c < 0:
                    found = []
                    break
                found.append(c)
        if len(found) == len(model_seats):
            return round(sum(found), 9)
    single_c = finite_float(_as_dict(result.get("agent_result")).get("cost_usd"))
    if single_c is not None and single_c >= 0:
        return round(single_c, 9)
    atif_costs = [s.atif.cost_usd for s in seats if s.is_model and s.atif is not None]
    if atif_costs and all(c is not None and c >= 0 for c in atif_costs):
        return round(sum(c for c in atif_costs if c is not None), 9)
    if seats and all(not s.is_model for s in seats):
        return 0.0
    return None


def load_trial_bundle(
    trial_dir: Path,
    ecosystem: str,
    proxy_rows: list[dict[str, Any]] | None = None,
) -> TrialBundle:
    assert ecosystem in ("olam", "bespoke")
    cfg_doc, _ = read_tolerant_json_object(trial_dir / "config.json", root=trial_dir)
    cfg_map: Mapping[str, Any] = cfg_doc or {}
    res_path = trial_dir / "result.json"
    result_state = "missing"
    result_err: str | None = None
    res_map: Mapping[str, Any] = {}
    if os.path.lexists(res_path):
        parsed_res, rerr = read_tolerant_json_object(res_path, root=trial_dir)
        if rerr is not None or parsed_res is None:
            result_state = "unreadable"
            result_err = rerr or "unreadable result.json"
        else:
            result_state = "ready"
            res_map = parsed_res
    if ecosystem == "bespoke":
        seats, facing_map = _build_bespoke_seats(trial_dir, res_map)
    else:
        seats = _build_olam_seats(trial_dir, res_map, cfg_map)
        facing_map = {s.seat: s.seat for s in seats}
    rewards, primary, valid = _extract_rewards(trial_dir, res_map) if result_state == "ready" else ({}, None, False)
    exc_raw = _as_dict(res_map.get("exception_info")).get("exception_type")
    ag_exec = _as_dict(res_map.get("agent_execution"))
    ag_start = parse_iso_utc(ag_exec.get("started_at"))
    not_started = str(exc_raw) if (result_state == "ready" and ag_start is None and isinstance(exc_raw, str) and exc_raw) else None
    res_cfg = _as_dict(res_map.get("config")) or cfg_map
    task_nm = str(res_map.get("task_name") or _as_dict(res_cfg.get("task")).get("name") or _as_dict(res_cfg.get("task")).get("path") or trial_dir.name)
    trial_nm = str(res_map.get("trial_name") or cfg_map.get("trial_name") or trial_dir.name)
    cost = _extract_cost_usd(trial_dir, ecosystem, res_map, seats, proxy_rows)
    return TrialBundle(
        path=trial_dir,
        trial_name=trial_nm,
        task_name=task_nm,
        ecosystem=ecosystem,
        result_state=result_state,
        result_error=result_err,
        reward=primary,
        rewards=rewards,
        reward_valid=valid,
        exception=str(exc_raw) if isinstance(exc_raw, str) else None,
        not_started=not_started,
        started_at=parse_iso_utc(res_map.get("started_at")),
        finished_at=parse_iso_utc(res_map.get("finished_at")),
        agent_started_at=ag_start,
        agent_finished_at=parse_iso_utc(ag_exec.get("finished_at")),
        cost_usd=cost,
        seats=seats,
        facing_to_seat=facing_map,
    )
