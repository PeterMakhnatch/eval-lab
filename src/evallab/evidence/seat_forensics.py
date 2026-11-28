from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ._forensics_join import StepEventLink
from ._forensics_metrics import trial_comms_disabled
from ._forensics_models import (
    BatchHerdingSummary,
    CollisionMetrics,
    HerdingMetrics,
    IntegrityFlags,
    JobForensicsReport,
    MemberForensicsReport,
    MessageFunnelMetrics,
    TrialForensicsReport,
    render_forensics_summary,
)
from ._forensics_trial_builder import (
    build_members_and_integrity,
    build_trial_funnel_and_collisions,
    build_trial_slices,
    compute_trial_metrics_bundle,
)
from ._herding_models import extract_seat_rewards
from .multiagent_adapter import (
    NormalizedMultiAgentTrial,
    discover_trial_dirs,
    read_multiagent_trial,
)
from .seat_failure_modes import FailureEpisode, detect_failure_episodes
from .seat_herding import aggregate_herding_summary, compute_herding_metrics
from .world_events import WorldEventLog, load_world_events

__all__ = [
    "BatchHerdingSummary",
    "CollisionMetrics",
    "FailureEpisode",
    "HerdingMetrics",
    "IntegrityFlags",
    "JobForensicsReport",
    "MemberForensicsReport",
    "MessageFunnelMetrics",
    "StepEventLink",
    "TrialForensicsReport",
    "analyze_forensics",
    "analyze_normalized_trial",
    "analyze_trial_forensics",
    "format_forensics_json",
    "render_forensics_text",
]


def analyze_trial_forensics(trial_dir: Path) -> TrialForensicsReport:
    trial = read_multiagent_trial(trial_dir)
    roster = [m.member_id for m in trial.members]
    world_log = load_world_events(
        trial_dir,
        roster=roster,
        member_aliases=trial.member_aliases,
        agent_execution_finished_at=trial.agent_execution_finished_at,
    )
    return analyze_normalized_trial(trial, world_log)


def analyze_normalized_trial(
    trial: NormalizedMultiAgentTrial, world_log: WorldEventLog
) -> TrialForensicsReport:
    roster = [m.member_id for m in trial.members]
    roster_set = set(roster)
    slices = build_trial_slices(trial, world_log, roster_set)
    mb = compute_trial_metrics_bundle(trial, world_log, roster, roster_set, slices)
    comms_off = trial_comms_disabled(trial, world_log)
    seat_rewards = extract_seat_rewards(trial)
    member_reports, integrity = build_members_and_integrity(
        trial, world_log, roster_set, slices, mb, comms_off, seat_rewards
    )
    herding = compute_herding_metrics(
        roster=roster,
        roster_set=roster_set,
        in_play_events=slices.in_play_events,
        all_in_play_actions=slices.all_in_play_actions,
        actions_by_member=slices.actions_by_member,
        peer_posts=slices.peer_posts,
        first_fetch=slices.first_fetch,
        event_to_step=slices.event_to_step,
        timed_reads=slices.timed_reads,
        comms_disabled=comms_off,
        seat_rewards=seat_rewards,
        duplicate_target_actions=sum(mb.dup_by_member.values()),
    )
    failure_episodes, failure_mode_counts = detect_failure_episodes(
        trial, world_log, roster=roster, roster_set=roster_set,
        in_play_events=slices.in_play_events, in_play_git=slices.in_play_git,
        all_in_play_actions=slices.all_in_play_actions, peer_posts=slices.peer_posts,
        first_fetch=slices.first_fetch, in_context_steps=slices.in_context_steps,
        actions_by_member=slices.actions_by_member, informed_dup_keys=mb.informed_dup_keys,
        event_to_step=slices.event_to_step, unread_by_action=mb.unread_by_action,
        timed_reads=slices.timed_reads, observable_steps=slices.observable_steps,
        excluded_mut_steps=mb.excluded_mut_steps, shared_branches_set=set(mb.shared_branches),
    )
    funnel, collisions = build_trial_funnel_and_collisions(
        roster, roster_set, slices, mb, member_reports
    )
    return TrialForensicsReport(
        trial_name=trial.trial_name, trial_id=trial.trial_id, task_name=trial.task_name,
        ecosystem=trial.ecosystem, orchestration=trial.orchestration,
        board_policy=trial.board_policy, comms_disabled=comms_off, grade_marks=trial.grade_marks,
        source_formats=world_log.source_formats, rewards=trial.rewards,
        reward_valid=trial.reward_valid, trial_exception_type=trial.trial_exception_type,
        total_cost_usd=trial.total_cost_usd, known_cost_usd=trial.known_cost_usd,
        total_input_tokens=trial.total_input_tokens, total_cache_tokens=trial.total_cache_tokens,
        total_output_tokens=trial.total_output_tokens, n_members=len(trial.members),
        n_world_events=len(world_log.events), n_lifecycle_events=len(world_log.lifecycle_events),
        n_git_commits=len(world_log.git_commits), causal_links_count=len(slices.links),
        causal_links_by_basis=slices.links_by_basis, end_of_play_source=world_log.end_of_play_source,
        end_of_play_ts=world_log.end_of_play_ts, funnel=funnel, collisions=collisions,
        herding=herding, integrity=integrity, members=member_reports, causal_links=slices.links,
        failure_episodes=failure_episodes, failure_mode_counts=failure_mode_counts,
    )


def _build_integrity_summary(reports: list[TrialForensicsReport]) -> dict[str, int]:
    n_logs_ok = sum(1 for r in reports if r.integrity.logs_complete)
    n_cost_ok = sum(1 for r in reports if r.integrity.cost_complete)
    n_ev_ok = sum(1 for r in reports if r.integrity.event_log_present)
    return {
        "logs_complete": n_logs_ok,
        "logs_incomplete": len(reports) - n_logs_ok,
        "cost_complete": n_cost_ok,
        "cost_incomplete": len(reports) - n_cost_ok,
        "seat_error_with_reward": sum(1 for r in reports if r.integrity.seat_error_with_reward),
        "unauthenticated_author_mismatch": sum(
            1 for r in reports if r.integrity.unauthenticated_author_mismatch
        ),
        "event_log_present": n_ev_ok,
        "event_log_absent": len(reports) - n_ev_ok,
        "network_not_enforced": sum(1 for r in reports if r.integrity.network_not_enforced),
        "budget_unbounded_subscription": sum(
            1 for r in reports if r.integrity.budget_unbounded_subscription
        ),
        "covert_git_signaling_when_silent": sum(
            1 for r in reports if r.integrity.covert_git_signaling_when_silent
        ),
    }


def analyze_forensics(path: Path) -> TrialForensicsReport | JobForensicsReport:
    trial_dirs, job_id, discovery_issues = discover_trial_dirs(path)
    if len(trial_dirs) == 1 and trial_dirs[0] == path.resolve():
        return analyze_trial_forensics(trial_dirs[0])
    if not trial_dirs and job_id is None and not discovery_issues:
        raise ValueError(f"no Harbor trials found under {path}")

    reports = sorted(
        (analyze_trial_forensics(t) for t in trial_dirs),
        key=lambda r: (r.trial_name, r.trial_id or ""),
    )
    total_fetched = sum(r.funnel.messages_fetched for r in reports)
    total_acted = sum(r.funnel.messages_acted_on for r in reports)
    mode_counts = sum((Counter(r.failure_mode_counts) for r in reports), Counter[str]())
    return JobForensicsReport(
        target_path=path.resolve().as_posix(),
        job_id=job_id,
        n_trials=len(reports),
        ecosystems=dict(sorted(Counter(r.ecosystem for r in reports).items())),
        funnel=MessageFunnelMetrics(
            messages_posted=sum(r.funnel.messages_posted for r in reports),
            messages_fetched=total_fetched,
            messages_in_context=sum(r.funnel.messages_in_context for r in reports),
            messages_acted_on=total_acted,
            unread_at_action=sum(r.funnel.unread_at_action for r in reports),
            never_fetched=sum(r.funnel.never_fetched for r in reports),
            message_delivery_pairs=sum(r.funnel.message_delivery_pairs for r in reports),
            unique_messages_fetched=sum(r.funnel.unique_messages_fetched for r in reports),
            unique_messages_never_fetched=sum(
                r.funnel.unique_messages_never_fetched for r in reports
            ),
            messages_fetched_but_ignored=max(0, total_fetched - total_acted),
            unread_messages_at_action_total=sum(
                r.funnel.unread_messages_at_action_total for r in reports
            ),
            fetch_calls=sum(r.funnel.fetch_calls for r in reports),
            context_observable=bool(reports) and all(r.funnel.context_observable for r in reports),
        ),
        collisions=CollisionMetrics(
            duplicate_target_actions=sum(r.collisions.duplicate_target_actions for r in reports),
            stale_read_write_races=sum(r.collisions.stale_read_write_races for r in reports),
            shared_branch_or_file_conflicts=sum(
                r.collisions.shared_branch_or_file_conflicts for r in reports
            ),
            post_end_of_play_events=sum(r.collisions.post_end_of_play_events for r in reports),
            informed_duplicate_actions=sum(
                r.collisions.informed_duplicate_actions for r in reports
            ),
            overwrite_events=sum(r.collisions.overwrite_events for r in reports),
        ),
        herding_summary=aggregate_herding_summary(reports),
        integrity_summary=_build_integrity_summary(reports),
        failure_mode_counts=dict(sorted(mode_counts.items())),
        discovery_issues=sorted(set(discovery_issues)),
        trials=reports,
    )


def format_forensics_json(report: TrialForensicsReport | JobForensicsReport) -> str:
    return json.dumps(report.model_dump(mode="json"), allow_nan=False, sort_keys=True, indent=2)


def render_forensics_text(report: TrialForensicsReport | JobForensicsReport) -> str:
    return render_forensics_summary(report)
