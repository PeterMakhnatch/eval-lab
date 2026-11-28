from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ._bespoke_public_io import (
    as_finite_float,
    build_public_world_log,
    load_public_sources,
    parse_public_agent_steps,
    resolve_attachment,
)
from ._multiagent_io import (
    GradeMarkRecord,
    MemberRecord,
    MemberUsage,
    NormalizedAtifStep,
    as_dict,
    as_nonneg_float,
    as_nonneg_int,
    as_str,
    safe_fsum,
    sanitize_utf8_text,
)
from .multiagent_adapter import NormalizedMultiAgentTrial
from .seat_forensics import TrialForensicsReport, analyze_normalized_trial
from .world_events import WorldEventLog

__all__ = [
    "BespokeArmSummary",
    "BespokeModelDecomposition",
    "BespokePublicPackage",
    "BespokePublicReport",
    "BespokePublicRun",
    "analyze_bespoke_public_package",
    "load_bespoke_public_package",
    "parse_public_agent_steps",
    "render_bespoke_public_summary",
]

_MAX_RUNS = 10_000
_RunCtx = tuple[str, str, str | None, Path | None, dict[str, dict[str, Any]]]
_AgentsOut = tuple[
    list[MemberRecord],
    dict[str, str],
    dict[str, list[NormalizedAtifStep]],
    list[str],
    tuple[int, int, dict[str, Any] | None],
    tuple[int | None, int | None, int],
]


class BespokePublicRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_slug: str
    task_title: str | None = None
    model_slug: str
    model_name: str
    harness: str | None = None
    team_size: int
    setting: str
    setting_label: str | None = None
    rollout: int
    score: float | None = None
    marks: list[float | None] = Field(default_factory=list)
    cost_usd: float | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_steps: int = 0
    board_steps: int = 0
    board_senders: int = 0
    has_board_log: bool = False
    loaded_agent_ids: list[str] = Field(default_factory=list)
    trial: NormalizedMultiAgentTrial
    world_log: WorldEventLog


class BespokePublicPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "bespoke_public_package.v1"
    source_path: str
    mark_hours: list[float] = Field(default_factory=list)
    n_runs: int = 0
    tasks: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    runs: list[BespokePublicRun] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)


class BespokeArmSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arm: str
    team_size: int
    setting: str
    n_runs: int
    mean_score: float | None = None
    scores: list[float] = Field(default_factory=list)
    mean_marks: list[float | None] = Field(default_factory=list)
    mean_cost_usd: float | None = None
    mean_input_tokens: float | None = None
    mean_output_tokens: float | None = None
    mean_total_tokens: float | None = None
    mean_steps: float | None = None
    mean_board_steps: float | None = None
    agent_hours: float | None = None
    score_per_usd: float | None = None


class BespokeModelDecomposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_slug: str
    model_slug: str
    model_name: str
    harness: str | None = None
    mark_hours: list[float] = Field(default_factory=list)
    arms: list[BespokeArmSummary] = Field(default_factory=list)
    solo_mean: float | None = None
    control_mean: float | None = None
    team_mean: float | None = None
    headcount_gain: float | None = None
    coordination_gain: float | None = None
    total_gain: float | None = None
    coordination_share: float | None = None
    cost_ratio_team_vs_solo: float | None = None
    cost_ratio_team_vs_control: float | None = None
    token_ratio_team_vs_solo: float | None = None
    token_ratio_team_vs_control: float | None = None


class BespokePublicReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "bespoke_public_report.v1"
    source_path: str
    mark_hours: list[float] = Field(default_factory=list)
    n_runs: int = 0
    n_with_board_log: int = 0
    n_with_agent_trajectories: int = 0
    decompositions: list[BespokeModelDecomposition] = Field(default_factory=list)
    forensics_reports: list[TrialForensicsReport] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)


def _build_grade_marks(marks: list[float | None], mark_hours: list[float]) -> list[GradeMarkRecord]:
    out: list[GradeMarkRecord] = []
    for idx, m_val in enumerate(marks):
        hrs = mark_hours[idx] if idx < len(mark_hours) else float(idx + 1)
        mark_s = hrs * 3600.0
        out.append(
            GradeMarkRecord(
                mark=f"{hrs:g}h",
                mark_s=round(mark_s, 3) if math.isfinite(mark_s) else None,
                reward=m_val,
                status="ok" if m_val is not None else "missing",
                final=(idx == len(marks) - 1),
            )
        )
    return out


def _parse_agent_member(
    entry: dict[str, Any], mid: str, word_name: str | None, ctx: _RunCtx
) -> tuple[MemberRecord, list[NormalizedAtifStep] | None, int, int | None, int | None]:
    run_id, model_name, harness, base_dir, attachments = ctx
    traj_path = as_str(entry.get("trajectoryPath"))
    n_steps_meta = as_nonneg_int(entry.get("steps")) or 0
    in_tok = as_nonneg_int(entry.get("inputTokens"))
    out_tok = as_nonneg_int(entry.get("outputTokens"))
    m_cost = as_nonneg_float(entry.get("costUsd"))
    ag_payload = resolve_attachment(run_id, traj_path, base_dir, attachments)
    steps = parse_public_agent_steps(mid, ag_payload) if ag_payload is not None else None
    agent_meta = as_dict(ag_payload.get("agent")) if ag_payload is not None else {}
    res_harness = as_str(agent_meta.get("harness")) or harness
    rec = MemberRecord(
        member_id=mid,
        display_name=word_name,
        agent_name=res_harness,
        harness=res_harness,
        model_name=as_str(agent_meta.get("model")) or model_name,
        ran_model=True,
        trajectory_path=traj_path,
        trajectory_status="present" if ag_payload is not None else "missing",
        n_steps=len(steps) if steps is not None else n_steps_meta,
        n_tool_calls=sum(len(s.tool_calls) for s in steps) if steps is not None else None,
        usage=MemberUsage(
            n_input_tokens=in_tok,
            n_output_tokens=out_tok,
            cost_usd=m_cost,
            cost_status="recorded" if m_cost is not None else "missing",
            cost_source="bespoke_public_index" if m_cost is not None else None,
        ),
    )
    return rec, steps, n_steps_meta, in_tok, out_tok


def _collect_run_agents(run_raw: dict[str, Any], ctx: _RunCtx) -> _AgentsOut:
    run_id, _, _, base_dir, attachments = ctx
    members: list[MemberRecord] = []
    seen_ids: set[str] = set()
    aliases: dict[str, str] = {}
    colliding: set[str] = set()
    member_steps: dict[str, list[NormalizedAtifStep]] = {}
    loaded_ids: list[str] = []
    b_steps, b_senders, b_payload = 0, 0, None
    total_in, total_out, total_steps = 0, 0, 0
    any_in, any_out = False, False
    raw_agents = run_raw.get("agents")
    for entry in raw_agents[:500] if isinstance(raw_agents, list) else ():
        if not isinstance(entry, dict):
            continue
        if as_str(entry.get("kind")) == "board":
            b_steps = as_nonneg_int(entry.get("steps")) or 0
            b_senders = as_nonneg_int(entry.get("senders")) or 0
            b_payload = resolve_attachment(
                run_id, as_str(entry.get("trajectoryPath")), base_dir, attachments
            )
            continue
        base_mid = as_str(entry.get("id")) or f"agent-{len(members) + 1}"
        mid, dup_i = base_mid, 2
        while mid in seen_ids:
            mid, dup_i = f"{base_mid}-{dup_i}", dup_i + 1
        seen_ids.add(mid)
        word_name = as_str(entry.get("name"))
        if word_name:
            if word_name in aliases and aliases[word_name] != mid:
                colliding.add(word_name)
                aliases.pop(word_name, None)
            elif word_name not in colliding:
                aliases[word_name] = mid
        rec, steps, n_meta, in_tok, out_tok = _parse_agent_member(entry, mid, word_name, ctx)
        total_steps += n_meta
        if in_tok is not None:
            total_in, any_in = total_in + in_tok, True
        if out_tok is not None:
            total_out, any_out = total_out + out_tok, True
        if steps is not None:
            member_steps[mid] = steps
            loaded_ids.append(mid)
        members.append(rec)
    for cid in seen_ids:
        if aliases.get(cid) != cid:
            aliases.pop(cid, None)
    toks = (total_in if any_in else None, total_out if any_out else None, total_steps)
    return members, aliases, member_steps, loaded_ids, (b_steps, b_senders, b_payload), toks


def _resolve_run_costs(
    run_cost: float | None, members: list[MemberRecord]
) -> tuple[float | None, float | None, float | None, bool, str | None]:
    member_costs = [m.usage.cost_usd for m in members if m.usage.cost_usd is not None]
    member_cost_sum = safe_fsum(member_costs) if member_costs else None
    all_ok = bool(members) and len(member_costs) == len(members) and member_cost_sum is not None
    if run_cost is not None:
        known = round(run_cost, 6)
        return known, known, run_cost, True, "bespoke_public_index"
    if member_cost_sum is not None:
        known = round(member_cost_sum, 6)
        src = "bespoke_public_index" if all_ok else None
        return (
            known,
            (known if all_ok else None),
            (member_cost_sum if all_ok else None),
            all_ok,
            src,
        )
    return None, None, None, False, None


def _make_public_trial(
    run_id: str,
    task_slug: str,
    setting: str,
    score: float | None,
    marks: list[float | None],
    mark_hours: list[float],
    run_cost: float | None,
    parsed: _AgentsOut,
) -> tuple[NormalizedMultiAgentTrial, WorldEventLog, float | None]:
    members, aliases, member_steps, _, (_, _, b_payload), (tot_in, tot_out, _) = parsed
    comms_disabled = setting == "control"
    world_log = build_public_world_log(
        b_payload, member_steps, aliases, comms_disabled=comms_disabled
    )
    known_c, total_c, eff_c, c_complete, c_source = _resolve_run_costs(run_cost, members)
    trial = NormalizedMultiAgentTrial(
        trial_dir=f"bespoke-public://{task_slug}/{run_id}",
        trial_name=run_id,
        trial_id=run_id,
        task_name=task_slug,
        ecosystem="bespoke_team",
        orchestration="bespoke_public_rollout",
        rewards={"score": score} if score is not None else {},
        reward_valid=score is not None,
        reward_source="bespoke_public_index",
        members=members,
        member_steps=member_steps,
        total_input_tokens=tot_in,
        total_output_tokens=tot_out,
        total_cost_usd=total_c,
        known_cost_usd=known_c,
        cost_complete=c_complete,
        cost_source=c_source,
        member_aliases=aliases,
        grade_marks=_build_grade_marks(marks, mark_hours),
        board_policy="NoMessagesPolicy" if comms_disabled else "default",
        comms_disabled=comms_disabled,
    )
    return trial, world_log, eff_c


def _build_public_run(
    run_raw: dict[str, Any],
    *,
    task_slug: str,
    task_title: str | None,
    model_slug: str,
    model_name: str,
    harness: str | None,
    mark_hours: list[float],
    base_dir: Path | None,
    attachments: dict[str, dict[str, Any]],
) -> BespokePublicRun:
    run_id = as_str(run_raw.get("runId")) or f"{model_slug}-unknown"
    team_size = as_nonneg_int(run_raw.get("teamSize")) or 1
    setting = as_str(run_raw.get("setting")) or "team"
    raw_rollout = as_nonneg_int(run_raw.get("rollout"))
    rollout = raw_rollout if raw_rollout is not None else 1
    metrics = as_dict(run_raw.get("metrics"))
    score = as_finite_float(metrics.get("score"))
    raw_marks = metrics.get("marks")
    marks = [as_finite_float(v) for v in raw_marks[:500]] if isinstance(raw_marks, list) else []
    parsed = _collect_run_agents(run_raw, (run_id, model_name, harness, base_dir, attachments))
    _, _, _, loaded_ids, (b_steps, b_senders, b_payload), (tot_in, tot_out, tot_steps) = parsed
    trial, world_log, eff_cost = _make_public_trial(
        run_id,
        task_slug,
        setting,
        score,
        marks,
        mark_hours,
        as_nonneg_float(metrics.get("costUsd")),
        parsed,
    )
    return BespokePublicRun(
        run_id=run_id,
        task_slug=task_slug,
        task_title=task_title,
        model_slug=model_slug,
        model_name=model_name,
        harness=harness,
        team_size=team_size,
        setting=setting,
        setting_label=as_str(run_raw.get("settingLabel")),
        rollout=rollout,
        score=score,
        marks=marks,
        cost_usd=eff_cost,
        total_input_tokens=tot_in,
        total_output_tokens=tot_out,
        total_steps=tot_steps,
        board_steps=b_steps,
        board_senders=b_senders,
        has_board_log=b_payload is not None,
        loaded_agent_ids=loaded_ids,
        trial=trial,
        world_log=world_log,
    )


def load_bespoke_public_package(path: Path) -> BespokePublicPackage:
    index_obj, base_dir, attachments, errors = load_public_sources(path)
    if index_obj is None:
        raise ValueError(f"Could not load Bespoke public index.json from {path}: {errors}")
    raw_hours = index_obj.get("markHours")
    mark_hours = [
        fv
        for v in (raw_hours[:500] if isinstance(raw_hours, list) else ())
        if (fv := as_nonneg_float(v)) is not None
    ]
    runs: list[BespokePublicRun] = []
    task_slugs: list[str] = []
    model_slugs: set[str] = set()
    for raw_tslug, tval in sorted(as_dict(index_obj.get("tasks")).items()):
        tslug = sanitize_utf8_text(str(raw_tslug))
        tdata = as_dict(tval)
        task_slugs.append(tslug)
        title = as_str(tdata.get("title"))
        for raw_mslug, mval in sorted(as_dict(tdata.get("models")).items()):
            mslug = sanitize_utf8_text(str(raw_mslug))
            mdata = as_dict(mval)
            model_slugs.add(mslug)
            mname = as_str(mdata.get("modelName")) or mslug
            harness = as_str(mdata.get("harness"))
            raw_runs = mdata.get("runs")
            if not isinstance(raw_runs, list):
                continue
            for run_raw in raw_runs:
                if len(runs) >= _MAX_RUNS:
                    break
                if isinstance(run_raw, dict):
                    runs.append(
                        _build_public_run(
                            run_raw,
                            task_slug=tslug,
                            task_title=title,
                            model_slug=mslug,
                            model_name=mname,
                            harness=harness,
                            mark_hours=mark_hours,
                            base_dir=base_dir,
                            attachments=attachments,
                        )
                    )
    return BespokePublicPackage(
        source_path=str(path),
        mark_hours=mark_hours,
        n_runs=len(runs),
        tasks=task_slugs,
        models=sorted(model_slugs),
        runs=runs,
        parse_errors=errors,
    )


def _mean_opt(vals: Sequence[float | int | None]) -> float | None:
    clean = [fv for v in vals if (fv := as_finite_float(v)) is not None]
    if not clean:
        return None
    total = safe_fsum(clean)
    if total is not None:
        return round(total / len(clean), 6)
    scaled = safe_fsum([x / len(clean) for x in clean])
    return round(scaled, 6) if scaled is not None else None


def _ratio_opt(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or abs(den) < 1e-12:
        return None
    val = num / den
    return round(val, 6) if math.isfinite(val) else None


def _finite_op(val: float) -> float | None:
    return round(val, 6) if math.isfinite(val) else None


def _summarize_arm(
    k: int,
    setting: str,
    arm_runs: list[BespokePublicRun],
    mark_hours: list[float],
    max_hours: float,
) -> BespokeArmSummary:
    scores = [r.score for r in arm_runs if r.score is not None]
    mean_score = _mean_opt(scores)
    mean_cost = _mean_opt([r.cost_usd for r in arm_runs])
    mean_in = _mean_opt([r.total_input_tokens for r in arm_runs])
    mean_out = _mean_opt([r.total_output_tokens for r in arm_runs])
    mean_tok = (
        _finite_op(mean_in + mean_out) if (mean_in is not None and mean_out is not None) else None
    )
    mean_marks = [
        _mean_opt([r.marks[idx] if idx < len(r.marks) else None for r in arm_runs])
        for idx in range(len(mark_hours))
    ]
    return BespokeArmSummary(
        arm=f"k{k}_{setting}",
        team_size=k,
        setting=setting,
        n_runs=len(arm_runs),
        mean_score=mean_score,
        scores=[round(x, 6) for x in scores],
        mean_marks=mean_marks,
        mean_cost_usd=mean_cost,
        mean_input_tokens=mean_in,
        mean_output_tokens=mean_out,
        mean_total_tokens=mean_tok,
        mean_steps=_mean_opt([r.total_steps for r in arm_runs]),
        mean_board_steps=_mean_opt([r.board_steps for r in arm_runs]),
        agent_hours=(
            _finite_op(k_f * max_hours) if (k_f := as_finite_float(k)) is not None else None
        ),
        score_per_usd=_ratio_opt(mean_score, mean_cost),
    )


def _decompose_model_runs(
    task_slug: str, model_slug: str, runs: list[BespokePublicRun], mark_hours: list[float]
) -> BespokeModelDecomposition:
    assert runs
    max_hours = max(mark_hours) if mark_hours else 4.0
    grouped: dict[tuple[int, str], list[BespokePublicRun]] = {}
    for run in runs:
        grouped.setdefault((run.team_size, run.setting), []).append(run)
    arms: list[BespokeArmSummary] = []
    arm_by_key: dict[tuple[int, str], BespokeArmSummary] = {}
    for (k, setting), arm_runs in sorted(grouped.items()):
        summary = _summarize_arm(k, setting, arm_runs, mark_hours, max_hours)
        arms.append(summary)
        arm_by_key[(k, setting)] = summary

    solo = (
        arm_by_key.get((1, "team")) or arm_by_key.get((1, "solo")) or arm_by_key.get((1, "control"))
    )
    control_keys = [key for key in arm_by_key if key[1] == "control" and key[0] > 1]
    ctrl = arm_by_key.get(max(control_keys)) if control_keys else None
    max_k = max(k for k, _ in arm_by_key)
    team_k = ctrl.team_size if ctrl is not None else (max_k if max_k > 1 else None)
    team = arm_by_key.get((team_k, "team")) if team_k is not None else None
    s1, sc, sn = (
        solo.mean_score if solo else None,
        ctrl.mean_score if ctrl else None,
        team.mean_score if team else None,
    )
    hc = _finite_op(sc - s1) if (sc is not None and s1 is not None) else None
    co = _finite_op(sn - sc) if (sn is not None and sc is not None) else None
    tot = _finite_op(sn - s1) if (sn is not None and s1 is not None) else None
    t_cost, t_tok = (team.mean_cost_usd, team.mean_total_tokens) if team else (None, None)
    return BespokeModelDecomposition(
        task_slug=task_slug,
        model_slug=model_slug,
        model_name=runs[0].model_name,
        harness=runs[0].harness,
        mark_hours=mark_hours,
        arms=arms,
        solo_mean=s1,
        control_mean=sc,
        team_mean=sn,
        headcount_gain=hc,
        coordination_gain=co,
        total_gain=tot,
        coordination_share=_ratio_opt(co, tot),
        cost_ratio_team_vs_solo=_ratio_opt(t_cost, solo.mean_cost_usd if solo else None),
        cost_ratio_team_vs_control=_ratio_opt(t_cost, ctrl.mean_cost_usd if ctrl else None),
        token_ratio_team_vs_solo=_ratio_opt(t_tok, solo.mean_total_tokens if solo else None),
        token_ratio_team_vs_control=_ratio_opt(t_tok, ctrl.mean_total_tokens if ctrl else None),
    )


def analyze_loaded_bespoke_public_package(
    pkg: BespokePublicPackage,
    *,
    model_slug: str | None = None,
    run_id: str | None = None,
) -> BespokePublicReport:
    filtered = [
        r
        for r in pkg.runs
        if (model_slug is None or r.model_slug == model_slug)
        and (run_id is None or r.run_id == run_id)
    ]
    by_tm: dict[tuple[str, str], list[BespokePublicRun]] = {}
    for run in filtered:
        by_tm.setdefault((run.task_slug, run.model_slug), []).append(run)
    return BespokePublicReport(
        source_path=pkg.source_path,
        mark_hours=pkg.mark_hours,
        n_runs=len(filtered),
        n_with_board_log=sum(1 for r in filtered if r.has_board_log),
        n_with_agent_trajectories=sum(1 for r in filtered if r.loaded_agent_ids),
        decompositions=[
            _decompose_model_runs(tslug, mslug, mruns, pkg.mark_hours)
            for (tslug, mslug), mruns in sorted(by_tm.items())
        ],
        forensics_reports=[analyze_normalized_trial(r.trial, r.world_log) for r in filtered],
        parse_errors=pkg.parse_errors,
    )


def analyze_bespoke_public_package(
    path: Path, *, model_slug: str | None = None, run_id: str | None = None
) -> BespokePublicReport:
    pkg = load_bespoke_public_package(path)
    return analyze_loaded_bespoke_public_package(pkg, model_slug=model_slug, run_id=run_id)


def render_bespoke_public_summary(report: BespokePublicReport) -> str:
    lines = [
        f"Bespoke Public Rollout: {report.n_runs} runs "
        f"(board_logs={report.n_with_board_log}, agent_trajs={report.n_with_agent_trajectories})"
    ]
    for d in report.decompositions:
        lines.append(
            f"  {d.task_slug} / {d.model_name} ({d.harness or 'unknown'}): "
            f"S1={d.solo_mean}, S_ctrl={d.control_mean}, S_team={d.team_mean}, "
            f"headcount={d.headcount_gain}, coord={d.coordination_gain}, "
            f"total={d.total_gain}, cost_ratio={d.cost_ratio_team_vs_solo}x"
        )
    return "\n".join(lines)
