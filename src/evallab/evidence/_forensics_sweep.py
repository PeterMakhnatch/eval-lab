from __future__ import annotations

import heapq
from bisect import bisect_right
from collections import defaultdict

from ._forensics_rules import happened_before, is_mutating_atif_step
from ._multiagent_io import NormalizedAtifStep
from .world_events import NormalizedWorldEvent

_BOARD_FORMATS = frozenset({"bespoke_board_v2", "bespoke_board_legacy", "bespoke_public_board"})


def log_group(source_format: str) -> str:
    return "bespoke_board" if source_format in _BOARD_FORMATS else source_format


def build_action_suffix_index(
    actions_by_member: dict[str, list[NormalizedWorldEvent]],
    informed_dup_keys: set[tuple[str, int]],
    event_to_step: dict[tuple[str, int], int],
) -> tuple[
    dict[tuple[str, str], tuple[list[int], list[int | None]]],
    dict[str, dict[str, tuple[list[float], list[int | None]]]],
]:
    same_idx: dict[tuple[str, str], tuple[list[int], list[int | None]]] = {}
    cross_idx: dict[str, dict[str, tuple[list[float], list[int | None]]]] = {}

    def _suffix_max_steps(steps: list[int | None]) -> list[int | None]:
        out: list[int | None] = [None] * len(steps)
        has_cur = False
        cur: int | None = None
        for i in range(len(steps) - 1, -1, -1):
            st = steps[i]
            if not has_cur:
                cur = st
                has_cur = True
            elif cur is None or st is None:
                cur = None
            elif st > cur:
                cur = st
            out[i] = cur
        return out

    for recip, acts in actions_by_member.items():
        elig = [a for a in acts if (a.source_format, a.seq) not in informed_dup_keys]
        by_grp: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
        for a in elig:
            by_grp[log_group(a.source_format)].append(a)
        recip_cross: dict[str, tuple[list[float], list[int | None]]] = {}
        for grp, glist in by_grp.items():
            by_seq = sorted(glist, key=lambda a: a.seq)
            same_idx[(recip, grp)] = (
                [a.seq for a in by_seq],
                _suffix_max_steps([event_to_step.get((a.source_format, a.seq)) for a in by_seq]),
            )
            by_ts = sorted(
                (a for a in glist if a.ts_epoch is not None),
                key=lambda a: a.ts_epoch or 0.0,
            )
            if by_ts:
                recip_cross[grp] = (
                    [a.ts_epoch for a in by_ts if a.ts_epoch is not None],
                    _suffix_max_steps([event_to_step.get((a.source_format, a.seq)) for a in by_ts]),
                )
        cross_idx[recip] = recip_cross
    return same_idx, cross_idx


def compute_excluded_mut_steps(
    events: list[NormalizedWorldEvent],
    git_events: list[NormalizedWorldEvent],
    informed_dup_keys: set[tuple[str, int]],
    event_to_step: dict[tuple[str, int], int],
) -> dict[str, set[int]]:
    has_bad: dict[tuple[str, int], bool] = {}
    has_good: set[tuple[str, int]] = set()
    for ev in (*events, *git_events):
        if ev.actor is None:
            continue
        sid = event_to_step.get((ev.source_format, ev.seq))
        if sid is None:
            continue
        key = (ev.actor, sid)
        is_dup = (ev.source_format, ev.seq) in informed_dup_keys
        is_post_play = ev.phase == "post_play" or ev.late
        if (
            is_dup
            or is_post_play
            or not ev.accepted
            or ev.kind in ("world_read", "message_fetch", "lifecycle")
        ):
            has_bad[key] = True
        elif ev.kind in ("world_action", "artifact_write", "message_post"):
            has_good.add(key)
    excluded: dict[str, set[int]] = defaultdict(set)
    for actor, sid in has_bad:
        if (actor, sid) not in has_good:
            excluded[actor].add(sid)
    return excluded


def compute_last_mut_steps(
    steps_by_member: dict[str, list[NormalizedAtifStep]],
    excluded_mut_steps: dict[str, set[int]] | None = None,
) -> dict[str, int]:
    out: dict[str, int] = {}
    excl = excluded_mut_steps or {}
    for mid, steps in steps_by_member.items():
        bad_sids = excl.get(mid, set())
        mut_sids = [
            st.step_id for st in steps if st.step_id not in bad_sids and is_mutating_atif_step(st)
        ]
        out[mid] = max(mut_sids) if mut_sids else -1
    return out


def has_downstream_action(
    recip: str,
    fetch_ev: NormalizedWorldEvent,
    same_idx: dict[tuple[str, str], tuple[list[int], list[int | None]]],
    cross_idx: dict[str, dict[str, tuple[list[float], list[int | None]]]],
    *,
    min_step_gt: int | None = None,
    allow_same_step_same_log: bool = False,
) -> bool:
    f_grp = log_group(fetch_ev.source_format)
    entry = same_idx.get((recip, f_grp))
    if entry is not None:
        seqs, s_max = entry
        pos = bisect_right(seqs, fetch_ev.seq)
        if pos < len(s_max):
            mx = s_max[pos]
            if (
                min_step_gt is None
                or mx is None
                or (mx >= min_step_gt if allow_same_step_same_log else mx > min_step_gt)
            ):
                return True
    if fetch_ev.ts_epoch is not None:
        for grp, (ts_arr, s_max) in cross_idx.get(recip, {}).items():
            if grp == f_grp:
                continue
            pos = bisect_right(ts_arr, fetch_ev.ts_epoch)
            if pos < len(s_max):
                mx = s_max[pos]
                if min_step_gt is None or mx is None or mx > min_step_gt:
                    return True
    return False


def _sweep_same_group_unread(
    actor: str,
    act_grp: str,
    bucket_acts: list[NormalizedWorldEvent],
    same_posts: list[tuple[int, NormalizedWorldEvent]],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    result: dict[int, list[tuple[int, NormalizedWorldEvent]]],
) -> None:
    same_sorted = sorted(same_posts, key=lambda item: (item[1].seq, item[0]))
    acts_sorted = sorted(bucket_acts, key=lambda a: a.seq)
    active: dict[int, NormalizedWorldEvent] = {}
    exit_seq_heap: list[tuple[int, int]] = []
    cross_f: dict[int, NormalizedWorldEvent] = {}
    p_ptr = 0
    for act in acts_sorted:
        while p_ptr < len(same_sorted) and same_sorted[p_ptr][1].seq < act.seq:
            ord_i, p = same_sorted[p_ptr]
            p_ptr += 1
            assert p.msg_id is not None
            f_ev = first_fetch.get((actor, act_grp, p.msg_id))
            if f_ev is not None and log_group(f_ev.source_format) == act_grp:
                if f_ev.seq > act.seq:
                    active[ord_i] = p
                    heapq.heappush(exit_seq_heap, (f_ev.seq, ord_i))
            else:
                active[ord_i] = p
                if f_ev is not None:
                    cross_f[ord_i] = f_ev
        while exit_seq_heap and exit_seq_heap[0][0] <= act.seq:
            _, ord_i = heapq.heappop(exit_seq_heap)
            active.pop(ord_i, None)
        if active:
            key = id(act)
            for ord_i, p in active.items():
                cf = cross_f.get(ord_i)
                if cf is None or not happened_before(cf, act, inclusive=True):
                    result[key].append((ord_i, p))


def _sweep_cross_group_unread(
    actor: str,
    act_grp: str,
    is_git: bool,
    bucket_acts: list[NormalizedWorldEvent],
    cross_posts: list[tuple[int, NormalizedWorldEvent]],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    result: dict[int, list[tuple[int, NormalizedWorldEvent]]],
) -> None:
    cross_sorted = sorted(cross_posts, key=lambda item: (item[1].ts_epoch or 0.0, item[0]))
    timed_acts = sorted(
        (a for a in bucket_acts if a.ts_epoch is not None),
        key=lambda a: (a.ts_epoch or 0.0, a.seq),
    )
    active_c: dict[int, NormalizedWorldEvent] = {}
    exit_ts_heap: list[tuple[float, int]] = []
    same_f: dict[int, NormalizedWorldEvent] = {}
    p_ptr = 0
    grace = 1.0 if is_git else 0.0
    for act in timed_acts:
        assert act.ts_epoch is not None
        a_ts = act.ts_epoch
        while p_ptr < len(cross_sorted) and (cross_sorted[p_ptr][1].ts_epoch or 0.0) < a_ts:
            ord_i, p = cross_sorted[p_ptr]
            p_ptr += 1
            assert p.msg_id is not None
            f_ev = first_fetch.get((actor, log_group(p.source_format), p.msg_id))
            if f_ev is not None and log_group(f_ev.source_format) == act_grp:
                active_c[ord_i] = p
                same_f[ord_i] = f_ev
            elif f_ev is not None and f_ev.ts_epoch is not None:
                if f_ev.ts_epoch > a_ts + grace:
                    active_c[ord_i] = p
                    heapq.heappush(exit_ts_heap, (f_ev.ts_epoch, ord_i))
            else:
                active_c[ord_i] = p
        while exit_ts_heap and exit_ts_heap[0][0] <= a_ts + grace:
            _, ord_i = heapq.heappop(exit_ts_heap)
            active_c.pop(ord_i, None)
        if active_c:
            key = id(act)
            for ord_i, p in active_c.items():
                sf = same_f.get(ord_i)
                if sf is None or not happened_before(sf, act, inclusive=True):
                    result[key].append((ord_i, p))


def sweep_unread_for_actor(
    actor: str,
    actor_actions: list[NormalizedWorldEvent],
    indexed_posts: list[tuple[int, NormalizedWorldEvent]],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
) -> dict[int, list[NormalizedWorldEvent]]:
    result: dict[int, list[tuple[int, NormalizedWorldEvent]]] = defaultdict(list)
    elig_by_grp: dict[str, list[tuple[int, NormalizedWorldEvent]]] = defaultdict(list)
    for ord_idx, post in indexed_posts:
        if (
            post.actor != actor
            and post.msg_id is not None
            and (not post.recipients or actor in post.recipients)
        ):
            elig_by_grp[log_group(post.source_format)].append((ord_idx, post))
    if not elig_by_grp:
        return {}

    acts_by_bucket: dict[tuple[str, bool], list[NormalizedWorldEvent]] = defaultdict(list)
    for act in actor_actions:
        acts_by_bucket[
            (log_group(act.source_format), act.source_format == "bespoke_git_bundle")
        ].append(act)

    for (act_grp, is_git), bucket_acts in acts_by_bucket.items():
        same_posts = elig_by_grp.get(act_grp)
        if same_posts:
            _sweep_same_group_unread(
                actor, act_grp, bucket_acts, same_posts, first_fetch, result
            )
        cross_posts = [
            (ord_i, p)
            for grp, plist in elig_by_grp.items()
            if grp != act_grp
            for ord_i, p in plist
            if p.ts_epoch is not None
        ]
        if cross_posts:
            _sweep_cross_group_unread(
                actor, act_grp, is_git, bucket_acts, cross_posts, first_fetch, result
            )

    out: dict[int, list[NormalizedWorldEvent]] = {}
    for key, pairs in result.items():
        pairs.sort(key=lambda item: item[0])
        out[key] = [p for _, p in pairs]
    return out
