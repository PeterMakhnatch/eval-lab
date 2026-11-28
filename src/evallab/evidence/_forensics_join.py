from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

from ._forensics_context import (
    extract_timed_atif_file_reads,
    find_in_context_steps,
    message_in_step_context,
)
from ._forensics_join_match import (
    _CLOCK_SKEW_SEC,
    _MAX_TS_WINDOW_SEC,
    MatchBasis,
    StepEventLink,
    _get_norm_args,
    _is_mutating_world_event,
    _make_link,
    _MemberStepCache,
    _step_window_check,
    _target_tokens,
    match_commit_sha,
    match_message_fetch,
    match_message_post,
    match_request_id_in_window,
    match_target_tokens,
    match_timestamp_fallback,
)
from ._forensics_rules import (
    FETCH_TOOL_HINTS,
    FILE_READ_TOOLS,
    SEND_TOOL_HINTS,
    is_read_only_tool_call,
    normalize_text,
    short_tool_name,
    step_observation_corpus,
    unwrap_observation_text,
)
from ._forensics_sweep import log_group
from ._multiagent_io import NormalizedAtifStep
from .world_events import NormalizedWorldEvent

__all__ = [
    "MatchBasis",
    "StepEventLink",
    "extract_timed_atif_file_reads",
    "find_in_context_steps",
    "join_steps_and_events",
    "message_in_step_context",
    "normalize_text",
    "step_observation_corpus",
    "unwrap_observation_text",
]


def _step_declared_duration(step: NormalizedAtifStep) -> float:
    total = 0.0
    for tc in step.tool_calls:
        val = tc.arguments.get("duration")
        if (
            isinstance(val, (int, float))
            and not isinstance(val, bool)
            and math.isfinite(val)
            and val > 0
        ):
            total += min(float(val), 60.0)
    return min(total, 120.0)


def _step_has_hint(step: NormalizedAtifStep, hints: tuple[str, ...]) -> bool:
    for tc in step.tool_calls:
        fn = short_tool_name(tc.function_name)
        if fn in FILE_READ_TOOLS and fn != "read":
            continue
        hay = f"{tc.function_name} {tc.arguments_text}".lower()
        if any(hint in hay for hint in hints):
            return True
    return False


def _is_read_only_step(step: NormalizedAtifStep) -> bool:
    return bool(step.tool_calls) and all(is_read_only_tool_call(tc) for tc in step.tool_calls)


def _pair_tool_steps(
    steps: list[NormalizedAtifStep],
) -> tuple[dict[int, str], list[tuple[str, NormalizedAtifStep]]]:
    paired_obs: dict[int, list[str]] = {}
    paired_ids: list[tuple[str, NormalizedAtifStep]] = []
    cur_agent: NormalizedAtifStep | None = None
    for st in steps:
        if st.source == "agent":
            cur_agent = st if st.tool_calls else None
        elif st.source == "tool" and cur_agent is not None:
            corp = step_observation_corpus(st)
            if corp.strip():
                paired_obs.setdefault(id(cur_agent), []).append(corp)
            for obs in st.observations:
                if obs.source_call_id and len(obs.source_call_id) >= 3:
                    paired_ids.append((obs.source_call_id, cur_agent))
        elif st.source in ("user", "system"):
            cur_agent = None
    merged_norm: dict[int, str] = {}
    for st in steps:
        if id(st) in paired_obs:
            own = step_observation_corpus(st)
            parts = [own, *paired_obs[id(st)]] if own.strip() else paired_obs[id(st)]
            merged_norm[id(st)] = normalize_text("\n".join(parts))
    return merged_norm, paired_ids


def _build_step_cache(steps: list[NormalizedAtifStep]) -> _MemberStepCache:
    agent_steps = [s for s in steps if s.source == "agent"] or steps
    wake_steps = [
        s
        for s in steps
        if s.source in ("user", "tool")
        and (s.source == "tool" or s.ts_epoch is not None or s.step_id > 2)
        and bool(step_observation_corpus(s).strip())
    ]
    has_tools = any(bool(s.tool_calls) for s in agent_steps)
    exec_steps = [s for s in agent_steps if s.tool_calls] if has_tools else agent_steps
    timed_exec = sorted(
        (s for s in exec_steps if s.ts_epoch is not None),
        key=lambda s: (s.ts_epoch or 0.0, s.step_id),
    )
    timed_epochs = [s.ts_epoch for s in timed_exec if s.ts_epoch is not None]
    untimed_exec = [s for s in exec_steps if s.ts_epoch is None]
    seen_timed_ids: set[int] = set()
    all_timed_raw: list[NormalizedAtifStep] = []
    for s in (*agent_steps, *(w for w in wake_steps if w.source == "user")):
        if s.ts_epoch is not None and id(s) not in seen_timed_ids:
            seen_timed_ids.add(id(s))
            all_timed_raw.append(s)
    all_timed = sorted(all_timed_raw, key=lambda s: (s.ts_epoch or 0.0, s.step_id))
    prev_epochs = {
        id(step): (all_timed[idx - 1].ts_epoch if idx > 0 else None)
        for idx, step in enumerate(all_timed)
    }
    next_epochs = {
        id(step): (all_timed[idx + 1].ts_epoch if idx + 1 < len(all_timed) else None)
        for idx, step in enumerate(all_timed)
    }
    args_low = {
        id(s): normalize_text(
            " ".join(
                f"{short_tool_name(tc.function_name)} {tc.arguments_text}" for tc in s.tool_calls
            )
        )
        for s in exec_steps
    }
    paired_norm_obs, paired_call_ids = _pair_tool_steps(steps)
    req_id_to_step: dict[str, tuple[NormalizedAtifStep, str]] = {}
    for s in exec_steps:
        for tc in s.tool_calls:
            if tc.tool_call_id and len(tc.tool_call_id) >= 3:
                req_id_to_step.setdefault(tc.tool_call_id, (s, "request_id"))
        for obs in s.observations:
            if obs.source_call_id and len(obs.source_call_id) >= 3:
                req_id_to_step.setdefault(obs.source_call_id, (s, "source_call_id"))
    for call_id, ag_step in paired_call_ids:
        req_id_to_step.setdefault(call_id, (ag_step, "source_call_id"))
    return _MemberStepCache(
        agent_steps=agent_steps,
        exec_steps=exec_steps,
        timed_exec=timed_exec,
        timed_epochs=timed_epochs,
        untimed_exec=untimed_exec,
        wake_steps=wake_steps,
        prev_epochs=prev_epochs,
        next_epochs=next_epochs,
        dur_by_step={id(s): _step_declared_duration(s) for s in exec_steps},
        args_low=args_low,
        read_only_step={id(s): _is_read_only_step(s) for s in exec_steps},
        has_send_hint={id(s): _step_has_hint(s, SEND_TOOL_HINTS) for s in exec_steps},
        has_fetch_hint={id(s): _step_has_hint(s, FETCH_TOOL_HINTS) for s in exec_steps},
        req_id_to_step=req_id_to_step,
        norm_obs=paired_norm_obs,
    )


def _candidate_window_steps(
    ev_epoch: float | None,
    cache: _MemberStepCache,
) -> list[tuple[NormalizedAtifStep, bool, float]]:
    if ev_epoch is None:
        return [(step, False, 0.0) for step in cache.exec_steps]
    out: list[tuple[NormalizedAtifStep, bool, float]] = []
    if cache.timed_epochs:
        if cache.end_stamped:
            i0 = bisect_left(cache.timed_epochs, ev_epoch - _CLOCK_SKEW_SEC - 1e-9)
            i1 = bisect_right(
                cache.timed_epochs, ev_epoch + _MAX_TS_WINDOW_SEC + _CLOCK_SKEW_SEC + 1e-9
            )
        else:
            i0 = bisect_left(cache.timed_epochs, ev_epoch - _MAX_TS_WINDOW_SEC - 1e-9)
            i1 = bisect_right(cache.timed_epochs, ev_epoch + _CLOCK_SKEW_SEC + 1e-9)
        for step in cache.timed_exec[i0:i1]:
            in_win, strict_in, delta = _step_window_check(ev_epoch, step, cache)
            if in_win:
                out.append((step, strict_in, delta))
    for step in cache.untimed_exec:
        out.append((step, False, 0.0))
    return out


def _infer_end_stamped(
    cache: _MemberStepCache,
    member_events: list[NormalizedWorldEvent],
    msg_norm_by_id: dict[tuple[str, str], str],
) -> bool:
    if not cache.timed_exec:
        return False
    neg_votes = 0
    pos_votes = 0
    for ev in member_events:
        if ev.ts_epoch is None:
            continue
        i0 = bisect_left(cache.timed_epochs, ev.ts_epoch - 30.0 - 1e-9)
        i1 = bisect_right(cache.timed_epochs, ev.ts_epoch + 30.0 + 1e-9)
        nearby = cache.timed_exec[i0:i1]
        if not nearby:
            continue
        hits: list[NormalizedAtifStep] = []
        if ev.kind == "message_post":
            mkey = (log_group(ev.source_format), ev.msg_id) if ev.msg_id else None
            norm_msg = (msg_norm_by_id.get(mkey) if mkey else None) or normalize_text(ev.text or "")
            snippet = norm_msg[:64] if len(norm_msg) >= 6 else ""
            if snippet:
                hits = [
                    s
                    for s in nearby
                    if s.ts_epoch is not None
                    and abs(ev.ts_epoch - s.ts_epoch) <= 30.0
                    and not cache.read_only_step.get(id(s))
                    and snippet in _get_norm_args(cache, s)
                ]
        if not hits:
            toks = [t for t in _target_tokens(ev) if not t.startswith("/")]
            if toks:
                hits = [
                    s
                    for s in nearby
                    if s.ts_epoch is not None
                    and abs(ev.ts_epoch - s.ts_epoch) <= 30.0
                    and not (_is_mutating_world_event(ev) and cache.read_only_step.get(id(s)))
                    and any(t in cache.args_low.get(id(s), "") for t in toks)
                ]
        if len(hits) == 1 and hits[0].ts_epoch is not None:
            diff = ev.ts_epoch - hits[0].ts_epoch
            neg_thresh = -1.05 if ev.source_format == "bespoke_git_bundle" else -0.05
            if diff < neg_thresh:
                neg_votes += 1
            elif diff > 0.05:
                pos_votes += 1
    if neg_votes > pos_votes and neg_votes >= 1:
        return True
    if pos_votes > neg_votes:
        return False
    return any(d > 0.0 for d in cache.dur_by_step.values())


def _match_step_for_event(
    ev: NormalizedWorldEvent,
    cache: _MemberStepCache,
    msg_norm_by_id: dict[tuple[str, str], str],
) -> StepEventLink | None:
    if not cache.exec_steps or ev.actor is None:
        return None
    if (
        ev.request_id
        and len(ev.request_id) >= 3
        and (hit_req := cache.req_id_to_step.get(ev.request_id)) is not None
    ):
        r_step, r_tag = hit_req
        return _make_link(ev, r_step, "request_id", f"{r_tag}={ev.request_id}")

    window_steps = _candidate_window_steps(ev.ts_epoch, cache)
    if not window_steps and not (
        ev.kind == "lifecycle" and ev.fetched_msg_ids and cache.wake_steps
    ):
        return None
    if hit := match_request_id_in_window(ev, window_steps):
        return hit

    ev_grp = log_group(ev.source_format)
    if ev.kind == "message_post":
        if hit := match_message_post(ev, ev_grp, window_steps, cache, msg_norm_by_id):
            return hit
    elif (
        ev.kind in ("message_fetch", "lifecycle")
        and ev.fetched_msg_ids
        and (hit := match_message_fetch(ev, ev_grp, window_steps, cache, msg_norm_by_id))
    ):
        return hit

    if hit := match_commit_sha(ev, window_steps, cache):
        return hit
    if hit := match_target_tokens(ev, window_steps, cache):
        return hit
    return match_timestamp_fallback(ev, window_steps, cache)


def join_steps_and_events(
    steps_by_member: dict[str, list[NormalizedAtifStep]],
    events: list[NormalizedWorldEvent],
    git_events: list[NormalizedWorldEvent],
    roster_set: set[str],
) -> tuple[list[StepEventLink], dict[tuple[str, int], int]]:
    msg_norm_by_id = {
        (log_group(ev.source_format), ev.msg_id): normalize_text(ev.text)
        for ev in events
        if ev.kind == "message_post" and ev.accepted and ev.msg_id is not None and ev.text
    }
    events_by_actor: dict[str, list[NormalizedWorldEvent]] = {}
    joinable: list[NormalizedWorldEvent] = []
    for ev in (*events, *git_events):
        if ev.actor not in roster_set or ev.kind == "world_internal":
            continue
        if (
            ev.kind == "lifecycle"
            and ev.raw_kind not in ("seat_done", "done")
            and not ev.fetched_msg_ids
        ):
            continue
        joinable.append(ev)
        events_by_actor.setdefault(ev.actor, []).append(ev)

    step_caches: dict[str, _MemberStepCache] = {}
    for member_id, steps in steps_by_member.items():
        cache = _build_step_cache(steps)
        cache.end_stamped = _infer_end_stamped(
            cache, events_by_actor.get(member_id, []), msg_norm_by_id
        )
        step_caches[member_id] = cache

    links: list[StepEventLink] = []
    event_to_step: dict[tuple[str, int], int] = {}
    for ev in joinable:
        assert ev.actor is not None
        cache = step_caches.get(ev.actor)
        if cache is None:
            continue
        link = _match_step_for_event(ev, cache, msg_norm_by_id)
        if link is not None:
            links.append(link)
            event_to_step[(ev.source_format, ev.seq)] = link.step_id
    links.sort(key=lambda item: (item.member_id, item.step_id, item.event_source, item.event_seq))
    return links, event_to_step
