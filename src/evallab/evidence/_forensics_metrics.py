from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ._forensics_metrics_conflicts import (
    BRANCH_RE,
    BRANCH_STOPWORDS,
    compute_shared_conflicts,
    compute_stale_races,
)
from ._forensics_rules import (
    NON_DUPLICABLE_ACTIONS,
    SYSTEM_ACTORS,
    compute_auth_mismatches,
    happened_before,
    is_author_spoof_event,
    is_duplicate_eligible_action,
    is_post_eop_mutation,
    member_logs_complete,
    normalize_repo_path,
    read_targets,
    same_log,
    trial_comms_disabled,
    write_targets,
)
from ._forensics_sweep import (
    build_action_suffix_index,
    compute_last_mut_steps,
    has_downstream_action,
    log_group,
    sweep_unread_for_actor,
)
from ._multiagent_io import NormalizedAtifStep
from .multiagent_adapter import NormalizedMultiAgentTrial
from .world_events import NormalizedWorldEvent

__all__ = [
    "BRANCH_RE",
    "BRANCH_STOPWORDS",
    "NON_DUPLICABLE_ACTIONS",
    "SYSTEM_ACTORS",
    "actor_read_file_after",
    "compute_auth_mismatches",
    "compute_duplicate_actions",
    "compute_funnel_counts",
    "compute_shared_conflicts",
    "compute_stale_races",
    "compute_unread_at_action",
    "happened_before",
    "is_author_spoof_event",
    "is_duplicate_eligible_action",
    "is_post_eop_mutation",
    "log_group",
    "member_logs_complete",
    "read_targets",
    "same_log",
    "trial_comms_disabled",
    "write_targets",
]


def actor_read_file_after(
    actor: str,
    fpath: str,
    prev: NormalizedWorldEvent,
    act: NormalizedWorldEvent,
    file_reads_by_actor: dict[tuple[str, str], list[NormalizedWorldEvent]],
    atif_reads_by_actor: dict[tuple[str, str], list[float]],
) -> bool:
    for r_ev in file_reads_by_actor.get((actor, fpath), []):
        if happened_before(prev, r_ev) and happened_before(r_ev, act, inclusive=True):
            return True
    if prev.ts_epoch is not None and act.ts_epoch is not None:
        hi = act.ts_epoch + (1.0 if act.source_format == "bespoke_git_bundle" else 0.0)
        for r_ts in atif_reads_by_actor.get((actor, fpath), []):
            if prev.ts_epoch < r_ts <= hi:
                return True
    return False


def _update_artifact_handoff(
    act: NormalizedWorldEvent,
    last_file_writer: dict[str, NormalizedWorldEvent],
    clean_file_owner: dict[str, str | None],
    file_reads_by_actor: dict[tuple[str, str], list[NormalizedWorldEvent]],
    atif_reads_by_actor: dict[tuple[str, str], list[float]],
) -> bool:
    assert act.actor is not None
    explicit_ow = bool(act.overwrites_actor and act.overwrites_actor != act.actor)
    peer_touched = False
    any_self_clean = False
    all_clean = not explicit_ow
    for fpath in dict.fromkeys(normalize_repo_path(raw_f) for raw_f in act.files):
        prev = last_file_writer.get(fpath)
        if explicit_ow:
            file_clean = False
        elif prev is None:
            file_clean = True
        elif prev.actor == act.actor:
            file_clean = clean_file_owner.get(fpath) == act.actor
            any_self_clean = any_self_clean or file_clean
        else:
            peer_touched = True
            file_clean = actor_read_file_after(
                act.actor, fpath, prev, act, file_reads_by_actor, atif_reads_by_actor
            )
        if not file_clean:
            all_clean = False
        clean_file_owner[fpath] = act.actor if file_clean else None
        last_file_writer[fpath] = act
    return all_clean and (peer_touched or any_self_clean)


def _prepare_dup_read_maps(
    peer_posts: list[NormalizedWorldEvent],
    in_play_events: list[NormalizedWorldEvent] | None,
    timed_reads: list[tuple[float, str, str]] | None,
) -> tuple[
    dict[str, list[NormalizedWorldEvent]],
    dict[tuple[str, str], list[NormalizedWorldEvent]],
    dict[tuple[str, str], list[float]],
]:
    posts_by_actor: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
    for post in peer_posts:
        if post.actor is not None and post.msg_id is not None:
            posts_by_actor[post.actor].append(post)
    file_reads_by_actor: dict[tuple[str, str], list[NormalizedWorldEvent]] = defaultdict(list)
    for ev in in_play_events or ():
        if ev.kind == "world_read" and ev.accepted and ev.actor is not None:
            for fpath in ev.files:
                file_reads_by_actor[(ev.actor, normalize_repo_path(fpath))].append(ev)
    atif_reads_by_actor: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ts_ep, actor, fpath in timed_reads or ():
        atif_reads_by_actor[(actor, normalize_repo_path(fpath))].append(ts_ep)
    return posts_by_actor, file_reads_by_actor, atif_reads_by_actor


def compute_duplicate_actions(
    actions: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    *,
    in_play_events: list[NormalizedWorldEvent] | None = None,
    timed_reads: list[tuple[float, str, str]] | None = None,
) -> tuple[Counter[str], Counter[str], set[tuple[str, int]], list[dict[str, Any]]]:
    first_act_by_sig: dict[str, NormalizedWorldEvent] = {}
    last_file_writer: dict[str, NormalizedWorldEvent] = {}
    clean_file_owner: dict[str, str | None] = {}
    dup_by_member: Counter[str] = Counter()
    informed_dup_by_member: Counter[str] = Counter()
    informed_dup_keys: set[tuple[str, int]] = set()
    details: list[dict[str, Any]] = []
    posts_by_actor, file_reads_by_actor, atif_reads_by_actor = _prepare_dup_read_maps(
        peer_posts, in_play_events, timed_reads
    )
    for act in actions:
        if act.actor is None:
            continue
        coordinated_handoff = (
            _update_artifact_handoff(
                act, last_file_writer, clean_file_owner, file_reads_by_actor, atif_reads_by_actor
            )
            if act.kind == "artifact_write" and act.files
            else False
        )
        if not is_duplicate_eligible_action(act):
            continue
        sig = act.action_signature or f"{act.raw_kind}:{act.target or ''}"
        first = first_act_by_sig.setdefault(sig, act)
        if coordinated_handoff:
            first_act_by_sig[sig] = act if first.actor != act.actor else first
            continue
        if first.actor == act.actor:
            continue
        dup_by_member[act.actor] += 1
        assert first.actor is not None
        is_informed = any(
            happened_before(first, post)
            and post.msg_id is not None
            and (f_ev := first_fetch.get((act.actor, log_group(post.source_format), post.msg_id)))
            is not None
            and happened_before(f_ev, act, inclusive=True)
            for post in posts_by_actor.get(first.actor, ())
        )
        if is_informed:
            informed_dup_by_member[act.actor] += 1
            informed_dup_keys.add((act.source_format, act.seq))
        details.append(
            {
                "kind": "duplicate_target_action",
                "actor": act.actor,
                "first_actor": first.actor,
                "signature": sig,
                "informed": is_informed,
                "seq": act.seq,
            }
        )
    return dup_by_member, informed_dup_by_member, informed_dup_keys, details


def compute_unread_at_action(
    actions: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
) -> tuple[
    Counter[str],
    Counter[str],
    dict[str, list[int]],
    list[int],
    dict[int, list[NormalizedWorldEvent]],
]:
    unread_actions: Counter[str] = Counter()
    unread_total: Counter[str] = Counter()
    by_member: dict[str, list[int]] = defaultdict(list)
    all_counts: list[int] = []
    unread_by_action: dict[int, list[NormalizedWorldEvent]] = {}
    if not actions:
        return unread_actions, unread_total, by_member, all_counts, unread_by_action

    if peer_posts:
        indexed_posts = list(enumerate(peer_posts))
        acts_by_actor: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
        for act in actions:
            if act.actor is not None:
                acts_by_actor[act.actor].append(act)
        for actor, actor_acts in acts_by_actor.items():
            unread_by_action.update(
                sweep_unread_for_actor(actor, actor_acts, indexed_posts, first_fetch)
            )

    for act in actions:
        if act.actor is None:
            continue
        u_list = unread_by_action.get(id(act))
        unread = len(u_list) if u_list is not None else 0
        by_member[act.actor].append(unread)
        all_counts.append(unread)
        if unread > 0:
            unread_actions[act.actor] += 1
            unread_total[act.actor] += unread
    return unread_actions, unread_total, by_member, all_counts, unread_by_action


def _recipient_acted_in_funnel(
    recip: str,
    fetch_ev: NormalizedWorldEvent,
    in_ctx_step: int | None,
    has_atif: bool,
    same_idx: dict[tuple[str, str], tuple[list[int], list[int | None]]],
    cross_idx: dict[str, dict[str, tuple[list[float], list[int | None]]]],
    last_mut_step: dict[str, int],
) -> bool:
    if not has_atif:
        return has_downstream_action(recip, fetch_ev, same_idx, cross_idx)
    if in_ctx_step is None:
        return False
    return (
        has_downstream_action(
            recip,
            fetch_ev,
            same_idx,
            cross_idx,
            min_step_gt=in_ctx_step,
            allow_same_step_same_log=True,
        )
        or last_mut_step.get(recip, -1) > in_ctx_step
    )


FunnelCountsResult = tuple[
    Counter[str],
    Counter[str],
    Counter[str],
    Counter[str],
    Counter[str],
    Counter[str],
    dict[str, list[float]],
    list[float],
    set[tuple[str, str]],
]


def compute_funnel_counts(
    peer_posts: list[NormalizedWorldEvent],
    roster: list[str],
    roster_set: set[str],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    in_context_steps: dict[tuple[str, str, str], int],
    actions_by_member: dict[str, list[NormalizedWorldEvent]],
    informed_dup_keys: set[tuple[str, int]],
    event_to_step: dict[tuple[str, int], int],
    trial: NormalizedMultiAgentTrial,
    *,
    observable_steps: dict[str, list[NormalizedAtifStep]] | None = None,
    excluded_mut_steps: dict[str, set[int]] | None = None,
) -> FunnelCountsResult:
    m_posted: Counter[str] = Counter(ev.actor for ev in peer_posts if ev.actor is not None)
    m_pairs, m_fetched, m_never = Counter[str](), Counter[str](), Counter[str]()
    m_in_ctx, m_acted = Counter[str](), Counter[str]()
    lags_by_member: dict[str, list[float]] = defaultdict(list)
    all_lags: list[float] = []
    unique_fetched_ids: set[tuple[str, str]] = set()
    member_by_id = {m.member_id: m for m in trial.members}
    obs_steps = observable_steps if observable_steps is not None else trial.member_steps
    last_mut_step = compute_last_mut_steps(obs_steps, excluded_mut_steps)
    same_idx, cross_idx = build_action_suffix_index(
        actions_by_member, informed_dup_keys, event_to_step
    )
    for post in peer_posts:
        assert post.actor is not None and post.msg_id is not None
        p_grp = log_group(post.source_format)
        eligible = (
            sorted({r for r in post.recipients if r in roster_set and r != post.actor})
            if post.recipients
            else [r for r in roster if r != post.actor]
        )
        for recip in eligible:
            m_pairs[recip] += 1
            fetch_ev = first_fetch.get((recip, p_grp, post.msg_id))
            if fetch_ev is None:
                m_never[recip] += 1
                continue
            m_fetched[recip] += 1
            unique_fetched_ids.add((p_grp, post.msg_id))
            if post.ts_epoch is not None and fetch_ev.ts_epoch is not None:
                lag = max(0.0, fetch_ev.ts_epoch - post.ts_epoch)
                lags_by_member[recip].append(lag)
                all_lags.append(lag)
            in_ctx_step = in_context_steps.get((recip, p_grp, post.msg_id))
            if in_ctx_step is not None:
                m_in_ctx[recip] += 1
            has_atif = member_by_id[recip].trajectory_status == "present" and bool(
                obs_steps.get(recip)
            )
            if _recipient_acted_in_funnel(
                recip, fetch_ev, in_ctx_step, has_atif, same_idx, cross_idx, last_mut_step
            ):
                m_acted[recip] += 1
    return (
        m_posted, m_pairs, m_fetched, m_never, m_in_ctx, m_acted,
        lags_by_member, all_lags, unique_fetched_ids,
    )
