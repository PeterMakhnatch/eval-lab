from __future__ import annotations

import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ._decompose_stats import _safe_fmean, compare_paired_arms
from .multiagent_adapter import NormalizedMultiAgentTrial

CountingMode = Literal["valid", "all"]


class PaceResourceVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mean_input_tokens: float | None = None
    mean_cache_tokens: float | None = None
    mean_output_tokens: float | None = None
    mean_total_tokens: float | None = None
    trials_with_tokens: int = 0
    mean_cost_usd: float | None = None
    mean_cost_usd_macro: float | None = None
    total_cost_usd: float | None = None
    trials_with_cost: int = 0
    cost_complete_trials: int = 0
    mean_wall_time_sec: float | None = None
    trials_with_wall_time: int = 0
    mean_agent_hours: float | None = None
    trials_with_agent_hours: int = 0
    cost_ratio_vs_solo: float | None = None
    cost_ratio_vs_solo_paired: float | None = None
    tokens_ratio_vs_solo: float | None = None
    wall_time_ratio_vs_solo: float | None = None
    speedup_vs_solo: float | None = None
    agent_hours_ratio_vs_solo: float | None = None
    parallelism_factor: float | None = None
    score_per_usd: float | None = None
    score_per_agent_hour: float | None = None
    marginal_gain_per_usd_vs_solo: float | None = None


class BestOfKSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    k: int
    n_tasks_eligible: int
    n_tasks_unbiased: int
    unbiased_score_macro: float | None = None
    empirical_max_macro: float
    team_minus_best_of_k: float | None = None
    projected_cost_usd: float | None = None
    oracle_attempt_pool_mean: float | None = None
    mean_selection_gap: float | None = None


class ArmDecompositionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    path: str
    ecosystems: dict[str, int] = Field(default_factory=dict)
    seats_min: int
    seats_max: int
    n_tasks_total: int
    n_tasks_shared: int
    n_trials_shared: int
    score_macro: float
    score_micro: float
    ci_rerun: list[float] | None = None
    ci_tasks: list[float] | None = None
    pace: PaceResourceVector


class EffectEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: str
    baseline_role: str
    candidate_role: str
    n_tasks: int
    baseline_mean: float
    candidate_mean: float
    difference: float
    se_rerun: float | None = None
    df_rerun: float | None = None
    ci_rerun: list[float] | None = None
    p_rerun: float | None = None
    p_perm_rerun: float | None = None
    perm_rerun_exact: bool = False
    verdict_rerun: str
    se_tasks: float | None = None
    ci_tasks: list[float] | None = None
    p_paired_t: float | None = None
    p_signflip: float | None = None
    signflip_exact: bool = False
    p_mcnemar: float | None = None
    verdict_tasks: str
    candidate_better: int
    baseline_better: int
    ties: int
    task_deltas: dict[str, float] = Field(default_factory=dict)

    @property
    def diff(self) -> float:
        return self.difference


class DecompositionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.seat-decompose/v1"
    metric: str = "reward"
    counting: CountingMode = "valid"
    task_key_regex: str | None = None
    level: float = 0.95
    alpha: float = 0.05
    shared_tasks: list[str] = Field(default_factory=list)
    dropped_tasks: dict[str, list[str]] = Field(default_factory=dict)
    unmatched_trials: dict[str, int] = Field(default_factory=dict)
    unscored_trials: dict[str, int] = Field(default_factory=dict)
    arms: dict[str, ArmDecompositionSummary] = Field(default_factory=dict)
    effects: dict[str, EffectEstimate] = Field(default_factory=dict)
    best_of_k_solo: BestOfKSummary | None = None
    coordination_share: float | None = None
    headcount_share: float | None = None
    split_or_conflict_cost: float | None = None
    recovery_ratio: float | None = None
    solo_budget_recovery_ratio: float | None = None
    fragmentation_recovery_ratio: float | None = None
    notes: list[str] = Field(default_factory=list)


CoordinationDecompositionReport = DecompositionReport


def parse_decompose_options(
    counting: CountingMode,
    best_of_k: int | None,
    task_key_regex: str | None,
    task_key_pattern: str | None,
) -> tuple[str | None, re.Pattern[str] | None]:
    if counting not in ("valid", "all"):
        raise ValueError(f"counting must be 'valid' or 'all', got {counting!r}")
    if best_of_k is not None and (type(best_of_k) is not int or best_of_k < 1):
        raise ValueError(f"best_of_k must be a positive int, got {best_of_k!r}")
    if (
        task_key_regex is not None
        and task_key_pattern is not None
        and task_key_regex != task_key_pattern
    ):
        raise ValueError("task_key_regex and task_key_pattern must match when both are provided")
    eff_regex = task_key_regex if task_key_regex is not None else task_key_pattern
    try:
        pattern = re.compile(eff_regex) if eff_regex else None
    except re.error as exc:
        raise ValueError(f"invalid task_key_regex {eff_regex!r}: {exc}") from exc
    return eff_regex, pattern


def build_effect_estimate(
    eff_name: str,
    base_role: str,
    cand_role: str,
    base_outcomes: dict[str, list[float]],
    cand_outcomes: dict[str, list[float]],
    *,
    level: float,
    alpha: float,
) -> EffectEstimate:
    cmp = compare_paired_arms(base_outcomes, cand_outcomes, level=level, alpha=alpha)
    return EffectEstimate(
        effect=eff_name,
        baseline_role=base_role,
        candidate_role=cand_role,
        n_tasks=cmp.n_tasks,
        baseline_mean=cmp.baseline_mean,
        candidate_mean=cmp.candidate_mean,
        difference=cmp.difference,
        se_rerun=cmp.se_rerun,
        df_rerun=cmp.df_rerun,
        ci_rerun=[cmp.ci_rerun[0], cmp.ci_rerun[1]] if cmp.ci_rerun else None,
        p_rerun=cmp.p_rerun,
        p_perm_rerun=cmp.p_perm_rerun,
        perm_rerun_exact=cmp.perm_rerun_exact,
        verdict_rerun=cmp.verdict_rerun,
        se_tasks=cmp.se_tasks,
        ci_tasks=[cmp.ci_tasks[0], cmp.ci_tasks[1]] if cmp.ci_tasks else None,
        p_paired_t=cmp.p_paired_t,
        p_signflip=cmp.p_signflip,
        signflip_exact=cmp.signflip_exact,
        p_mcnemar=cmp.p_mcnemar,
        verdict_tasks=cmp.verdict_tasks,
        candidate_better=cmp.candidate_better,
        baseline_better=cmp.baseline_better,
        ties=cmp.ties,
        task_deltas=cmp.task_deltas,
    )


def _u_stat_best_of_k_task(sorted_vals: list[float], k: int) -> float:
    m = len(sorted_vals)
    assert 1 <= k <= m and sorted_vals[0] <= sorted_vals[-1]
    denom = math.comb(m, k)
    if denom <= (1 << 53):
        fdenom = float(denom)
        total = math.fsum((math.comb(i, k - 1) / fdenom) * sorted_vals[i] for i in range(k - 1, m))
    else:
        log_d = math.lgamma(m + 1.0) - math.lgamma(k + 1.0) - math.lgamma(m - k + 1.0)
        log_km1 = math.lgamma(k)
        total = math.fsum(
            math.exp(math.lgamma(i + 1.0) - log_km1 - math.lgamma(i - k + 2.0) - log_d)
            * sorted_vals[i]
            for i in range(k - 1, m)
        )
    tol = 1e-11 * max(1.0, abs(sorted_vals[0]), abs(sorted_vals[-1]))
    assert math.isfinite(total) and sorted_vals[0] - tol <= total <= sorted_vals[-1] + tol
    return min(sorted_vals[-1], max(sorted_vals[0], total))


def _trial_metric_mean(trials: list[NormalizedMultiAgentTrial], key: str) -> float | None:
    vals = [
        float(v)
        for tr in trials
        if (v := (tr.rewards or {}).get(key)) is not None
        and not isinstance(v, bool)
        and isinstance(v, (int, float))
        and math.isfinite(v)
    ]
    return _safe_fmean(vals) if vals else None


def compute_best_of_k_solo(
    shared: list[str],
    solo_outcomes: dict[str, list[float]],
    team_outcomes: dict[str, list[float]],
    team_trials: dict[str, list[NormalizedMultiAgentTrial]],
    solo_arm: ArmDecompositionSummary,
    team_arm: ArmDecompositionSummary,
    best_of_k: int | None,
) -> BestOfKSummary | None:
    k = best_of_k if best_of_k is not None else max(team_arm.seats_max, 1)
    if best_of_k is None and k < 2:
        return None
    assert k >= 1 and len(shared) >= 1
    emp_all, emp_unb = list[float](), list[float]()
    unbiased_vals: dict[str, float] = {}
    ba_means, sg_means = list[float](), list[float]()
    for t in shared:
        xs = sorted(solo_outcomes[t])
        emp_all.append(xs[-1])
        if len(xs) >= k:
            unbiased_vals[t] = _u_stat_best_of_k_task(xs, k)
            emp_unb.append(xs[-1])
        if (bm := _trial_metric_mean(team_trials[t], "best_attempt")) is not None:
            ba_means.append(bm)
        if (sm := _trial_metric_mean(team_trials[t], "selection_gap")) is not None:
            sg_means.append(sm)
    n_elig, n_unb = len(shared), len(unbiased_vals)
    unb_macro = _safe_fmean(list(unbiased_vals.values())) if n_unb > 0 else None
    emp_macro = _safe_fmean(emp_unb) if emp_unb else _safe_fmean(emp_all)
    assert unb_macro is None or unb_macro <= emp_macro + 1e-12
    tmb: float | None = None
    if unb_macro is not None:
        raw_tmb = (
            team_arm.score_macro - unb_macro
            if n_unb == n_elig
            else _safe_fmean([_safe_fmean(team_outcomes[t]) - v for t, v in unbiased_vals.items()])
        )
        tmb = raw_tmb if math.isfinite(raw_tmb) else None
    solo_c = (
        solo_arm.pace.mean_cost_usd_macro
        if solo_arm.pace.mean_cost_usd_macro is not None
        else solo_arm.pace.mean_cost_usd
    )
    proj = (k * solo_c) if solo_c is not None else None
    return BestOfKSummary(
        k=k,
        n_tasks_eligible=n_elig,
        n_tasks_unbiased=n_unb,
        unbiased_score_macro=unb_macro,
        empirical_max_macro=emp_macro,
        team_minus_best_of_k=tmb,
        projected_cost_usd=round(proj, 9) if proj is not None and math.isfinite(proj) else None,
        oracle_attempt_pool_mean=_safe_fmean(ba_means) if ba_means else None,
        mean_selection_gap=_safe_fmean(sg_means) if sg_means else None,
    )


def compute_shares_and_recovery(
    effects_out: dict[str, EffectEstimate],
    notes: list[str],
) -> tuple[float | None, float | None, float | None, float | None]:
    total_eff = effects_out["total"]
    coord_eff = effects_out["coordination"]
    head_eff = effects_out["headcount"]
    scale_3 = max(1.0, abs(total_eff.diff), abs(head_eff.diff), abs(coord_eff.diff))
    assert abs((head_eff.diff + coord_eff.diff) - total_eff.diff) < 1e-11 * scale_3
    coord_share = headcount_share = None
    total_resolved = total_eff.verdict_rerun == "resolved" or total_eff.verdict_tasks == "resolved"
    if total_resolved and abs(total_eff.difference) > 1e-12:
        total_abs = abs(total_eff.difference)
        if (
            abs(head_eff.difference) > 1e-9 * total_abs
            and abs(coord_eff.difference) > 1e-9 * total_abs
            and head_eff.difference * coord_eff.difference < 0.0
        ):
            notes.append(
                "shares_suppressed: headcount and coordination effects have opposite signs"
            )
        elif abs((head_eff.difference + coord_eff.difference) - total_eff.difference) > (
            1e-6 * total_abs
        ):
            notes.append(
                "shares_suppressed: floating-point cancellation between total and component deltas"
            )
        else:
            raw_coord = coord_eff.difference / total_eff.difference
            if math.isfinite(raw_coord):
                coord_share = min(1.0, max(0.0, raw_coord))
                headcount_share = 1.0 - coord_share
    split_cost = rec_ratio = None
    solo_minus_silent = -head_eff.difference
    if solo_minus_silent > 1e-9:
        split_cost = solo_minus_silent
        raw_rec = coord_eff.difference / solo_minus_silent
        if math.isfinite(raw_rec):
            rec_ratio = raw_rec
    return coord_share, headcount_share, split_cost, rec_ratio


def format_decompose_json(report: DecompositionReport) -> str:
    return json.dumps(report.model_dump(mode="json"), allow_nan=False, sort_keys=True, indent=2)


def _render_arm_line(role: str, a: ArmDecompositionSummary) -> str:
    p = a.pace
    ci_r = f"[{a.ci_rerun[0]:.4f}, {a.ci_rerun[1]:.4f}]" if a.ci_rerun else "n/a"
    ci_t = f"[{a.ci_tasks[0]:.4f}, {a.ci_tasks[1]:.4f}]" if a.ci_tasks else "n/a"
    cost_s = f"${p.mean_cost_usd:.4f}" if p.mean_cost_usd is not None else "n/a"
    if role != "solo" and p.cost_ratio_vs_solo_paired is not None:
        cost_s += f"({p.cost_ratio_vs_solo_paired:.2f}x)"
    wall_s = f"{p.mean_wall_time_sec:.1f}s" if p.mean_wall_time_sec is not None else "n/a"
    ah_s = f"{p.mean_agent_hours:.4f}h" if p.mean_agent_hours is not None else "n/a"
    return (
        f"  {role:17s}: seats={a.seats_min}-{a.seats_max} trials={a.n_trials_shared} "
        f"score={a.score_macro:.4f} rerun={ci_r} tasks={ci_t} "
        f"cost={cost_s} wall={wall_s} agent_h={ah_s}"
    )


def _render_effect_line(eff_name: str, e: EffectEstimate) -> str:
    if e.ci_rerun is not None:
        rerun_s = f"[{e.ci_rerun[0]:+.4f}, {e.ci_rerun[1]:+.4f}]"
    elif e.p_perm_rerun is not None:
        ex_tag = ",exact" if e.perm_rerun_exact else ""
        rerun_s = f"perm_p={e.p_perm_rerun:.4g}{ex_tag}"
    else:
        rerun_s = "n/a"
    ci_t = f"[{e.ci_tasks[0]:+.4f}, {e.ci_tasks[1]:.4f}]" if e.ci_tasks else "n/a"
    pt = f"{e.p_paired_t:.4g}" if e.p_paired_t is not None else "n/a"
    pf = f"{e.p_signflip:.4g}" if e.p_signflip is not None else "n/a"
    mc = f" mcnemar_p={e.p_mcnemar:.4g}" if e.p_mcnemar is not None else ""
    return (
        f"  {eff_name:24s}: diff={e.difference:+.4f} rerun={rerun_s} ({e.verdict_rerun}) "
        f"tasks={ci_t} t_p={pt} flip_p={pf}{mc} ({e.verdict_tasks})"
    )


def _render_bok_line(bok: BestOfKSummary) -> str:
    unb = (
        f"{bok.unbiased_score_macro:.4f}"
        if bok.unbiased_score_macro is not None
        else f"n/a ({bok.n_tasks_unbiased}/{bok.n_tasks_eligible} tasks with m>=k)"
    )
    tmb = f"{bok.team_minus_best_of_k:+.4f}" if bok.team_minus_best_of_k is not None else "n/a"
    pc = f"${bok.projected_cost_usd:.4f}" if bok.projected_cost_usd is not None else "n/a"
    parts = [
        f"k={bok.k}",
        f"unbiased={unb}",
        f"empirical_max={bok.empirical_max_macro:.4f}",
        f"team_minus_bok={tmb}",
        f"proj_cost={pc}",
    ]
    if bok.oracle_attempt_pool_mean is not None:
        parts.append(f"oracle_pool={bok.oracle_attempt_pool_mean:.4f}")
    if bok.mean_selection_gap is not None:
        parts.append(f"selection_gap={bok.mean_selection_gap:.4f}")
    return "Best-of-K Solo: " + " ".join(parts)


def render_decomposition_summary(report: DecompositionReport) -> str:
    lines = [
        f"Coordination Decomposition (metric={report.metric}, counting={report.counting}, "
        f"shared_tasks={len(report.shared_tasks)}, level={report.level:.0%})",
        "",
        "Arms & PACE Resources:",
    ]
    for role in ("solo", "equal_budget_solo", "silent", "team"):
        if role in report.arms:
            lines.append(_render_arm_line(role, report.arms[role]))
    lines.extend(["", "Effects:"])
    eff_order = (
        "headcount",
        "coordination",
        "total",
        "budget_scaling",
        "fragmentation_gap",
        "silent_fragmentation_gap",
    )
    for eff_name in eff_order:
        if eff_name in report.effects:
            lines.append(_render_effect_line(eff_name, report.effects[eff_name]))
    if report.best_of_k_solo is not None:
        lines.extend(["", _render_bok_line(report.best_of_k_solo)])
    if report.coordination_share is not None and report.headcount_share is not None:
        lines.append(
            f"\nShares: coordination={report.coordination_share:.1%}, "
            f"headcount={report.headcount_share:.1%}"
        )
    rec_parts: list[str] = []
    if report.split_or_conflict_cost is not None:
        rec_parts.append(f"split_cost={report.split_or_conflict_cost:.4f}")
    if report.recovery_ratio is not None:
        rec_parts.append(f"recovery_ratio={report.recovery_ratio:.4f}")
    if report.solo_budget_recovery_ratio is not None:
        rec_parts.append(f"solo_budget_recovery={report.solo_budget_recovery_ratio:.4f}")
    if report.fragmentation_recovery_ratio is not None:
        rec_parts.append(f"fragmentation_recovery={report.fragmentation_recovery_ratio:.4f}")
    if rec_parts:
        lines.append("Recovery: " + ", ".join(rec_parts))
    return "\n".join(lines)
