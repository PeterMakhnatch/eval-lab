from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from ._forensics_metrics import actor_read_file_after
from ._forensics_rules import happened_before, is_duplicate_eligible_action, normalize_repo_path
from ._forensics_sweep import log_group
from ._herding_models import (
    BatchHerdingSummary,
    HerdingMetrics,
    aggregate_herding_summary,
    compute_mixed_motive_metrics,
)
from .world_events import NormalizedWorldEvent

__all__ = [
    "BatchHerdingSummary",
    "HerdingMetrics",
    "aggregate_herding_summary",
    "compute_herding_metrics",
]
_BATCH_WINDOW_SEC = 2.0


def _build_known_boxes_before(
    in_play_events: list[NormalizedWorldEvent], roster_set: set[str]
) -> dict[tuple[str, int], frozenset[int]]:
    known: dict[str, set[int]] = defaultdict(set)
    before: dict[tuple[str, int], frozenset[int]] = {}
    for ev in sorted(in_play_events, key=lambda e: (log_group(e.source_format), e.seq)):
        if not ev.accepted or ev.actor not in roster_set:
            continue
        assert ev.actor is not None
        before[(ev.source_format, ev.seq)] = frozenset(known[ev.actor])
        if ev.raw_kind == "open" and type(ev.detail.get("box")) is int:
            known[ev.actor].add(ev.detail["box"])
        for k in ("team_open_boxes", "own_open_boxes"):
            if isinstance(ev.detail.get(k), list):
                known[ev.actor].update(b for b in ev.detail[k] if type(b) is int)
    return before


def _build_read_maps(
    peer_posts: list[NormalizedWorldEvent],
    in_play_events: list[NormalizedWorldEvent],
    timed_reads: list[tuple[float, str, str]],
) -> tuple[
    dict[str, list[NormalizedWorldEvent]],
    dict[tuple[str, str], list[NormalizedWorldEvent]],
    dict[tuple[str, str], list[float]],
]:
    posts_by_actor: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
    for post in peer_posts:
        if post.actor is not None and post.msg_id is not None:
            posts_by_actor[post.actor].append(post)
    file_reads: dict[tuple[str, str], list[NormalizedWorldEvent]] = defaultdict(list)
    for ev in in_play_events:
        if ev.kind == "world_read" and ev.accepted and ev.actor is not None:
            for fpath in ev.files:
                file_reads[(ev.actor, normalize_repo_path(fpath))].append(ev)
    atif_reads: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ts_ep, actor, fpath in timed_reads:
        atif_reads[(actor, normalize_repo_path(fpath))].append(ts_ep)
    return posts_by_actor, file_reads, atif_reads


def _check_artifact_handoff(
    act: NormalizedWorldEvent,
    last_file_writer: dict[str, NormalizedWorldEvent],
    clean_file_owner: dict[str, str | None],
    file_reads: dict[tuple[str, str], list[NormalizedWorldEvent]],
    atif_reads: dict[tuple[str, str], list[float]],
) -> bool:
    if act.kind != "artifact_write" or not act.files or act.actor is None:
        return False
    explicit_ow = bool(act.overwrites_actor and act.overwrites_actor != act.actor)
    peer_touched = any_self_clean = False
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
            file_clean = actor_read_file_after(act.actor, fpath, prev, act, file_reads, atif_reads)
        all_clean = all_clean and file_clean
        clean_file_owner[fpath] = act.actor if file_clean else None
        last_file_writer[fpath] = act
    return all_clean and (peer_touched or any_self_clean)


def _classify_cross_seat_dup(
    act: NormalizedWorldEvent,
    first: NormalizedWorldEvent,
    actor_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    known_before: dict[tuple[str, int], frozenset[int]],
) -> str:
    assert act.actor is not None
    act_box = act.detail.get("box")
    if (
        act.raw_kind == "open"
        and type(act_box) is int
        and act_box in known_before.get((act.source_format, act.seq), ())
    ):
        return "informed_state_duplicate_actions"
    if any(
        happened_before(first, post)
        and post.msg_id is not None
        and (f_ev := first_fetch.get((act.actor, log_group(post.source_format), post.msg_id)))
        is not None
        and happened_before(f_ev, act, inclusive=True)
        for post in actor_posts
    ):
        return "informed_msg_duplicate_actions"
    if any(
        happened_before(first, post)
        and happened_before(post, act)
        and (not post.recipients or act.actor in post.recipients)
        for post in actor_posts
    ):
        return "inflight_duplicate_actions"
    return "unreported_duplicate_actions"


def _partition_duplicates(
    actions: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    in_play_events: list[NormalizedWorldEvent],
    timed_reads: list[tuple[float, str, str]],
    known_before: dict[tuple[str, int], frozenset[int]],
    expected_dup_total: int,
) -> dict[str, Any]:
    posts_by_actor, file_reads, atif_reads = _build_read_maps(
        peer_posts, in_play_events, timed_reads
    )
    first_act_by_sig: dict[str, NormalizedWorldEvent] = {}
    last_file_writer: dict[str, NormalizedWorldEvent] = {}
    clean_file_owner: dict[str, str | None] = {}
    buckets: Counter[str] = Counter()
    self_repeats = 0

    for act in actions:
        if act.actor is None:
            continue
        handoff = _check_artifact_handoff(
            act, last_file_writer, clean_file_owner, file_reads, atif_reads
        )
        if not is_duplicate_eligible_action(act):
            continue
        sig = act.action_signature or f"{act.raw_kind}:{act.target or ''}"
        first = first_act_by_sig.get(sig)
        if first is None:
            first_act_by_sig[sig] = act
            continue
        if handoff:
            if first.actor != act.actor:
                first_act_by_sig[sig] = act
            continue
        if first.actor == act.actor:
            self_repeats += 1
            continue
        assert first.actor is not None
        key = _classify_cross_seat_dup(
            act, first, posts_by_actor.get(first.actor, []), first_fetch, known_before
        )
        buckets[key] += 1
    dup_keys = (
        "unreported_duplicate_actions",
        "inflight_duplicate_actions",
        "informed_msg_duplicate_actions",
        "informed_state_duplicate_actions",
    )
    assert sum(buckets[k] for k in dup_keys) == expected_dup_total
    return {**{k: buckets[k] for k in dup_keys}, "self_repeat_actions": self_repeats}


def _action_target_keys(act: NormalizedWorldEvent) -> list[str]:
    if act.kind == "artifact_write" and act.files:
        return list(dict.fromkeys(normalize_repo_path(f) for f in act.files))
    return [act.action_signature or f"{act.raw_kind}:{act.target or ''}"]


def _compute_symmetry_and_overlap(
    roster: list[str],
    actions_by_member: dict[str, list[NormalizedWorldEvent]],
) -> dict[str, Any]:
    sorted_roster = sorted(roster)
    counts = [len(actions_by_member.get(m, [])) for m in sorted_roster]
    s1, s2 = sum(counts), sum(c * c for c in counts)
    jain = (
        round(min(1.0, max(0.0, (s1 * s1) / (len(sorted_roster) * s2))), 6)
        if sorted_roster and s2 > 0
        else None
    )
    first_keys = [
        ",".join(_action_target_keys(acts[0]))
        for m in sorted_roster
        if (acts := actions_by_member.get(m))
    ]
    if len(first_keys) >= 2:
        fk_counts = Counter(first_keys)
        identical_first = len(fk_counts) == 1
        collision_seats = sum(c for c in fk_counts.values() if c > 1)
        m_n = len(first_keys)
        h_val = -math.fsum((c / m_n) * math.log(c / m_n) for c in fk_counts.values() if c > 0)
        first_entropy = round(min(1.0, max(0.0, h_val / math.log(m_n))), 6)
    else:
        identical_first, collision_seats, first_entropy = False, 0, None
    r_sets = [
        {t for act in actions_by_member.get(m, []) for t in _action_target_keys(act)}
        for m in sorted_roster
    ]
    sum_ri = sum(len(r) for r in r_sets)
    union_r: set[str] = set().union(*r_sets) if r_sets else set()
    return {
        "identical_first_action": identical_first,
        "first_action_collision_seats": collision_seats,
        "first_target_entropy": first_entropy,
        "target_overlap_ratio": round(max(0.0, (sum_ri - len(union_r)) / sum_ri), 6)
        if sum_ri > 0
        else None,
        "action_evenness_jain": jain,
        "never_acted_members": [m for m in sorted_roster if not actions_by_member.get(m)],
    }


def _compute_obvious_actions(
    in_play_events: list[NormalizedWorldEvent],
    all_in_play_actions: list[NormalizedWorldEvent],
    roster_set: set[str],
    known_before: dict[tuple[str, int], frozenset[int]],
) -> dict[str, Any]:
    empty = {
        "obvious_action_count": 0,
        "obvious_eligible_actions": 0,
        "obvious_action_share": None,
        "repeat_obvious_actions": 0,
        "repeat_open_actions": 0,
        "repeat_obvious_share": None,
    }
    for ev in in_play_events:
        bx, rm = ev.detail.get("box"), ev.detail.get("room")
        if (
            ev.accepted
            and type(bx) is int
            and type(rm) is int
            and (bx < 1 or rm < 1 or not ((rm - 1) * 10 + 1 <= bx <= rm * 10))
        ):
            return empty
    box_opens = [
        a
        for a in all_in_play_actions
        if a.raw_kind == "open"
        and a.actor in roster_set
        and type(a.detail.get("box")) is int
        and type(a.detail.get("room")) is int
    ]
    if not box_opens:
        return empty
    first_actor_by_box: dict[int, str] = {}
    obv_cnt = rep_opens = rep_obv = 0
    for act in box_opens:
        assert act.actor is not None
        bx, rm = act.detail["box"], act.detail["room"]
        kb = known_before.get((act.source_format, act.seq), frozenset())
        lo = (rm - 1) * 10 + 1
        is_obv = bx == next((b for b in range(lo, lo + 10) if b not in kb), -1)
        obv_cnt += int(is_obv)
        if first_actor_by_box.setdefault(bx, act.actor) != act.actor:
            rep_opens, rep_obv = rep_opens + 1, rep_obv + int(is_obv)
    return {
        "obvious_action_count": obv_cnt,
        "obvious_eligible_actions": len(box_opens),
        "obvious_action_share": round(obv_cnt / len(box_opens), 6),
        "repeat_obvious_actions": rep_obv,
        "repeat_open_actions": rep_opens,
        "repeat_obvious_share": round(rep_obv / rep_opens, 6) if rep_opens > 0 else None,
    }


def _compute_batching(
    roster: list[str],
    actions_by_member: dict[str, list[NormalizedWorldEvent]],
    event_to_step: dict[tuple[str, int], int],
) -> dict[str, Any]:
    batch_sizes: list[int] = []
    total_actions = 0
    step_counts: Counter[tuple[str, int]] = Counter()
    for mid in sorted(roster):
        acts = actions_by_member.get(mid, [])
        if not acts:
            continue
        cur_size = 0
        prev_ts: float | None = None
        for act in acts:
            total_actions += 1
            if (sid := event_to_step.get((act.source_format, act.seq))) is not None:
                step_counts[(mid, sid)] += 1
            if cur_size == 0:
                cur_size = 1
            elif (
                prev_ts is not None
                and act.ts_epoch is not None
                and abs(act.ts_epoch - prev_ts) < _BATCH_WINDOW_SEC
            ):
                cur_size += 1
            else:
                batch_sizes.append(cur_size)
                cur_size = 1
            prev_ts = act.ts_epoch
        if cur_size > 0:
            batch_sizes.append(cur_size)
    n_batches = len(batch_sizes)
    return {
        "action_batch_count": n_batches,
        "actions_per_batch_mean": round(total_actions / n_batches, 6) if n_batches > 0 else None,
        "max_batch_size": max(batch_sizes) if batch_sizes else 0,
        "batched_actions": sum(s for s in batch_sizes if s >= 2),
        "same_step_batched_actions": sum(c for c in step_counts.values() if c >= 2),
    }


def _compute_protocol_automaton(
    roster_set: set[str],
    in_play_events: list[NormalizedWorldEvent],
    all_in_play_actions: list[NormalizedWorldEvent],
    comms_disabled: bool,
) -> dict[str, Any]:
    action_ids = {id(act) for act in all_in_play_actions}
    git_acts = [act for act in all_in_play_actions if act.source_format == "bespoke_git_bundle"]
    stream = [
        ev
        for ev in (*in_play_events, *git_acts)
        if ev.accepted
        and ev.actor in roster_set
        and (
            id(ev) in action_ids
            or ev.kind in ("message_post", "message_fetch")
            or (ev.kind == "lifecycle" and bool(ev.fetched_msg_ids))
            or (ev.kind == "world_read" and ev.raw_kind in ("claims", "pledges"))
        )
    ]
    groups = {log_group(ev.source_format) for ev in stream}
    if len(groups) == 1 and next(iter(groups)) != "bespoke_git_bundle":
        stream.sort(key=lambda ev: (ev.seq, ev.source_format))
    elif stream:
        stream.sort(
            key=lambda ev: (
                0 if ev.ts_epoch is not None else 1,
                ev.ts_epoch if ev.ts_epoch is not None else 0.0,
                1 if ev.source_format == "bespoke_git_bundle" else 0,
                log_group(ev.source_format),
                ev.seq,
            )
        )
    dfa_state: dict[str, int] = defaultdict(int)
    has_post: dict[str, bool] = defaultdict(bool)
    has_read: dict[str, bool] = defaultdict(bool)
    ever_fetched: dict[str, bool] = defaultdict(bool)
    total_acts = post_and_read = post_then_read = bef_fetch = 0
    for ev in stream:
        assert ev.actor is not None
        actor = ev.actor
        if id(ev) in action_ids:
            total_acts += 1
            if not comms_disabled and len(roster_set) >= 2 and not ever_fetched[actor]:
                bef_fetch += 1
            post_and_read += int(has_post[actor] and has_read[actor])
            post_then_read += int(dfa_state[actor] == 2)
            dfa_state[actor], has_post[actor], has_read[actor] = 0, False, False
        elif ev.kind == "message_post":
            has_post[actor] = True
            if dfa_state[actor] == 0:
                dfa_state[actor] = 1
        else:
            has_read[actor] = ever_fetched[actor] = True
            if dfa_state[actor] == 1:
                dfa_state[actor] = 2
    return {
        "post_and_read_actions": post_and_read,
        "post_then_read_actions": post_then_read,
        "post_read_share": round(post_and_read / total_acts, 6) if total_acts > 0 else None,
        "post_then_read_share": round(post_then_read / total_acts, 6) if total_acts > 0 else None,
        "actions_before_any_fetch": bef_fetch,
    }


def _compute_topology_metrics(
    roster: list[str],
    roster_set: set[str],
    peer_posts: list[NormalizedWorldEvent],
) -> dict[str, Any]:
    pub_posts = dir_posts = 0
    dm_inbox: Counter[str] = Counter()
    dm_senders: dict[str, set[str]] = defaultdict(set)
    for post in peer_posts:
        assert post.actor is not None
        if post.visibility in ("direct", "multi") or bool(post.recipients):
            dir_posts += 1
            for recip in dict.fromkeys(post.recipients):
                if recip in roster_set and recip != post.actor:
                    dm_inbox[recip] += 1
                    dm_senders[recip].add(post.actor)
        else:
            pub_posts += 1
    tot_msgs = pub_posts + dir_posts
    tot_dm = sum(dm_inbox.values())
    n_seats = len(roster)
    dm_cent: float | None = None
    if n_seats >= 2:
        in_degs = [len(dm_senders.get(m, ())) for m in roster]
        d_max = max(in_degs)
        denom = (n_seats - 1) * (n_seats - 1)
        dm_cent = round(min(1.0, max(0.0, sum(d_max - d for d in in_degs) / denom)), 6)
    return {
        "public_board_posts": pub_posts,
        "total_peer_posts": tot_msgs,
        "public_board_share": round(pub_posts / tot_msgs, 6) if tot_msgs > 0 else None,
        "max_dm_inbox_share": round(max(dm_inbox.values()) / tot_dm, 6) if tot_dm > 0 else None,
        "dm_in_degree_centralization": dm_cent,
    }


def _count_statement_churn(ordered: list[NormalizedWorldEvent], roster_set: set[str]) -> int:
    last_stmt: dict[tuple[str, str], Any] = {}
    churn = 0
    for ev in ordered:
        if ev.actor not in roster_set:
            continue
        assert ev.actor is not None
        val: Any = None
        if ev.raw_kind == "claim" and ev.accepted and type(ev.detail.get("claimed_need")) is int:
            val = ev.detail["claimed_need"]
        elif (
            ev.raw_kind == "pledge" and ev.accepted and type(ev.detail.get("pledged_amount")) is int
        ):
            val = ev.detail["pledged_amount"]
        elif (
            ev.raw_kind == "answer"
            and (ev.accepted or ev.detail.get("already_answered") is True)
            and isinstance(ev.detail.get("answer_totals"), dict)
            and ev.detail["answer_totals"]
        ):
            val = ev.detail["answer_totals"]
        elif ev.raw_kind in ("vote", "accuse") and ev.accepted and ev.target:
            val = ev.target
        if val is not None:
            skey = (ev.actor, ev.raw_kind)
            if skey in last_stmt and last_stmt[skey] != val:
                churn += 1
            last_stmt[skey] = val
    return churn


def _compute_answer_churn(
    roster_set: set[str],
    in_play_events: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
) -> dict[str, Any]:
    ordered = sorted(in_play_events, key=lambda ev: (log_group(ev.source_format), ev.seq))
    late_refusals = sum(
        1
        for ev in ordered
        if ev.raw_kind == "answer"
        and ev.actor in roster_set
        and ev.detail.get("already_answered") is True
    )
    proposals = [
        (ev.accepted, tot)
        for ev in ordered
        if ev.raw_kind == "answer"
        and ev.actor in roster_set
        and (ev.accepted or ev.detail.get("already_answered") is True)
        and isinstance((tot := ev.detail.get("answer_totals")), dict)
        and tot
    ]
    conflicting = 0
    if proposals:
        base_idx = next((i for i, (acc, _) in enumerate(proposals) if acc), 0)
        base_tot = proposals[base_idx][1]
        conflicting = sum(
            1 for i, (_, tot) in enumerate(proposals) if i != base_idx and tot != base_tot
        )
    first_ans = next(
        (
            ev
            for ev in ordered
            if ev.raw_kind == "answer" and ev.accepted and ev.actor in roster_set
        ),
        None,
    )
    premature = False
    if first_ans is not None and len(roster_set) >= 2:
        assert first_ans.actor is not None
        seen_peers = {
            p.actor
            for p in peer_posts
            if p.actor in roster_set
            and p.actor != first_ans.actor
            and p.msg_id is not None
            and (fev := first_fetch.get((first_ans.actor, log_group(p.source_format), p.msg_id)))
            is not None
            and happened_before(fev, first_ans, inclusive=True)
        }
        premature = len(seen_peers) < len(roster_set) - 1
    return {
        "premature_commit": premature,
        "late_answer_refusals": late_refusals,
        "conflicting_answer_proposals": conflicting,
        "statement_churn_count": _count_statement_churn(ordered, roster_set),
    }


def compute_herding_metrics(
    *,
    roster: list[str],
    roster_set: set[str],
    in_play_events: list[NormalizedWorldEvent],
    all_in_play_actions: list[NormalizedWorldEvent],
    actions_by_member: dict[str, list[NormalizedWorldEvent]],
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    event_to_step: dict[tuple[str, int], int],
    timed_reads: list[tuple[float, str, str]],
    comms_disabled: bool,
    seat_rewards: dict[str, float | None],
    duplicate_target_actions: int,
) -> HerdingMetrics:
    kb = _build_known_boxes_before(in_play_events, roster_set)
    dup_d = _partition_duplicates(
        all_in_play_actions,
        peer_posts,
        first_fetch,
        in_play_events,
        timed_reads,
        kb,
        duplicate_target_actions,
    )
    fields: dict[str, Any] = {
        **dup_d,
        **_compute_symmetry_and_overlap(roster, actions_by_member),
        **_compute_obvious_actions(in_play_events, all_in_play_actions, roster_set, kb),
        **_compute_batching(roster, actions_by_member, event_to_step),
        **_compute_protocol_automaton(
            roster_set, in_play_events, all_in_play_actions, comms_disabled
        ),
        **_compute_topology_metrics(roster, roster_set, peer_posts),
        **_compute_answer_churn(roster_set, in_play_events, peer_posts, first_fetch),
        **compute_mixed_motive_metrics(roster, roster_set, in_play_events, seat_rewards),
    }
    return HerdingMetrics(**fields)
