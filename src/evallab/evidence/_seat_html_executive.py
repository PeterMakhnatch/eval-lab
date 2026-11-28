from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._forensics_join import join_steps_and_events
from ._multiagent_io import safe_fsum
from ._seat_html_cda import aggregate_cda_reports, dump_trial_cda, merge_corpus_cda
from .bespoke_public_rollout import (
    BespokePublicRun,
    analyze_loaded_bespoke_public_package,
    load_bespoke_public_package,
)
from .multiagent_adapter import (
    NormalizedMultiAgentTrial,
    discover_trial_dirs,
    is_trial_dir,
    read_multiagent_trial,
)
from .seat_cda import TrialCdaReport, analyze_normalized_trial_cda
from .seat_decompose import decompose_coordination
from .seat_forensics import (
    TrialForensicsReport,
    analyze_normalized_trial,
)
from .world_events import WorldEventLog, load_world_events

TimelineBuilder = Callable[
    [NormalizedMultiAgentTrial, WorldEventLog, dict[tuple[str, int], int], int],
    dict[str, Any],
]

_FUNNEL_INT_KEYS = (
    "messages_posted",
    "messages_fetched",
    "messages_in_context",
    "messages_acted_on",
    "unread_at_action",
    "never_fetched",
    "message_delivery_pairs",
    "unique_messages_fetched",
    "unique_messages_never_fetched",
    "messages_fetched_but_ignored",
    "unread_messages_at_action_total",
    "fetch_calls",
)


def _integrity_counts_for_trial(report: TrialForensicsReport) -> dict[str, int]:
    return {
        "logs_complete": int(report.integrity.logs_complete),
        "logs_incomplete": int(not report.integrity.logs_complete),
        "cost_complete": int(report.integrity.cost_complete),
        "cost_incomplete": int(not report.integrity.cost_complete),
        "seat_error_with_reward": int(report.integrity.seat_error_with_reward),
        "unauthenticated_author_mismatch": int(report.integrity.unauthenticated_author_mismatch),
        "event_log_present": int(report.integrity.event_log_present),
        "event_log_absent": int(not report.integrity.event_log_present),
    }


def _dump_trial_entry(
    report: TrialForensicsReport,
    cda_report: TrialCdaReport,
    trial: NormalizedMultiAgentTrial,
    wlog: WorldEventLog,
    corpus_label: str,
    build_timeline: TimelineBuilder,
    max_events_per_trial: int,
) -> dict[str, Any]:
    roster_set = {m.member_id for m in trial.members}
    observable_steps = {
        m.member_id: trial.member_steps[m.member_id]
        for m in trial.members
        if m.trajectory_status == "present" and trial.member_steps.get(m.member_id)
    }
    _, ev_map = join_steps_and_events(observable_steps, wlog.events, wlog.git_events, roster_set)
    dumped = report.model_dump(mode="json")
    dumped["corpus_label"] = corpus_label
    full_eps = dumped["failure_episodes"]
    dumped["truncated_failure_episodes"] = max(0, len(full_eps) - 120)
    dumped["causal_links"] = dumped["causal_links"][:50]
    dumped["failure_episodes"] = full_eps[:120]
    dumped["timeline"] = build_timeline(trial, wlog, ev_map, max_events_per_trial)
    dumped["cda"] = dump_trial_cda(cda_report)
    return dumped


def _select_representative_pairs[T](
    pairs: list[tuple[TrialForensicsReport, T]],
    limit: int,
    tie_key: Callable[[T], str],
) -> list[tuple[TrialForensicsReport, T]]:
    if limit <= 0:
        return []
    if len(pairs) <= limit:
        return sorted(pairs, key=lambda pair: (pair[0].trial_name, tie_key(pair[1])))
    ranked = sorted(
        pairs,
        key=lambda pair: (
            -sum(pair[0].failure_mode_counts.values()),
            -(pair[0].funnel.messages_posted + pair[0].n_world_events + pair[0].n_git_commits),
            pair[0].trial_name,
            tie_key(pair[1]),
        ),
    )
    selected = ranked[:limit]
    selected.sort(key=lambda pair: (pair[0].trial_name, tie_key(pair[1])))
    return selected


def _accumulate_trial_metrics(
    rep: TrialForensicsReport,
    funnel_sum: Counter[str],
    coll_sum: Counter[str],
    integ_sum: Counter[str],
    mode_sum: Counter[str],
) -> None:
    fd = rep.funnel.model_dump(mode="json")
    for k in _FUNNEL_INT_KEYS:
        funnel_sum[k] += int(fd.get(k) or 0)
    cd = rep.collisions.model_dump(mode="json")
    for k, v in cd.items():
        if isinstance(v, int) and not isinstance(v, bool):
            coll_sum[k] += v
    for k, v in _integrity_counts_for_trial(rep).items():
        integ_sum[k] += v
    for k, v in rep.failure_mode_counts.items():
        mode_sum[k] += v


def _load_trial_and_events(td: Path) -> tuple[NormalizedMultiAgentTrial, WorldEventLog]:
    trial = read_multiagent_trial(td)
    wlog = load_world_events(
        td,
        roster=[m.member_id for m in trial.members],
        member_aliases=trial.member_aliases,
        agent_execution_finished_at=trial.agent_execution_finished_at,
    )
    return trial, wlog


def _summarize_single_trial(
    resolved: Path,
    corpus_label: str,
    trial_quota: int,
    max_events_per_trial: int,
    build_timeline: TimelineBuilder,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    trial, wlog = _load_trial_and_events(resolved)
    rep = analyze_normalized_trial(trial, wlog)
    cda_rep = analyze_normalized_trial_cda(trial, wlog, trial_dir=resolved)
    trials_out = (
        [
            _dump_trial_entry(
                rep, cda_rep, trial, wlog, corpus_label, build_timeline, max_events_per_trial
            )
        ]
        if trial_quota > 0
        else []
    )
    known_cost = rep.total_cost_usd if rep.total_cost_usd is not None else rep.known_cost_usd
    summary = {
        "label": corpus_label,
        "source_path": resolved.as_posix(),
        "n_trials": 1,
        "ecosystems": {rep.ecosystem: 1},
        "events_total": rep.n_world_events + rep.n_git_commits,
        "causal_links_total": rep.causal_links_count,
        "known_cost_usd": (
            round(known_cost, 6) if known_cost is not None and math.isfinite(known_cost) else None
        ),
        "funnel": rep.funnel.model_dump(mode="json"),
        "collisions": rep.collisions.model_dump(mode="json"),
        "integrity_summary": _integrity_counts_for_trial(rep),
        "failure_mode_counts": dict(rep.failure_mode_counts),
        "cda": aggregate_cda_reports([cda_rep]),
    }
    return summary, trials_out


def _aggregate_forensics_reports(
    reports: list[TrialForensicsReport],
) -> tuple[Counter[str], dict[str, Any], Counter[str], Counter[str], Counter[str], int, int]:
    eco_counts: Counter[str] = Counter()
    funnel_sum: Counter[str] = Counter()
    coll_sum: Counter[str] = Counter()
    integ_sum: Counter[str] = Counter()
    mode_sum: Counter[str] = Counter()
    events_total = links_total = 0
    all_ctx_obs = bool(reports)
    for rep in reports:
        eco_counts[rep.ecosystem] += 1
        events_total += rep.n_world_events + rep.n_git_commits
        links_total += rep.causal_links_count
        if not rep.funnel.context_observable:
            all_ctx_obs = False
        _accumulate_trial_metrics(rep, funnel_sum, coll_sum, integ_sum, mode_sum)
    funnel_dict: dict[str, Any] = dict(funnel_sum)
    funnel_dict["context_observable"] = all_ctx_obs
    return eco_counts, funnel_dict, coll_sum, integ_sum, mode_sum, events_total, links_total


def summarize_disk_corpus(
    path: Path,
    *,
    label: str | None = None,
    trial_quota: int = 100,
    max_events_per_trial: int = 300,
    build_timeline: TimelineBuilder,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    resolved = path.resolve()
    corpus_label = label or resolved.name
    if is_trial_dir(resolved):
        return _summarize_single_trial(
            resolved, corpus_label, trial_quota, max_events_per_trial, build_timeline
        )

    trial_dirs, _, _ = discover_trial_dirs(resolved)
    if not trial_dirs:
        raise ValueError(f"no Harbor trials found under {path}")

    cost_parts: list[float] = []
    pairs: list[tuple[TrialForensicsReport, tuple[Path, TrialCdaReport]]] = []
    cda_reports: list[TrialCdaReport] = []
    for td in trial_dirs:
        trial, wlog = _load_trial_and_events(td)
        rep = analyze_normalized_trial(trial, wlog)
        cda_rep = analyze_normalized_trial_cda(trial, wlog, trial_dir=td)
        cda_reports.append(cda_rep)
        pairs.append((rep, (td, cda_rep)))
        c_val = rep.total_cost_usd if rep.total_cost_usd is not None else rep.known_cost_usd
        if c_val is not None and math.isfinite(c_val):
            cost_parts.append(c_val)

    eco_c, funnel_d, coll_s, integ_s, mode_s, ev_tot, lk_tot = _aggregate_forensics_reports(
        [r for r, _ in pairs]
    )
    chosen = _select_representative_pairs(pairs, trial_quota, lambda item: item[0].as_posix())
    trials_json: list[dict[str, Any]] = []
    for rep, (td, cda_rep) in chosen:
        trial, wlog = _load_trial_and_events(td)
        trials_json.append(
            _dump_trial_entry(
                rep, cda_rep, trial, wlog, corpus_label, build_timeline, max_events_per_trial
            )
        )
    known_cost = safe_fsum(cost_parts) if cost_parts else None
    summary = {
        "label": corpus_label,
        "source_path": resolved.as_posix(),
        "n_trials": len(pairs),
        "ecosystems": dict(sorted(eco_c.items())),
        "events_total": ev_tot,
        "causal_links_total": lk_tot,
        "known_cost_usd": (
            round(known_cost, 6) if known_cost is not None and math.isfinite(known_cost) else None
        ),
        "funnel": funnel_d,
        "collisions": dict(coll_s),
        "integrity_summary": dict(integ_s),
        "failure_mode_counts": dict(sorted(mode_s.items())),
        "cda": aggregate_cda_reports(cda_reports),
    }
    return summary, trials_json


def _bespoke_integrity_summary(
    integ_sum: Counter[str], cost_complete_n: int, n_bench_runs: int, n_forensics: int
) -> dict[str, int]:
    return {
        "logs_complete": integ_sum.get("logs_complete", 0),
        "logs_incomplete": integ_sum.get("logs_incomplete", 0),
        "cost_complete": cost_complete_n,
        "cost_incomplete": max(0, n_bench_runs - cost_complete_n),
        "seat_error_with_reward": integ_sum.get("seat_error_with_reward", 0),
        "unauthenticated_author_mismatch": integ_sum.get("unauthenticated_author_mismatch", 0),
        "event_log_present": integ_sum.get("event_log_present", 0),
        "event_log_absent": max(0, n_forensics - integ_sum.get("event_log_present", 0)),
    }


def _known_bespoke_cost(runs: list[BespokePublicRun]) -> tuple[float | None, int]:
    parts = [
        c
        for r in runs
        if (
            c := (
                r.trial.total_cost_usd
                if r.trial.total_cost_usd is not None
                else r.trial.known_cost_usd
            )
        )
        is not None
        and math.isfinite(c)
    ]
    raw = safe_fsum(parts) if parts else None
    rounded = round(raw, 6) if raw is not None and math.isfinite(raw) else None
    return rounded, sum(1 for r in runs if r.trial.cost_complete)


def summarize_bespoke_public_source(
    path: Path,
    *,
    label: str | None = None,
    trial_quota: int = 12,
    max_events_per_trial: int = 300,
    build_timeline: TimelineBuilder,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    resolved = path.resolve()
    pkg = load_bespoke_public_package(resolved)
    pub = analyze_loaded_bespoke_public_package(pkg)
    corpus_label = label or f"Bespoke Public ({resolved.stem})"
    cda_reports = [
        analyze_normalized_trial_cda(r.trial, r.world_log, trial_dir=None) for r in pkg.runs
    ]
    pairs: list[tuple[TrialForensicsReport, tuple[BespokePublicRun, TrialCdaReport]]] = list(
        zip(pub.forensics_reports, zip(pkg.runs, cda_reports, strict=True), strict=True)
    )
    chosen_bp = _select_representative_pairs(
        pairs, trial_quota, lambda item: f"{item[0].task_slug}/{item[0].run_id}"
    )
    trials_json: list[dict[str, Any]] = [
        _dump_trial_entry(
            rep,
            cda_rep,
            run.trial,
            run.world_log,
            corpus_label,
            build_timeline,
            max_events_per_trial,
        )
        for rep, (run, cda_rep) in chosen_bp
    ]
    _, funnel_d, coll_s, integ_s, mode_s, ev_tot, lk_tot = _aggregate_forensics_reports(
        list(pub.forensics_reports)
    )
    known_cost, cost_complete_n = _known_bespoke_cost(pkg.runs)
    n_bench_runs = pub.n_runs
    corpus_summary = {
        "label": corpus_label,
        "source_path": resolved.as_posix(),
        "n_trials": n_bench_runs,
        "ecosystems": {"bespoke_team": n_bench_runs},
        "events_total": ev_tot,
        "causal_links_total": lk_tot,
        "known_cost_usd": known_cost,
        "funnel": funnel_d,
        "collisions": dict(coll_s),
        "integrity_summary": _bespoke_integrity_summary(
            integ_s, cost_complete_n, n_bench_runs, len(pub.forensics_reports)
        ),
        "failure_mode_counts": dict(sorted(mode_s.items())),
        "cda": aggregate_cda_reports(cda_reports),
    }
    bespoke_entry = {
        "label": corpus_label,
        "source_path": resolved.as_posix(),
        "n_runs": pub.n_runs,
        "n_with_board_log": pub.n_with_board_log,
        "n_with_agent_trajectories": pub.n_with_agent_trajectories,
        "mark_hours": pub.mark_hours,
        "decompositions": [d.model_dump(mode="json") for d in pub.decompositions],
    }
    return corpus_summary, bespoke_entry, trials_json


def summarize_decomposition_spec(
    label: str,
    solo: Path,
    silent: Path,
    team: Path,
    *,
    equal_budget_solo: Path | None = None,
    task_key_regex: str | None = None,
) -> dict[str, Any]:
    rep = decompose_coordination(
        solo.resolve(),
        silent.resolve(),
        team.resolve(),
        equal_budget_solo_dir=equal_budget_solo.resolve()
        if equal_budget_solo is not None
        else None,
        task_key_regex=task_key_regex,
    )
    dumped = rep.model_dump(mode="json")
    dumped["label"] = label
    return dumped


def merge_corpus_overviews(corpora: list[dict[str, Any]]) -> dict[str, Any]:
    ecosystems: Counter[str] = Counter()
    funnel: Counter[str] = Counter()
    collisions: Counter[str] = Counter()
    integrity: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    ctx_flags: list[bool] = []
    for c in corpora:
        for k, v in (c.get("ecosystems") or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                ecosystems[k] += v
        for k, v in (c.get("funnel") or {}).items():
            if k == "context_observable" and isinstance(v, bool):
                ctx_flags.append(v)
            elif isinstance(v, int) and not isinstance(v, bool):
                funnel[k] += v
        for k, v in (c.get("collisions") or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                collisions[k] += v
        for k, v in (c.get("integrity_summary") or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                integrity[k] += v
        for k, v in (c.get("failure_mode_counts") or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                modes[k] += v
    funnel_out: dict[str, Any] = dict(funnel)
    if ctx_flags:
        funnel_out["context_observable"] = all(ctx_flags)
    return {
        "ecosystems": dict(sorted(ecosystems.items())),
        "funnel": funnel_out,
        "collisions": dict(collisions),
        "integrity_summary": dict(integrity),
        "failure_mode_counts": dict(sorted(modes.items())),
        "cda": merge_corpus_cda(corpora),
    }
