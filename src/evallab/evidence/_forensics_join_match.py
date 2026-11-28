from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ._forensics_rules import (
    GENERIC_RAW_KINDS,
    extract_id_tokens,
    has_id_token,
    normalize_repo_path,
    normalize_text,
    step_observation_corpus,
)
from ._multiagent_io import NormalizedAtifStep
from .world_events import NormalizedWorldEvent

MatchBasis = Literal[
    "request_id",
    "msg_id",
    "tool_argument",
    "observation_content",
    "timestamp_window",
]

_MAX_TS_WINDOW_SEC = 180.0
_CLOCK_SKEW_SEC = 2.0


class StepEventLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: str
    step_id: int
    event_source: str
    event_seq: int
    event_kind: str
    match_basis: MatchBasis
    evidence: str


@dataclass
class _MemberStepCache:
    agent_steps: list[NormalizedAtifStep]
    exec_steps: list[NormalizedAtifStep]
    timed_exec: list[NormalizedAtifStep]
    timed_epochs: list[float]
    untimed_exec: list[NormalizedAtifStep]
    wake_steps: list[NormalizedAtifStep]
    prev_epochs: dict[int, float | None]
    next_epochs: dict[int, float | None]
    dur_by_step: dict[int, float]
    args_low: dict[int, str]
    read_only_step: dict[int, bool]
    has_send_hint: dict[int, bool]
    has_fetch_hint: dict[int, bool]
    req_id_to_step: dict[str, tuple[NormalizedAtifStep, str]] = field(default_factory=dict)
    end_stamped: bool = False
    norm_args: dict[int, str] = field(default_factory=dict)
    norm_obs: dict[int, str] = field(default_factory=dict)
    obs_ids: dict[int, frozenset[str]] = field(default_factory=dict)


def _step_window_check(
    ev_epoch: float | None,
    step: NormalizedAtifStep,
    cache: _MemberStepCache,
) -> tuple[bool, bool, float]:
    if ev_epoch is None or step.ts_epoch is None:
        return True, False, 0.0
    sid = id(step)
    prev_ep = cache.prev_epochs.get(sid)
    next_ep = cache.next_epochs.get(sid)
    delta = abs(ev_epoch - step.ts_epoch)
    if cache.end_stamped:
        first_budget = max(10.0, cache.dur_by_step.get(sid, 0.0) + 5.0)
        lo = (
            max(prev_ep - _CLOCK_SKEW_SEC, step.ts_epoch - _MAX_TS_WINDOW_SEC)
            if prev_ep is not None
            else step.ts_epoch - first_budget - _CLOCK_SKEW_SEC
        )
        hi = step.ts_epoch + _CLOCK_SKEW_SEC
        if not (lo <= ev_epoch <= hi):
            return False, False, delta
        strict_in = (
            (prev_ep < ev_epoch <= step.ts_epoch)
            if prev_ep is not None
            else (step.ts_epoch - first_budget <= ev_epoch <= step.ts_epoch)
        )
        return True, strict_in, delta
    lo = step.ts_epoch - _CLOCK_SKEW_SEC
    hi = (
        min(next_ep + _CLOCK_SKEW_SEC, step.ts_epoch + _MAX_TS_WINDOW_SEC)
        if next_ep is not None
        else step.ts_epoch + _MAX_TS_WINDOW_SEC
    )
    if not (lo <= ev_epoch <= hi):
        return False, False, delta
    strict_in = step.ts_epoch <= ev_epoch and (next_ep is None or ev_epoch < next_ep)
    return True, strict_in, delta


def _is_mutating_world_event(ev: NormalizedWorldEvent) -> bool:
    return ev.kind in ("artifact_write", "message_post") or (
        ev.kind == "world_action" and ev.raw_kind not in ("done", "seat_done")
    )


def _get_norm_args(cache: _MemberStepCache, step: NormalizedAtifStep) -> str:
    key = id(step)
    val = cache.norm_args.get(key)
    if val is None:
        val = normalize_text(" ".join(tc.arguments_text for tc in step.tool_calls))
        cache.norm_args[key] = val
    return val


def _get_norm_obs(cache: _MemberStepCache, step: NormalizedAtifStep) -> str:
    key = id(step)
    val = cache.norm_obs.get(key)
    if val is None:
        val = normalize_text(step_observation_corpus(step))
        cache.norm_obs[key] = val
    return val


def _get_obs_ids(cache: _MemberStepCache, step: NormalizedAtifStep) -> frozenset[str]:
    key = id(step)
    val = cache.obs_ids.get(key)
    if val is None:
        val = extract_id_tokens(_get_norm_obs(cache, step))
        cache.obs_ids[key] = val
    return val


def _target_tokens(ev: NormalizedWorldEvent) -> list[str]:
    tokens: list[str] = []
    for fpath in ev.files:
        norm = normalize_repo_path(fpath).lower()
        if len(norm) >= 3:
            tokens.append(norm)
    if ev.target and ":" in ev.target:
        rhs = ev.target.split(":", 1)[1].lower()
        if "=" in rhs:
            for part in rhs.split(","):
                if "=" not in part:
                    continue
                k, v = part.split("=", 1)
                if k and v:
                    tokens.append(f'"{k}": {v}')
                    tokens.append(f'"{k}":{v}')
                    tokens.append(f'"{k}": "{v}"')
                    tokens.append(f'"{k}":"{v}"')
        elif len(rhs) >= 2 and rhs not in ("public", "default", "unknown", "messages"):
            tokens.append(rhs)
    if ev.raw_kind:
        rk = ev.raw_kind.lower()
        if rk in GENERIC_RAW_KINDS:
            tokens.append(f"/{rk}")
        elif len(rk) >= 3:
            tokens.append(rk)
    return tokens


def _pick_best(
    candidates: list[tuple[tuple[int, float, int], StepEventLink]],
) -> StepEventLink | None:
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _make_link(
    ev: NormalizedWorldEvent, step: NormalizedAtifStep, basis: MatchBasis, evidence: str
) -> StepEventLink:
    assert ev.actor is not None
    return StepEventLink(
        member_id=ev.actor,
        step_id=step.step_id,
        event_source=ev.source_format,
        event_seq=ev.seq,
        event_kind=ev.kind,
        match_basis=basis,
        evidence=evidence,
    )


def _rank_tier(
    ev_epoch: float | None, step: NormalizedAtifStep, strict_in: bool, base: int = 0
) -> int:
    if ev_epoch is not None and step.ts_epoch is None:
        return base + 2
    return base if strict_in else base + 1


def match_request_id_in_window(
    ev: NormalizedWorldEvent,
    window_steps: list[tuple[NormalizedAtifStep, bool, float]],
) -> StepEventLink | None:
    if not (ev.request_id and len(ev.request_id) >= 3):
        return None
    cands: list[tuple[tuple[int, float, int], StepEventLink]] = []
    for step, strict_in, delta in window_steps:
        if any(ev.request_id in tc.arguments_text for tc in step.tool_calls):
            cands.append(
                (
                    (_rank_tier(ev.ts_epoch, step, strict_in), delta, step.step_id),
                    _make_link(ev, step, "request_id", f"request_id={ev.request_id}"),
                )
            )
    return _pick_best(cands)


def match_message_post(
    ev: NormalizedWorldEvent,
    ev_grp: str,
    window_steps: list[tuple[NormalizedAtifStep, bool, float]],
    cache: _MemberStepCache,
    msg_norm_by_id: dict[tuple[str, str], str],
) -> StepEventLink | None:
    mkey = (ev_grp, ev.msg_id) if ev.msg_id else None
    norm_msg = (msg_norm_by_id.get(mkey) if mkey else None) or normalize_text(ev.text or "")
    snippet = norm_msg[:64] if len(norm_msg) >= 6 else ""
    cands: list[tuple[tuple[int, float, int], StepEventLink]] = []
    for step, strict_in, delta in window_steps:
        if cache.read_only_step.get(id(step)):
            continue
        if snippet and snippet in _get_norm_args(cache, step):
            cands.append(
                (
                    (_rank_tier(ev.ts_epoch, step, strict_in, 0), delta, step.step_id),
                    _make_link(ev, step, "msg_id", f"msg_id={ev.msg_id or ev.seq}:text_in_args"),
                )
            )
        elif (
            ev.msg_id
            and cache.has_send_hint.get(id(step))
            and has_id_token(
                _get_norm_obs(cache, step), ev.msg_id, obs_ids=_get_obs_ids(cache, step)
            )
        ):
            cands.append(
                (
                    (_rank_tier(ev.ts_epoch, step, strict_in, 3), delta, step.step_id),
                    _make_link(ev, step, "msg_id", f"msg_id={ev.msg_id}:id_in_obs"),
                )
            )
    return _pick_best(cands)


def match_message_fetch(
    ev: NormalizedWorldEvent,
    ev_grp: str,
    window_steps: list[tuple[NormalizedAtifStep, bool, float]],
    cache: _MemberStepCache,
    msg_norm_by_id: dict[tuple[str, str], str],
) -> StepEventLink | None:
    cands: list[tuple[tuple[int, float, int], StepEventLink]] = []
    search_pool = list(window_steps)
    if ev.kind == "lifecycle" and cache.wake_steps:
        seen_pool = {id(s) for s, _, _ in search_pool}
        for w_step in cache.wake_steps:
            if id(w_step) in seen_pool:
                continue
            ok, strict_in, delta = _step_window_check(ev.ts_epoch, w_step, cache)
            if ok:
                search_pool.append((w_step, strict_in, delta))
    for step, strict_in, delta in search_pool:
        if ev.kind == "message_fetch" and not cache.has_fetch_hint.get(id(step)):
            continue
        obs_norm = _get_norm_obs(cache, step)
        obs_ids = _get_obs_ids(cache, step)
        for mid in ev.fetched_msg_ids:
            mtext = msg_norm_by_id.get((ev_grp, mid), "")
            if (len(mtext) >= 6 and mtext[:64] in obs_norm) or has_id_token(
                obs_norm, mid, obs_ids=obs_ids
            ):
                cands.append(
                    (
                        (_rank_tier(ev.ts_epoch, step, strict_in), delta, step.step_id),
                        _make_link(ev, step, "msg_id", f"fetched_msg_id={mid}"),
                    )
                )
                break
    return _pick_best(cands)


def match_commit_sha(
    ev: NormalizedWorldEvent,
    window_steps: list[tuple[NormalizedAtifStep, bool, float]],
    cache: _MemberStepCache,
) -> StepEventLink | None:
    if not (ev.commit_sha and len(ev.commit_sha) >= 7):
        return None
    sha_prefix = ev.commit_sha[:7].lower()
    cands: list[tuple[tuple[int, float, int], StepEventLink]] = []
    for step, strict_in, delta in window_steps:
        if cache.read_only_step.get(id(step)):
            continue
        if sha_prefix in _get_norm_obs(cache, step):
            cands.append(
                (
                    (_rank_tier(ev.ts_epoch, step, strict_in), delta, step.step_id),
                    _make_link(ev, step, "observation_content", f"commit_sha={sha_prefix}"),
                )
            )
    return _pick_best(cands)


def match_target_tokens(
    ev: NormalizedWorldEvent,
    window_steps: list[tuple[NormalizedAtifStep, bool, float]],
    cache: _MemberStepCache,
) -> StepEventLink | None:
    tokens = _target_tokens(ev)
    if not tokens:
        return None
    cands: list[tuple[tuple[int, float, int], StepEventLink]] = []
    mutating = _is_mutating_world_event(ev)
    for step, strict_in, delta in window_steps:
        if mutating and cache.read_only_step.get(id(step)):
            continue
        args_low = cache.args_low.get(id(step), "")
        for tok_idx, token in enumerate(tokens):
            if token in args_low:
                cands.append(
                    (
                        (
                            _rank_tier(ev.ts_epoch, step, strict_in),
                            delta + tok_idx * 1e-4,
                            step.step_id,
                        ),
                        _make_link(ev, step, "tool_argument", f"arg:{token[:48]}"),
                    )
                )
                break
    return _pick_best(cands)


def match_timestamp_fallback(
    ev: NormalizedWorldEvent,
    window_steps: list[tuple[NormalizedAtifStep, bool, float]],
    cache: _MemberStepCache,
) -> StepEventLink | None:
    if ev.ts_epoch is None or not cache.timed_exec:
        return None
    best_ts: tuple[int, float, int, NormalizedAtifStep] | None = None
    mutating = _is_mutating_world_event(ev)
    for step, strict_in, delta in window_steps:
        if step.ts_epoch is None:
            continue
        if mutating and cache.read_only_step.get(id(step)):
            continue
        rank = (0 if strict_in else 1, delta, step.step_id if cache.end_stamped else -step.step_id)
        if best_ts is None or rank < best_ts[:3]:
            best_ts = (*rank, step)
    if best_ts is not None:
        step = best_ts[3]
        assert step.ts_epoch is not None
        return _make_link(
            ev, step, "timestamp_window", f"ts_delta={round(ev.ts_epoch - step.ts_epoch, 3):+.3f}s"
        )
    return None
