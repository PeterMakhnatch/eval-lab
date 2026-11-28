from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ._decompose_models import (
    ArmDecompositionSummary,
    BestOfKSummary,
    CoordinationDecompositionReport,
    CountingMode,
    DecompositionReport,
    EffectEstimate,
    PaceResourceVector,
    build_effect_estimate,
    compute_best_of_k_solo,
    compute_shares_and_recovery,
    format_decompose_json,
    parse_decompose_options,
    render_decomposition_summary,
)
from ._decompose_stats import _safe_fmean, compute_arm_stats
from ._multiagent_io import safe_finite_float, safe_fsum
from .multiagent_adapter import (
    NormalizedMultiAgentTrial,
    discover_trial_dirs,
    parse_timestamp_epoch,
    read_multiagent_trial,
)

__all__ = [
    "ArmDecompositionSummary",
    "BestOfKSummary",
    "CoordinationDecompositionReport",
    "CountingMode",
    "DecompositionReport",
    "EffectEstimate",
    "PaceResourceVector",
    "decompose_coordination",
    "format_decompose_json",
    "render_decompose_text",
    "render_decomposition_summary",
]


@dataclass
class _DecomposeContext:
    shared: list[str]
    shared_outcomes: dict[str, dict[str, list[float]]]
    shared_trials: dict[str, dict[str, list[NormalizedMultiAgentTrial]]]
    arms_out: dict[str, ArmDecompositionSummary]
    effects_out: dict[str, EffectEstimate]
    dropped: dict[str, list[str]]
    unmatched: dict[str, int]
    unscored: dict[str, int]


def _extract_task_key(
    trial: NormalizedMultiAgentTrial, pattern: re.Pattern[str] | None
) -> str | None:
    raw = trial.task_name or trial.trial_name
    if pattern is None:
        return raw
    for candidate in (trial.task_name, trial.trial_name):
        if not candidate:
            continue
        match = pattern.search(candidate)
        if match is not None:
            first_group = next((g for g in match.groups() if g is not None), None)
            return first_group if first_group is not None else match.group(0)
    return None


def _trial_wall_time_sec(trial: NormalizedMultiAgentTrial) -> float | None:
    for start_s, end_s in (
        (trial.agent_execution_started_at, trial.agent_execution_finished_at),
        (trial.started_at, trial.finished_at),
    ):
        t0, t1 = parse_timestamp_epoch(start_s), parse_timestamp_epoch(end_s)
        if t0 is not None and t1 is not None and t1 >= t0:
            return t1 - t0
    return None


def _trial_agent_hours(trial: NormalizedMultiAgentTrial) -> float | None:
    member_secs: list[float] = []
    for member in trial.members:
        t0 = parse_timestamp_epoch(member.started_at)
        t1 = parse_timestamp_epoch(member.finished_at)
        if t0 is not None and t1 is not None and t1 >= t0:
            member_secs.append(t1 - t0)
    if len(member_secs) == len(trial.members) and member_secs:
        return math.fsum(member_secs) / 3600.0
    wall = _trial_wall_time_sec(trial)
    if wall is not None and trial.members:
        return (wall * len(trial.members)) / 3600.0
    return None


def _trial_tokens(
    trial: NormalizedMultiAgentTrial,
) -> tuple[int | None, int | None, int | None, int | None]:
    n_in, n_ca, n_ou = (
        trial.total_input_tokens,
        trial.total_cache_tokens,
        trial.total_output_tokens,
    )
    parts = [v for v in (n_in, n_ca, n_ou) if v is not None]
    return n_in, n_ca, n_ou, (sum(parts) if parts else None)


def _trial_cost_for_pace(trial: NormalizedMultiAgentTrial) -> float | None:
    return trial.total_cost_usd if trial.total_cost_usd is not None else trial.known_cost_usd


def _safe_div(num: float | None, denom: float | None, *, digits: int = 6) -> float | None:
    if num is None or denom is None or not math.isfinite(num) or not math.isfinite(denom):
        return None
    if denom <= 0.0:
        return None
    val = num / denom
    return round(val, digits) if math.isfinite(val) else None


def _task_cost_means(
    trials_by_task: dict[str, list[NormalizedMultiAgentTrial]],
) -> dict[str, float]:
    out: dict[str, float] = {}
    for task in sorted(trials_by_task):
        cs = [c for tr in trials_by_task[task] if (c := _trial_cost_for_pace(tr)) is not None]
        if cs:
            out[task] = _safe_fmean(cs)
    return out


def _compute_pace(
    trials_by_task: dict[str, list[NormalizedMultiAgentTrial]],
    score_macro: float,
) -> PaceResourceVector:
    all_trials = [t for ts in trials_by_task.values() for t in ts]
    in_v, ca_v, ou_v, tot_v = [], [], [], []
    cost_v, wall_v, ah_v = [], [], []
    cost_complete_count = 0
    for tr in all_trials:
        n_in, n_ca, n_ou, n_tot = _trial_tokens(tr)
        if (v := safe_finite_float(n_in)) is not None:
            in_v.append(v)
        if (v := safe_finite_float(n_ca)) is not None:
            ca_v.append(v)
        if (v := safe_finite_float(n_ou)) is not None:
            ou_v.append(v)
        if (v := safe_finite_float(n_tot)) is not None:
            tot_v.append(v)
        cost_complete_count += int(tr.cost_complete)
        if (cost := _trial_cost_for_pace(tr)) is not None:
            cost_v.append(cost)
        if (wall := _trial_wall_time_sec(tr)) is not None:
            wall_v.append(wall)
        if (ah := _trial_agent_hours(tr)) is not None:
            ah_v.append(ah)
    mean_cost = _safe_fmean(cost_v) if cost_v else None
    task_costs = _task_cost_means(trials_by_task)
    macro_cost = _safe_fmean(list(task_costs.values())) if task_costs else None
    mean_wall = _safe_fmean(wall_v) if wall_v else None
    mean_ah = _safe_fmean(ah_v) if ah_v else None
    total_cost = safe_fsum(cost_v) if cost_v else None
    par_factor = (
        _safe_div(mean_ah * 3600.0, mean_wall)
        if mean_ah is not None and mean_ah > 0.0 and mean_wall is not None and mean_wall > 0.0
        else None
    )
    return PaceResourceVector(
        mean_input_tokens=round(_safe_fmean(in_v), 3) if in_v else None,
        mean_cache_tokens=round(_safe_fmean(ca_v), 3) if ca_v else None,
        mean_output_tokens=round(_safe_fmean(ou_v), 3) if ou_v else None,
        mean_total_tokens=round(_safe_fmean(tot_v), 3) if tot_v else None,
        trials_with_tokens=len(tot_v),
        mean_cost_usd=round(mean_cost, 9) if mean_cost is not None else None,
        mean_cost_usd_macro=round(macro_cost, 9) if macro_cost is not None else None,
        total_cost_usd=round(total_cost, 9) if total_cost is not None else None,
        trials_with_cost=len(cost_v),
        cost_complete_trials=cost_complete_count,
        mean_wall_time_sec=round(mean_wall, 6) if mean_wall is not None else None,
        trials_with_wall_time=len(wall_v),
        mean_agent_hours=round(mean_ah, 9) if mean_ah is not None else None,
        trials_with_agent_hours=len(ah_v),
        parallelism_factor=par_factor,
        score_per_usd=_safe_div(score_macro, mean_cost),
        score_per_agent_hour=_safe_div(score_macro, mean_ah),
    )


def _attach_solo_pace_ratios(ctx: _DecomposeContext) -> None:
    solo_pace = ctx.arms_out["solo"].pace
    solo_tc = _task_cost_means(ctx.shared_trials["solo"])
    for role, arm in ctx.arms_out.items():
        arm.pace.cost_ratio_vs_solo = _safe_div(arm.pace.mean_cost_usd, solo_pace.mean_cost_usd)
        arm_tc = _task_cost_means(ctx.shared_trials[role])
        both_c = sorted(t for t in solo_tc if t in arm_tc)
        if both_c:
            base_c = _safe_fmean([solo_tc[t] for t in both_c])
            cand_c = _safe_fmean([arm_tc[t] for t in both_c])
            arm.pace.cost_ratio_vs_solo_paired = _safe_div(cand_c, base_c)
        arm.pace.tokens_ratio_vs_solo = _safe_div(
            arm.pace.mean_total_tokens, solo_pace.mean_total_tokens
        )
        arm.pace.wall_time_ratio_vs_solo = _safe_div(
            arm.pace.mean_wall_time_sec, solo_pace.mean_wall_time_sec
        )
        arm.pace.speedup_vs_solo = _safe_div(
            solo_pace.mean_wall_time_sec, arm.pace.mean_wall_time_sec
        )
        arm.pace.agent_hours_ratio_vs_solo = _safe_div(
            arm.pace.mean_agent_hours, solo_pace.mean_agent_hours
        )
        if role != "solo" and both_c:
            delta_score = _safe_fmean(
                [
                    _safe_fmean(ctx.shared_outcomes[role][t])
                    - _safe_fmean(ctx.shared_outcomes["solo"][t])
                    for t in both_c
                ]
            )
            delta_cost = _safe_fmean([arm_tc[t] - solo_tc[t] for t in both_c])
            if delta_cost > 1e-12:
                arm.pace.marginal_gain_per_usd_vs_solo = _safe_div(delta_score, delta_cost)


def _load_arm(
    arm_path: Path,
    *,
    metric: str,
    pattern: re.Pattern[str] | None,
    counting: CountingMode = "valid",
) -> tuple[dict[str, list[float]], dict[str, list[NormalizedMultiAgentTrial]], int, int]:
    trial_dirs, _, _ = discover_trial_dirs(arm_path)
    if not trial_dirs:
        raise ValueError(f"no Harbor trials found under {arm_path}")
    outcomes: dict[str, list[float]] = defaultdict(list)
    trials_by_task: dict[str, list[NormalizedMultiAgentTrial]] = defaultdict(list)
    unmatched = unscored = 0
    for td in trial_dirs:
        trial = read_multiagent_trial(td)
        key = _extract_task_key(trial, pattern)
        if key is None:
            unmatched += 1
            continue
        raw_val = (trial.rewards or {}).get(metric)
        invalid = (
            trial.reward_valid is False
            or raw_val is None
            or isinstance(raw_val, bool)
            or not isinstance(raw_val, (int, float))
            or not math.isfinite(raw_val)
            or raw_val == -1.0
        )
        if invalid:
            if counting == "all" and metric == "reward":
                val = 0.0
            else:
                unscored += 1
                continue
        else:
            val = float(raw_val)
        outcomes[key].append(val)
        trials_by_task[key].append(trial)
    return dict(outcomes), dict(trials_by_task), unmatched, unscored


def _build_arm_summary(
    role: str,
    arm_path: Path,
    raw_task_count: int,
    sh_out: dict[str, list[float]],
    sh_tr: dict[str, list[NormalizedMultiAgentTrial]],
    *,
    level: float,
) -> ArmDecompositionSummary:
    st = compute_arm_stats(sh_out, level=level)
    ecos: dict[str, int] = defaultdict(int)
    seat_counts: list[int] = []
    for ts in sh_tr.values():
        for tr in ts:
            ecos[tr.ecosystem] += 1
            seat_counts.append(len(tr.members))
    return ArmDecompositionSummary(
        role=role,
        path=arm_path.as_posix(),
        ecosystems=dict(sorted(ecos.items())),
        seats_min=min(seat_counts) if seat_counts else 0,
        seats_max=max(seat_counts) if seat_counts else 0,
        n_tasks_total=raw_task_count,
        n_tasks_shared=st.n_tasks,
        n_trials_shared=st.n_trials,
        score_macro=st.mean,
        score_micro=st.micro,
        ci_rerun=[st.ci_rerun[0], st.ci_rerun[1]] if st.ci_rerun else None,
        ci_tasks=[st.ci_tasks[0], st.ci_tasks[1]] if st.ci_tasks else None,
        pace=_compute_pace(sh_tr, st.mean),
    )


def _load_primary_context(
    arm_paths: dict[str, Path],
    *,
    metric: str,
    pattern: re.Pattern[str] | None,
    counting: CountingMode,
    level: float,
    alpha: float,
) -> _DecomposeContext:
    raw_outcomes: dict[str, dict[str, list[float]]] = {}
    raw_trials: dict[str, dict[str, list[NormalizedMultiAgentTrial]]] = {}
    unmatched: dict[str, int] = {}
    unscored: dict[str, int] = {}
    for role, path in arm_paths.items():
        out_m, tr_m, unm, unsc = _load_arm(path, metric=metric, pattern=pattern, counting=counting)
        raw_outcomes[role], raw_trials[role] = out_m, tr_m
        unmatched[role], unscored[role] = unm, unsc
    shared = sorted(
        set(raw_outcomes["solo"]) & set(raw_outcomes["silent"]) & set(raw_outcomes["team"])
    )
    if not shared:
        raise ValueError(
            "the 3 arms share no scored tasks; check task names or pass --task-key REGEX"
        )
    dropped = {
        role: sorted(set(raw_outcomes[role]) - set(shared))
        for role in ("solo", "silent", "team")
        if set(raw_outcomes[role]) - set(shared)
    }
    arms_out: dict[str, ArmDecompositionSummary] = {}
    sh_outcomes: dict[str, dict[str, list[float]]] = {}
    sh_trials: dict[str, dict[str, list[NormalizedMultiAgentTrial]]] = {}
    for role in ("solo", "silent", "team"):
        sh_out = {t: raw_outcomes[role][t] for t in shared}
        sh_tr = {t: raw_trials[role][t] for t in shared}
        sh_outcomes[role], sh_trials[role] = sh_out, sh_tr
        arms_out[role] = _build_arm_summary(
            role, arm_paths[role], len(raw_outcomes[role]), sh_out, sh_tr, level=level
        )
    specs = (
        ("headcount", "solo", "silent"),
        ("coordination", "silent", "team"),
        ("total", "solo", "team"),
    )
    effects_out = {
        name: build_effect_estimate(
            name, b, c, sh_outcomes[b], sh_outcomes[c], level=level, alpha=alpha
        )
        for name, b, c in specs
    }
    return _DecomposeContext(
        shared=shared,
        shared_outcomes=sh_outcomes,
        shared_trials=sh_trials,
        arms_out=arms_out,
        effects_out=effects_out,
        dropped=dropped,
        unmatched=unmatched,
        unscored=unscored,
    )


def _attach_equal_budget_ladder(
    eb_dir: Path,
    ctx: _DecomposeContext,
    *,
    metric: str,
    pattern: re.Pattern[str] | None,
    counting: CountingMode,
    level: float,
    alpha: float,
) -> tuple[float | None, float | None]:
    eb_path = eb_dir.resolve()
    eb_out, eb_tr, eb_unm, eb_unsc = _load_arm(
        eb_path, metric=metric, pattern=pattern, counting=counting
    )
    ctx.unmatched["equal_budget_solo"] = eb_unm
    ctx.unscored["equal_budget_solo"] = eb_unsc
    eb_shared = sorted(set(ctx.shared) & set(eb_out))
    if not eb_shared:
        raise ValueError("equal_budget_solo shares no scored tasks with the primary 3 arms")
    if set(eb_out) - set(eb_shared):
        ctx.dropped["equal_budget_solo"] = sorted(set(eb_out) - set(eb_shared))
    sh_eb_out = {t: eb_out[t] for t in eb_shared}
    sh_eb_tr = {t: eb_tr[t] for t in eb_shared}
    ctx.shared_outcomes["equal_budget_solo"] = sh_eb_out
    ctx.shared_trials["equal_budget_solo"] = sh_eb_tr
    ctx.arms_out["equal_budget_solo"] = _build_arm_summary(
        "equal_budget_solo", eb_path, len(eb_out), sh_eb_out, sh_eb_tr, level=level
    )
    sub = {
        "solo": {t: ctx.shared_outcomes["solo"][t] for t in eb_shared},
        "silent": {t: ctx.shared_outcomes["silent"][t] for t in eb_shared},
        "team": {t: ctx.shared_outcomes["team"][t] for t in eb_shared},
        "equal_budget_solo": sh_eb_out,
    }
    eb_specs = (
        ("budget_scaling", "solo", "equal_budget_solo"),
        ("fragmentation_gap", "equal_budget_solo", "team"),
        ("silent_fragmentation_gap", "equal_budget_solo", "silent"),
    )
    for name, b_r, c_r in eb_specs:
        ctx.effects_out[name] = build_effect_estimate(
            name, b_r, c_r, sub[b_r], sub[c_r], level=level, alpha=alpha
        )
    bs, fg, sfg = (
        ctx.effects_out["budget_scaling"],
        ctx.effects_out["fragmentation_gap"],
        ctx.effects_out["silent_fragmentation_gap"],
    )
    tot = ctx.effects_out["total"]
    same_tasks = eb_shared == ctx.shared
    if same_tasks:
        scale = max(1.0, abs(tot.diff), abs(bs.diff), abs(fg.diff))
        assert abs((bs.diff + fg.diff) - tot.diff) < 1e-11 * scale
    team_minus_solo = tot.diff if same_tasks else (bs.diff + fg.diff)
    team_minus_silent = ctx.effects_out["coordination"].diff if same_tasks else (fg.diff - sfg.diff)
    eb_minus_silent = -sfg.diff
    sb_rec = bs.diff / team_minus_solo if abs(team_minus_solo) > 1e-9 else None
    fr_rec = team_minus_silent / eb_minus_silent if eb_minus_silent > 1e-9 else None
    return (
        sb_rec if sb_rec is not None and math.isfinite(sb_rec) else None,
        fr_rec if fr_rec is not None and math.isfinite(fr_rec) else None,
    )


def _finalize_report(
    ctx: _DecomposeContext,
    *,
    metric: str,
    counting: CountingMode,
    eff_regex: str | None,
    level: float,
    alpha: float,
    best_of_k: int | None,
    solo_bud_rec: float | None,
    frag_rec: float | None,
) -> DecompositionReport:
    bok = compute_best_of_k_solo(
        ctx.shared,
        ctx.shared_outcomes["solo"],
        ctx.shared_outcomes["team"],
        ctx.shared_trials["team"],
        ctx.arms_out["solo"],
        ctx.arms_out["team"],
        best_of_k,
    )
    notes = [
        "headcount = silent (S_N^0) - solo (S_1); coordination = team (S_N) - silent (S_N^0); total = headcount + coordination",
        "tasks verdict requires both paired t-test and exact sign-flip test to give p < alpha",
    ]
    coord_share, headcount_share, split_cost, rec_ratio = compute_shares_and_recovery(
        ctx.effects_out, notes
    )
    if any(ctx.unmatched.values()):
        notes.append(f"unmatched_task_key_trials={ctx.unmatched}")
    if any(ctx.unscored.values()):
        notes.append(f"unscored_trials_excluded={ctx.unscored}")
    return DecompositionReport(
        metric=metric,
        counting=counting,
        task_key_regex=eff_regex,
        level=level,
        alpha=alpha,
        shared_tasks=ctx.shared,
        dropped_tasks=ctx.dropped,
        unmatched_trials=ctx.unmatched,
        unscored_trials=ctx.unscored,
        arms=ctx.arms_out,
        effects=ctx.effects_out,
        best_of_k_solo=bok,
        coordination_share=coord_share,
        headcount_share=headcount_share,
        split_or_conflict_cost=split_cost,
        recovery_ratio=rec_ratio,
        solo_budget_recovery_ratio=solo_bud_rec,
        fragmentation_recovery_ratio=frag_rec,
        notes=notes,
    )


def decompose_coordination(
    solo_dir: str | Path,
    silent_dir: str | Path,
    team_dir: str | Path,
    *,
    equal_budget_solo_dir: str | Path | None = None,
    best_of_k: int | None = None,
    counting: CountingMode = "valid",
    task_key_regex: str | None = None,
    task_key_pattern: str | None = None,
    metric: str = "reward",
    level: float = 0.95,
    alpha: float = 0.05,
) -> DecompositionReport:
    eff_regex, pattern = parse_decompose_options(
        counting, best_of_k, task_key_regex, task_key_pattern
    )
    arm_paths = {
        "solo": Path(solo_dir).resolve(),
        "silent": Path(silent_dir).resolve(),
        "team": Path(team_dir).resolve(),
    }
    ctx = _load_primary_context(
        arm_paths, metric=metric, pattern=pattern, counting=counting, level=level, alpha=alpha
    )
    solo_bud_rec = frag_rec = None
    if equal_budget_solo_dir is not None:
        solo_bud_rec, frag_rec = _attach_equal_budget_ladder(
            Path(equal_budget_solo_dir),
            ctx,
            metric=metric,
            pattern=pattern,
            counting=counting,
            level=level,
            alpha=alpha,
        )
    _attach_solo_pace_ratios(ctx)
    return _finalize_report(
        ctx,
        metric=metric,
        counting=counting,
        eff_regex=eff_regex,
        level=level,
        alpha=alpha,
        best_of_k=best_of_k,
        solo_bud_rec=solo_bud_rec,
        frag_rec=frag_rec,
    )


def render_decompose_text(report: DecompositionReport) -> str:
    return render_decomposition_summary(report)
