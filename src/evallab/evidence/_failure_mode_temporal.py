from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from ._forensics_join import extract_timed_atif_file_reads
from ._forensics_rules import (
    happened_before,
    is_budget_exhausted,
    normalize_repo_path,
    read_targets,
    write_targets,
)
from ._forensics_sweep import (
    build_action_suffix_index,
    compute_last_mut_steps,
    has_downstream_action,
    log_group,
)
from ._multiagent_io import NormalizedAtifStep
from .multiagent_adapter import NormalizedMultiAgentTrial
from .world_events import NormalizedWorldEvent

_NON_RACING_COMMIT_RAW_KINDS = frozenset({"test_run", "refine", "transfer"})
_ACK_PHRASES = (
    "ok",
    "ack",
    "acknowledged",
    "got it",
    "thanks",
    "thank you",
    "done",
    "ready",
    "lgtm",
    "sounds good",
    "will do",
    "understood",
)
_UNREAD_GRACE_SEC = 2.0


def _budget_zero_before(
    recip: str,
    fetch_ev: NormalizedWorldEvent,
    budget_acts: dict[str, list[NormalizedWorldEvent]],
) -> bool:
    acts = budget_acts.get(recip)
    if not acts:
        return False
    last_prior: NormalizedWorldEvent | None = None
    for act in acts:
        if happened_before(act, fetch_ev, inclusive=True) and (
            last_prior is None or happened_before(last_prior, act)
        ):
            last_prior = act
    return last_prior is not None and is_budget_exhausted(last_prior.detail.get("budget_left"))


def _is_pure_ack(text: str | None) -> bool:
    cleaned = (text or "").strip().lower().strip(".!?,;:")
    return bool(cleaned) and cleaned in _ACK_PHRASES


def _last_agent_steps(obs_steps: dict[str, list[NormalizedAtifStep]]) -> dict[str, int]:
    last_agent_step: dict[str, int] = {}
    for mid, steps in obs_steps.items():
        agent_sids = [s.step_id for s in steps if s.source == "agent"]
        if agent_sids:
            last_agent_step[mid] = max(agent_sids)
    return last_agent_step


def _make_ignored_ep(
    make_ep: Callable[..., Any],
    recip: str,
    post: NormalizedWorldEvent,
    fetch_ev: NormalizedWorldEvent,
    in_ctx_step: int,
) -> Any:
    return make_ep(
        "ignored_finding",
        f"{recip} received msg {post.msg_id} from {post.actor} in step {in_ctx_step} "
        "and did not act on it before episode end",
        actor=recip,
        step_id=in_ctx_step,
        event_seq=fetch_ev.seq,
        ts=fetch_ev.ts,
        source_format=fetch_ev.source_format,
        target=post.target,
        peer_actor=post.actor,
        peer_event_seq=post.seq,
        msg_id=post.msg_id,
        suffix=f"msg={post.msg_id}",
        evidence={
            "fetch_seq": fetch_ev.seq,
            "post_seq": post.seq,
            "in_context_step_id": in_ctx_step,
            "snippet": (post.text or "")[:120],
        },
    )


def detect_ignored_findings(
    trial: NormalizedMultiAgentTrial,
    peer_posts: list[NormalizedWorldEvent],
    roster: list[str],
    roster_set: set[str],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    in_context_steps: dict[tuple[str, str, str], int],
    actions_by_member: dict[str, list[NormalizedWorldEvent]],
    informed_dup_keys: set[tuple[str, int]],
    event_to_step: dict[tuple[str, int], int],
    make_ep: Callable[..., Any],
    *,
    observable_steps: dict[str, list[NormalizedAtifStep]] | None = None,
    excluded_mut_steps: dict[str, set[int]] | None = None,
) -> list[Any]:
    episodes: list[Any] = []
    member_by_id = {m.member_id: m for m in trial.members}
    obs_steps = observable_steps if observable_steps is not None else trial.member_steps
    last_agent_step = _last_agent_steps(obs_steps)
    last_mut_step = compute_last_mut_steps(obs_steps, excluded_mut_steps)
    posts_by_member: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
    for post in peer_posts:
        if post.actor is not None:
            posts_by_member[post.actor].append(post)
    post_same_idx, post_cross_idx = build_action_suffix_index(posts_by_member, set(), event_to_step)
    budget_acts = {
        recip: b_list
        for recip, acts in actions_by_member.items()
        if (b_list := [a for a in acts if "budget_left" in a.detail])
    }
    same_idx, cross_idx = build_action_suffix_index(
        actions_by_member, informed_dup_keys, event_to_step
    )
    for post in peer_posts:
        if post.actor is None or post.msg_id is None or _is_pure_ack(post.text):
            continue
        p_grp = log_group(post.source_format)
        eligible = (
            sorted({r for r in post.recipients if r in roster_set and r != post.actor})
            if post.recipients
            else [r for r in roster if r != post.actor]
        )
        for recip in eligible:
            if member_by_id[recip].trajectory_status != "present":
                continue
            in_ctx_step = in_context_steps.get((recip, p_grp, post.msg_id))
            fetch_ev = first_fetch.get((recip, p_grp, post.msg_id))
            if in_ctx_step is None or fetch_ev is None:
                continue
            if _budget_zero_before(recip, fetch_ev, budget_acts):
                continue
            acted = (
                has_downstream_action(
                    recip, fetch_ev, same_idx, cross_idx, min_step_gt=in_ctx_step, allow_same_step_same_log=True
                )
                or has_downstream_action(
                    recip, fetch_ev, post_same_idx, post_cross_idx, min_step_gt=in_ctx_step, allow_same_step_same_log=True
                )
                or last_mut_step.get(recip, -1) > in_ctx_step
            )
            if not acted and last_agent_step.get(recip, -1) > in_ctx_step:
                episodes.append(_make_ignored_ep(make_ep, recip, post, fetch_ev, in_ctx_step))
    return episodes


def _unread_posts_for_commit_action(
    act: NormalizedWorldEvent,
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    unread_by_action: dict[int, list[NormalizedWorldEvent]] | None,
) -> list[NormalizedWorldEvent]:
    assert act.actor is not None
    act_round = act.detail.get("round")
    if unread_by_action is not None:
        raw_unread = unread_by_action.get(id(act), ())
        if not raw_unread:
            return []
        return [
            post
            for post in raw_unread
            if not (
                type(act_round) is int
                and type(post.detail.get("round")) is int
                and post.detail["round"] > act_round
            )
            and not (
                act.ts_epoch is not None
                and post.ts_epoch is not None
                and (act.ts_epoch - post.ts_epoch) < _UNREAD_GRACE_SEC
            )
        ]
    unread_posts: list[NormalizedWorldEvent] = []
    for post in peer_posts:
        if post.actor == act.actor or post.msg_id is None:
            continue
        if post.recipients and act.actor not in post.recipients:
            continue
        post_round = post.detail.get("round")
        if type(act_round) is int and type(post_round) is int and post_round > act_round:
            continue
        if not happened_before(post, act):
            continue
        if (
            act.ts_epoch is not None
            and post.ts_epoch is not None
            and (act.ts_epoch - post.ts_epoch) < _UNREAD_GRACE_SEC
        ):
            continue
        f_ev = first_fetch.get((act.actor, log_group(post.source_format), post.msg_id))
        if f_ev is not None and happened_before(f_ev, act, inclusive=True):
            continue
        unread_posts.append(post)
    return unread_posts


def detect_commit_race_unread(
    actions: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    event_to_step: dict[tuple[str, int], int],
    make_ep: Callable[..., Any],
    *,
    unread_by_action: dict[int, list[NormalizedWorldEvent]] | None = None,
) -> list[Any]:
    episodes: list[Any] = []
    if not peer_posts or not actions:
        return episodes
    for act in actions:
        if act.actor is None or act.raw_kind in _NON_RACING_COMMIT_RAW_KINDS:
            continue
        unread_posts = _unread_posts_for_commit_action(act, peer_posts, first_fetch, unread_by_action)
        if not unread_posts:
            continue
        first_u = unread_posts[0]
        episodes.append(
            make_ep(
                "commit_race_unread",
                f"{act.actor} executed {act.raw_kind or act.kind} on {act.target or 'state'} "
                f"with {len(unread_posts)} unread peer message(s)",
                actor=act.actor,
                step_id=event_to_step.get((act.source_format, act.seq)),
                event_seq=act.seq,
                ts=act.ts,
                source_format=act.source_format,
                target=act.target,
                peer_actor=first_u.actor,
                peer_event_seq=first_u.seq,
                msg_id=first_u.msg_id,
                suffix=f"src={act.source_format}",
                evidence={
                    "unread_count": len(unread_posts),
                    "unread_msg_ids": [p.msg_id for p in unread_posts[:10]],
                    "peer_actors": sorted({p.actor for p in unread_posts if p.actor}),
                },
            )
        )
    return episodes


def _find_intervening_world_write(
    writes: list[tuple[int, str]],
    prev_r: int,
    actor: str,
    ev_seq: int,
) -> tuple[int, str] | None:
    for w_seq, w_actor in reversed(writes):
        if w_seq <= prev_r:
            break
        if w_actor != actor and w_seq < ev_seq:
            return (w_seq, w_actor)
    return None


def _detect_world_stale_overwrites(
    in_play_events: list[NormalizedWorldEvent],
    roster_set: set[str],
    event_to_step: dict[tuple[str, int], int],
    make_ep: Callable[..., Any],
    seen: set[tuple[str, int, str]],
    episodes: list[Any],
) -> None:
    last_read_seq: dict[tuple[str, str, str], int] = {}
    writes_seq: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    ordered_in_play = sorted(
        (ev for ev in in_play_events if ev.actor in roster_set and ev.accepted),
        key=lambda ev: (log_group(ev.source_format), ev.seq),
    )
    for ev in ordered_in_play:
        assert ev.actor is not None
        grp = log_group(ev.source_format)
        if ev.kind == "world_read":
            for tgt in read_targets(ev):
                last_read_seq[(grp, ev.actor, tgt)] = ev.seq
        elif ev.kind in ("world_action", "artifact_write"):
            for tgt in write_targets(ev):
                prev_r = last_read_seq.get((grp, ev.actor, tgt))
                if prev_r is not None:
                    hit_w = _find_intervening_world_write(
                        writes_seq[(grp, tgt)], prev_r, ev.actor, ev.seq
                    )
                    if hit_w is not None and (ev.source_format, ev.seq, tgt) not in seen:
                        seen.add((ev.source_format, ev.seq, tgt))
                        w_seq, w_actor = hit_w
                        episodes.append(
                            make_ep(
                                "stale_read_overwrite",
                                f"{ev.actor} wrote {tgt} after {w_actor} updated it, without re-reading",
                                severity="error",
                                actor=ev.actor,
                                step_id=event_to_step.get((ev.source_format, ev.seq)),
                                event_seq=ev.seq,
                                ts=ev.ts,
                                source_format=ev.source_format,
                                target=tgt,
                                peer_actor=w_actor,
                                peer_event_seq=w_seq,
                                suffix=f"src={ev.source_format}:tgt={tgt}",
                                evidence={"read_event_seq": prev_r, "peer_write_seq": w_seq},
                            )
                        )
                writes_seq[(grp, tgt)].append((ev.seq, ev.actor))
                last_read_seq[(grp, ev.actor, tgt)] = ev.seq


def _build_git_stale_stream(
    reads: list[tuple[float, str, str]],
    in_play_git: list[NormalizedWorldEvent],
    roster_set: set[str],
) -> tuple[
    list[tuple[float, int, str, str, str, NormalizedWorldEvent | None]],
    dict[tuple[str, str], list[float]],
]:
    stream: list[tuple[float, int, str, str, str, NormalizedWorldEvent | None]] = [
        (ts_ep, 0, "read", actor, normalize_repo_path(fpath), None)
        for ts_ep, actor, fpath in reads
        if actor in roster_set
    ]
    for gev in in_play_git:
        if gev.actor not in roster_set or not gev.accepted or gev.ts_epoch is None:
            continue
        for fpath in gev.files:
            stream.append((gev.ts_epoch, 1, "write", gev.actor, normalize_repo_path(fpath), gev))
    reads_by_at: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ts_ep, r_actor, r_fpath in reads:
        if r_actor in roster_set:
            reads_by_at[(r_actor, normalize_repo_path(r_fpath))].append(ts_ep)
    for r_list in reads_by_at.values():
        r_list.sort()
    stream.sort(key=lambda item: (item[0], item[1], item[3], item[4]))
    return stream, reads_by_at


def _find_intervening_git_write(
    writes: list[tuple[float, str, int, str | None]],
    actor: str,
    prev_ts: float,
    t_val: float,
    gev: NormalizedWorldEvent,
    r_list: list[float] | tuple[float, ...],
) -> tuple[float, str, int, str | None] | None:
    hit_g: tuple[float, str, int, str | None] | None = None
    for row in reversed(writes):
        if row[0] <= prev_ts:
            break
        if (
            row[1] != actor
            and row[0] < t_val
            and (row[3] is None or gev.branch is None or row[3] == gev.branch)
        ):
            hit_g = row
            break
    if hit_g is not None:
        r_idx = bisect_right(r_list, hit_g[0])
        hi_ts = t_val + (1.0 if gev.source_format == "bespoke_git_bundle" else 0.0)
        if r_idx < len(r_list) and r_list[r_idx] <= hi_ts:
            return None
    return hit_g


def _detect_git_stale_overwrites(
    trial: NormalizedMultiAgentTrial,
    in_play_git: list[NormalizedWorldEvent],
    roster_set: set[str],
    event_to_step: dict[tuple[str, int], int],
    make_ep: Callable[..., Any],
    timed_reads: list[tuple[float, str, str]] | None,
    seen: set[tuple[str, int, str]],
    episodes: list[Any],
) -> None:
    reads = (
        timed_reads
        if timed_reads is not None
        else extract_timed_atif_file_reads(trial.member_steps)
    )
    stream, reads_by_at = _build_git_stale_stream(reads, in_play_git, roster_set)
    last_read_ts: dict[tuple[str, str], float] = {}
    last_write_ts: dict[tuple[str, str, str | None], float] = {}
    writes_ts: dict[str, list[tuple[float, str, int, str | None]]] = defaultdict(list)
    for t_val, _, op, actor, tgt, gev in stream:
        if op == "read":
            last_read_ts[(actor, tgt)] = t_val
            continue
        if gev is None:
            continue
        r_prev = last_read_ts.get((actor, tgt))
        w_prev = last_write_ts.get((actor, tgt, gev.branch))
        prev_ts = (
            max(r_prev, w_prev)
            if r_prev is not None and w_prev is not None
            else (r_prev if r_prev is not None else w_prev)
        )
        if prev_ts is not None:
            hit_g = _find_intervening_git_write(
                writes_ts[tgt], actor, prev_ts, t_val, gev, reads_by_at.get((actor, tgt), ())
            )
            if hit_g is not None and (gev.source_format, gev.seq, tgt) not in seen:
                seen.add((gev.source_format, gev.seq, tgt))
                w_ts, w_actor, w_seq, _ = hit_g
                episodes.append(
                    make_ep(
                        "stale_read_overwrite",
                        f"{actor} wrote {tgt} after {w_actor} updated it, without re-reading",
                        severity="error",
                        actor=actor,
                        step_id=event_to_step.get((gev.source_format, gev.seq)),
                        event_seq=gev.seq,
                        ts=gev.ts,
                        source_format=gev.source_format,
                        target=tgt,
                        peer_actor=w_actor,
                        peer_event_seq=w_seq,
                        suffix=f"src={gev.source_format}:tgt={tgt}",
                        evidence={
                            "read_ts_epoch": prev_ts,
                            "peer_write_ts_epoch": w_ts,
                            "write_ts_epoch": t_val,
                        },
                    )
                )
        writes_ts[tgt].append((t_val, actor, gev.seq, gev.branch))
        last_write_ts[(actor, tgt, gev.branch)] = t_val


def detect_stale_read_overwrites(
    trial: NormalizedMultiAgentTrial,
    in_play_events: list[NormalizedWorldEvent],
    in_play_git: list[NormalizedWorldEvent],
    roster_set: set[str],
    event_to_step: dict[tuple[str, int], int],
    make_ep: Callable[..., Any],
    *,
    timed_reads: list[tuple[float, str, str]] | None = None,
) -> list[Any]:
    episodes: list[Any] = []
    seen: set[tuple[str, int, str]] = set()
    _detect_world_stale_overwrites(
        in_play_events, roster_set, event_to_step, make_ep, seen, episodes
    )
    if in_play_git:
        _detect_git_stale_overwrites(
            trial, in_play_git, roster_set, event_to_step, make_ep, timed_reads, seen, episodes
        )
    return episodes
