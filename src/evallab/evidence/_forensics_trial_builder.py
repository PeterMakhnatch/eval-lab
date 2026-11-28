from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any

from ._forensics_join import (
    StepEventLink,
    extract_timed_atif_file_reads,
    find_in_context_steps,
    join_steps_and_events,
)
from ._forensics_metrics import (
    compute_auth_mismatches,
    compute_duplicate_actions,
    compute_funnel_counts,
    compute_shared_conflicts,
    compute_stale_races,
    compute_unread_at_action,
    happened_before,
    is_post_eop_mutation,
    log_group,
    member_logs_complete,
)
from ._forensics_models import (
    CollisionMetrics,
    IntegrityFlags,
    MemberForensicsReport,
    MessageFunnelMetrics,
)
from ._forensics_sweep import compute_excluded_mut_steps
from ._herding_models import evaluate_extra_integrity
from ._multiagent_io import MemberRecord, NormalizedAtifStep
from .multiagent_adapter import NormalizedMultiAgentTrial
from .world_events import NormalizedWorldEvent, WorldEventLog


@dataclass
class TrialSlices:
    observable_steps: dict[str, list[NormalizedAtifStep]]
    links: list[StepEventLink]
    event_to_step: dict[tuple[str, int], int]
    links_by_basis: dict[str, int]
    linked_count_by_member: Counter[str]
    in_play_events: list[NormalizedWorldEvent]
    in_play_git: list[NormalizedWorldEvent]
    all_in_play_actions: list[NormalizedWorldEvent]
    peer_posts: list[NormalizedWorldEvent]
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent]
    fetch_calls_by_member: Counter[str]
    in_context_steps: dict[tuple[str, str, str], int]
    timed_reads: list[tuple[float, str, str]]
    actions_by_member: dict[str, list[NormalizedWorldEvent]]


@dataclass
class TrialMetricsBundle:
    dup_by_member: Counter[str]
    informed_dup_by_member: Counter[str]
    informed_dup_keys: set[tuple[str, int]]
    stale_total: int
    stale_by_member: dict[str, int]
    shared_conflicts_total: int
    shared_branches: dict[str, list[str]]
    shared_files: dict[str, list[str]]
    proposed_collisions: dict[str, list[str]]
    overwrites_by_member: Counter[str]
    extra_overwrites_by_member: Counter[str]
    extra_proposed: set[str]
    collision_details: list[dict[str, Any]]
    excluded_mut_steps: dict[str, set[int]]
    m_posted: Counter[str]
    m_pairs: Counter[str]
    m_fetched: Counter[str]
    m_never: Counter[str]
    m_in_ctx: Counter[str]
    m_acted: Counter[str]
    lags_by_member: dict[str, list[float]]
    all_lags: list[float]
    unique_fetched_ids: set[tuple[str, str]]
    unread_actions_by_member: Counter[str]
    unread_total_by_member: Counter[str]
    action_unread_counts: dict[str, list[int]]
    all_unread_counts: list[int]
    unread_by_action: dict[int, list[NormalizedWorldEvent]]
    post_eop_by_member: Counter[str]
    post_eop_total: int


def _sort_in_play_actions(actions: list[NormalizedWorldEvent]) -> None:
    action_groups = {log_group(ev.source_format) for ev in actions}
    if len(action_groups) == 1 and next(iter(action_groups)) != "bespoke_git_bundle":
        actions.sort(key=lambda ev: (ev.seq, ev.source_format))
    elif actions:
        actions.sort(
            key=lambda ev: (
                0 if ev.ts_epoch is not None else 1,
                ev.ts_epoch if ev.ts_epoch is not None else 0.0,
                log_group(ev.source_format),
                ev.seq,
            )
        )


def _collect_first_fetches(
    in_play_events: list[NormalizedWorldEvent],
    roster_set: set[str],
) -> tuple[dict[tuple[str, str, str], NormalizedWorldEvent], Counter[str]]:
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent] = {}
    fetch_calls: Counter[str] = Counter()
    for ev in in_play_events:
        if ev.actor not in roster_set or not ev.accepted:
            continue
        if ev.kind == "message_fetch":
            fetch_calls[ev.actor] += 1
        grp = log_group(ev.source_format)
        for mid in ev.fetched_msg_ids:
            fkey = (ev.actor, grp, mid)
            cur = first_fetch.get(fkey)
            if cur is None or happened_before(ev, cur):
                first_fetch[fkey] = ev
    return first_fetch, fetch_calls


def build_trial_slices(
    trial: NormalizedMultiAgentTrial,
    world_log: WorldEventLog,
    roster_set: set[str],
) -> TrialSlices:
    observable_steps = {
        m.member_id: trial.member_steps[m.member_id]
        for m in trial.members
        if m.trajectory_status == "present" and trial.member_steps.get(m.member_id)
    }
    links, event_to_step = join_steps_and_events(
        observable_steps, world_log.events, world_log.git_events, roster_set
    )
    links_by_basis = {
        str(k): v for k, v in sorted(Counter(link.match_basis for link in links).items())
    }
    in_play_events = [ev for ev in world_log.events if ev.phase != "post_play" and not ev.late]
    in_play_git = [ev for ev in world_log.git_events if ev.phase != "post_play" and not ev.late]
    all_in_play_actions = [
        ev
        for ev in (*in_play_events, *in_play_git)
        if ev.kind in ("world_action", "artifact_write") and ev.accepted and ev.actor in roster_set
    ]
    _sort_in_play_actions(all_in_play_actions)
    peer_posts = [
        ev
        for ev in in_play_events
        if ev.kind == "message_post"
        and ev.accepted
        and ev.actor in roster_set
        and ev.msg_id is not None
    ]
    posted_by_id = {
        (log_group(ev.source_format), ev.msg_id): ev for ev in peer_posts if ev.msg_id is not None
    }
    first_fetch, fetch_calls_by_member = _collect_first_fetches(in_play_events, roster_set)
    in_context_steps = find_in_context_steps(
        posted_by_id, first_fetch, observable_steps, event_to_step
    )
    needs_file_reads = bool(in_play_git) or any(
        ev.kind == "artifact_write" for ev in in_play_events
    )
    timed_reads = extract_timed_atif_file_reads(observable_steps) if needs_file_reads else []
    actions_by_member: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
    for act in all_in_play_actions:
        if act.actor is not None:
            actions_by_member[act.actor].append(act)
    return TrialSlices(
        observable_steps=observable_steps,
        links=links,
        event_to_step=event_to_step,
        links_by_basis=links_by_basis,
        linked_count_by_member=Counter(link.member_id for link in links),
        in_play_events=in_play_events,
        in_play_git=in_play_git,
        all_in_play_actions=all_in_play_actions,
        peer_posts=peer_posts,
        first_fetch=first_fetch,
        fetch_calls_by_member=fetch_calls_by_member,
        in_context_steps=in_context_steps,
        timed_reads=timed_reads,
        actions_by_member=actions_by_member,
    )


def _count_post_eop_mutations(
    world_log: WorldEventLog, roster_set: set[str]
) -> tuple[Counter[str], int]:
    post_eop_by_member: Counter[str] = Counter()
    post_eop_total = 0
    for ev in (*world_log.events, *world_log.git_events):
        if is_post_eop_mutation(ev):
            post_eop_total += 1
            if ev.actor in roster_set:
                post_eop_by_member[ev.actor] += 1
    return post_eop_by_member, post_eop_total


def compute_trial_metrics_bundle(
    trial: NormalizedMultiAgentTrial,
    world_log: WorldEventLog,
    roster: list[str],
    roster_set: set[str],
    slices: TrialSlices,
) -> TrialMetricsBundle:
    dup_by_m, inf_dup_by_m, inf_dup_keys, dup_det = compute_duplicate_actions(
        slices.all_in_play_actions, slices.peer_posts, slices.first_fetch,
        in_play_events=slices.in_play_events, timed_reads=slices.timed_reads,
    )
    stale_tot, stale_by_m, stale_det = compute_stale_races(
        slices.in_play_events, slices.in_play_git, slices.observable_steps,
        roster_set, timed_reads=slices.timed_reads,
    )
    sh_tot, sh_br, sh_fi, prop_col, ow_by_m, ex_ow_by_m, ex_prop, ow_det = compute_shared_conflicts(
        slices.in_play_events, slices.peer_posts, world_log, roster_set,
        in_play_git=slices.in_play_git,
    )
    excl_mut = compute_excluded_mut_steps(
        world_log.events, world_log.git_events, inf_dup_keys, slices.event_to_step
    )
    m_post, m_pair, m_fch, m_nev, m_ctx, m_act, lags_m, all_lags, uniq_f = compute_funnel_counts(
        slices.peer_posts, roster, roster_set, slices.first_fetch, slices.in_context_steps,
        slices.actions_by_member, inf_dup_keys, slices.event_to_step, trial,
        observable_steps=slices.observable_steps, excluded_mut_steps=excl_mut,
    )
    unr_acts_m, unr_tot_m, act_unr_c, all_unr_c, unr_by_act = compute_unread_at_action(
        slices.all_in_play_actions, slices.peer_posts, slices.first_fetch
    )
    eop_by_m, eop_tot = _count_post_eop_mutations(world_log, roster_set)
    return TrialMetricsBundle(
        dup_by_member=dup_by_m, informed_dup_by_member=inf_dup_by_m, informed_dup_keys=inf_dup_keys,
        stale_total=stale_tot, stale_by_member=stale_by_m, shared_conflicts_total=sh_tot,
        shared_branches=sh_br, shared_files=sh_fi, proposed_collisions=prop_col,
        overwrites_by_member=ow_by_m, extra_overwrites_by_member=ex_ow_by_m, extra_proposed=ex_prop,
        collision_details=[*dup_det, *stale_det, *ow_det], excluded_mut_steps=excl_mut,
        m_posted=m_post, m_pairs=m_pair, m_fetched=m_fch, m_never=m_nev,
        m_in_ctx=m_ctx, m_acted=m_act, lags_by_member=lags_m, all_lags=all_lags,
        unique_fetched_ids=uniq_f, unread_actions_by_member=unr_acts_m,
        unread_total_by_member=unr_tot_m, action_unread_counts=act_unr_c,
        all_unread_counts=all_unr_c, unread_by_action=unr_by_act,
        post_eop_by_member=eop_by_m, post_eop_total=eop_tot,
    )


def _build_member_funnel_and_collisions(
    mid: str,
    member: MemberRecord,
    trial: NormalizedMultiAgentTrial,
    slices: TrialSlices,
    mb: TrialMetricsBundle,
) -> tuple[MessageFunnelMetrics, CollisionMetrics]:
    m_shared = (
        sum(1 for owners in mb.shared_branches.values() if mid in owners)
        + sum(1 for owners in mb.shared_files.values() if mid in owners)
        + sum(1 for b in mb.extra_proposed if mid in mb.proposed_collisions[b])
        + mb.extra_overwrites_by_member[mid]
    )
    m_lags = mb.lags_by_member.get(mid, [])
    m_unread_list = mb.action_unread_counts.get(mid, [])
    funnel = MessageFunnelMetrics(
        messages_posted=mb.m_posted[mid],
        messages_fetched=mb.m_fetched[mid],
        messages_in_context=mb.m_in_ctx[mid],
        messages_acted_on=mb.m_acted[mid],
        unread_at_action=mb.unread_actions_by_member[mid],
        never_fetched=mb.m_never[mid],
        message_delivery_pairs=mb.m_pairs[mid],
        unique_messages_fetched=mb.m_fetched[mid],
        unique_messages_never_fetched=mb.m_never[mid],
        messages_fetched_but_ignored=max(0, mb.m_fetched[mid] - mb.m_acted[mid]),
        unread_messages_at_action_total=mb.unread_total_by_member[mid],
        unread_at_action_mean=round(sum(m_unread_list) / len(m_unread_list), 6)
        if m_unread_list
        else None,
        fetch_calls=slices.fetch_calls_by_member[mid],
        fetch_lag_median_sec=round(median(m_lags), 3) if m_lags else None,
        context_observable=(
            member.trajectory_status == "present" and bool(trial.member_steps.get(mid))
        ),
    )
    collisions = CollisionMetrics(
        duplicate_target_actions=mb.dup_by_member[mid],
        stale_read_write_races=mb.stale_by_member.get(mid, 0),
        shared_branch_or_file_conflicts=m_shared,
        post_end_of_play_events=mb.post_eop_by_member[mid],
        informed_duplicate_actions=mb.informed_dup_by_member[mid],
        shared_branches={b: o for b, o in mb.shared_branches.items() if mid in o},
        shared_files={f: o for f, o in mb.shared_files.items() if mid in o},
        proposed_branch_collisions={b: o for b, o in mb.proposed_collisions.items() if mid in o},
        overwrite_events=mb.overwrites_by_member[mid],
    )
    return funnel, collisions


def _build_single_member_report(
    member: MemberRecord,
    trial: NormalizedMultiAgentTrial,
    slices: TrialSlices,
    mb: TrialMetricsBundle,
    reads_by_member: Counter[str],
    refused_by_member: Counter[str],
    probes_by_member: Counter[str],
    seat_rewards: dict[str, float | None],
    auth_flagged_members: set[str],
    m_logs_ok: bool,
    m_cost_ok: bool,
) -> MemberForensicsReport:
    mid = member.member_id
    funnel, collisions = _build_member_funnel_and_collisions(mid, member, trial, slices, mb)
    return MemberForensicsReport(
        member_id=mid, display_name=member.display_name, role=member.role,
        under_test=member.under_test, agent_name=member.agent_name, harness=member.harness,
        model_name=member.model_name, model_provider=member.model_provider,
        ran_model=member.ran_model, trajectory_status=member.trajectory_status,
        n_steps=member.n_steps, n_tool_calls=member.n_tool_calls,
        n_actions=len(slices.actions_by_member.get(mid, [])), n_reads=reads_by_member[mid],
        n_refused=refused_by_member[mid], n_probes=probes_by_member[mid],
        exception_type=member.exception_type, seat_reward=seat_rewards.get(mid),
        cost_usd=member.usage.cost_usd, cost_status=member.usage.cost_status,
        usage_mismatches=member.usage.usage_mismatches, funnel=funnel, collisions=collisions,
        logs_complete=m_logs_ok, cost_complete=m_cost_ok,
        unauthenticated_author_mismatch=mid in auth_flagged_members,
        linked_step_events=slices.linked_count_by_member[mid],
    )


def _finalize_integrity_flags(
    trial: NormalizedMultiAgentTrial,
    world_log: WorldEventLog,
    roster_set: set[str],
    comms_off: bool,
    all_members_logs_ok: bool,
    auth_mismatch_count: int,
    all_issues: list[str],
) -> IntegrityFlags:
    fatal_anomalies = any(
        a.startswith(
            (
                "result_json:",
                "missing_seat_result:",
                "undeclared_seat_dir:",
                "nonzero_seat_exit:",
                "invalid_seat_exit_code:",
                "stalled_seat_timeout:",
            )
        )
        for a in trial.anomalies
    )
    logs_complete = (
        bool(trial.members)
        and all_members_logs_ok
        and not fatal_anomalies
        and not world_log.parse_errors
    )
    seat_err_w_reward = bool(trial.rewards) and (
        bool(trial.trial_exception_type)
        or any(m.exception_type is not None or m.lost for m in trial.members)
    )
    if seat_err_w_reward:
        all_issues.append("seat_error_with_reward")
    net_unenforced, budget_unbounded, covert_git = evaluate_extra_integrity(
        trial, world_log, roster_set, comms_off, all_issues
    )
    return IntegrityFlags(
        logs_complete=logs_complete,
        cost_complete=trial.cost_complete,
        seat_error_with_reward=seat_err_w_reward,
        unauthenticated_author_mismatch=auth_mismatch_count > 0,
        event_log_present=world_log.event_log_present,
        trial_events_present=world_log.trial_events_present,
        git_bundle_present=world_log.git_bundle_present,
        unauthenticated_author_mismatch_count=auth_mismatch_count,
        reward_file_mismatch=trial.reward_file_mismatch,
        network_not_enforced=net_unenforced,
        budget_unbounded_subscription=budget_unbounded,
        covert_git_signaling_when_silent=covert_git,
        issues=sorted(set(all_issues)),
    )


def build_members_and_integrity(
    trial: NormalizedMultiAgentTrial,
    world_log: WorldEventLog,
    roster_set: set[str],
    slices: TrialSlices,
    mb: TrialMetricsBundle,
    comms_off: bool,
    seat_rewards: dict[str, float | None],
) -> tuple[dict[str, MemberForensicsReport], IntegrityFlags]:
    auth_mismatch_count, auth_flagged_members, auth_issues = compute_auth_mismatches(
        trial, world_log, roster_set
    )
    all_issues: list[str] = [
        *trial.anomalies, *trial.reward_problems, *world_log.parse_errors, *auth_issues,
    ]
    if trial.reward_file_mismatch:
        all_issues.append("reward_file_mismatch")
    reads_by_m = Counter(
        ev.actor
        for ev in slices.in_play_events
        if ev.kind == "world_read" and ev.accepted and ev.actor
    )
    refused_by_m = Counter(
        ev.actor for ev in slices.in_play_events if ev.kind == "policy_refusal" and ev.actor
    )
    probes_by_m = Counter(
        ev.actor for ev in slices.in_play_events if ev.kind == "probe" and ev.actor
    )
    member_reports: dict[str, MemberForensicsReport] = {}
    all_members_logs_ok = True
    for member in trial.members:
        mid = member.member_id
        m_logs_ok, m_log_issues = member_logs_complete(member, trial.ecosystem)
        if not m_logs_ok:
            all_members_logs_ok = False
            all_issues.extend(m_log_issues)
        m_cost_ok = member.usage.cost_status in ("recorded", "zero_no_model")
        if not m_cost_ok:
            all_issues.append(f"cost_{member.usage.cost_status}:{mid}")
        all_issues.extend(f"{mid}:{u}" for u in member.usage.usage_mismatches)
        member_reports[mid] = _build_single_member_report(
            member, trial, slices, mb, reads_by_m, refused_by_m, probes_by_m,
            seat_rewards, auth_flagged_members, m_logs_ok, m_cost_ok,
        )
    integrity = _finalize_integrity_flags(
        trial, world_log, roster_set, comms_off, all_members_logs_ok, auth_mismatch_count, all_issues
    )
    return member_reports, integrity


def build_trial_funnel_and_collisions(
    roster: list[str],
    roster_set: set[str],
    slices: TrialSlices,
    mb: TrialMetricsBundle,
    member_reports: dict[str, MemberForensicsReport],
) -> tuple[MessageFunnelMetrics, CollisionMetrics]:
    total_fetched = sum(mb.m_fetched.values())
    total_acted = sum(mb.m_acted.values())
    unique_deliverable_ids = {
        (log_group(p.source_format), p.msg_id)
        for p in slices.peer_posts
        if p.msg_id is not None
        and (
            any(r in roster_set and r != p.actor for r in p.recipients)
            if p.recipients
            else any(r != p.actor for r in roster)
        )
    }
    funnel = MessageFunnelMetrics(
        messages_posted=len(slices.peer_posts),
        messages_fetched=total_fetched,
        messages_in_context=sum(mb.m_in_ctx.values()),
        messages_acted_on=total_acted,
        unread_at_action=sum(mb.unread_actions_by_member.values()),
        never_fetched=sum(mb.m_never.values()),
        message_delivery_pairs=sum(mb.m_pairs.values()),
        unique_messages_fetched=len(mb.unique_fetched_ids),
        unique_messages_never_fetched=len(unique_deliverable_ids - mb.unique_fetched_ids),
        messages_fetched_but_ignored=max(0, total_fetched - total_acted),
        unread_messages_at_action_total=sum(mb.unread_total_by_member.values()),
        unread_at_action_mean=round(sum(mb.all_unread_counts) / len(mb.all_unread_counts), 6)
        if mb.all_unread_counts
        else None,
        fetch_calls=sum(slices.fetch_calls_by_member.values()),
        fetch_lag_median_sec=round(median(mb.all_lags), 3) if mb.all_lags else None,
        context_observable=bool(member_reports)
        and all(m.funnel.context_observable for m in member_reports.values()),
    )
    collisions = CollisionMetrics(
        duplicate_target_actions=sum(mb.dup_by_member.values()),
        stale_read_write_races=mb.stale_total,
        shared_branch_or_file_conflicts=mb.shared_conflicts_total,
        post_end_of_play_events=mb.post_eop_total,
        informed_duplicate_actions=sum(mb.informed_dup_by_member.values()),
        shared_branches=mb.shared_branches,
        shared_files=mb.shared_files,
        proposed_branch_collisions=mb.proposed_collisions,
        overwrite_events=sum(mb.overwrites_by_member.values()),
        collision_details=mb.collision_details[:100],
    )
    return funnel, collisions
