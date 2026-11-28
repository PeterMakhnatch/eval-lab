from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass

from ._forensics_rules import (
    BASH_TOOLS,
    FILE_READ_TOOLS,
    extract_id_tokens,
    has_id_token,
    is_simple_id_token,
    normalize_repo_path,
    normalize_text,
    shell_command_text,
    short_tool_name,
    step_observation_corpus,
)
from ._forensics_shell import extract_bash_read_paths as _extract_bash_read_paths
from ._multiagent_io import NormalizedAtifStep
from .world_events import NormalizedWorldEvent

_MAX_TS_WINDOW_SEC = 180.0
_CLOCK_SKEW_SEC = 2.0


def _step_end_epochs_by_obj(steps: list[NormalizedAtifStep]) -> dict[int, float | None]:
    timed = sorted(
        (s for s in steps if s.ts_epoch is not None),
        key=lambda s: (s.ts_epoch or 0.0, s.step_id),
    )
    return {
        id(step): (timed[idx + 1].ts_epoch if idx + 1 < len(timed) else None)
        for idx, step in enumerate(timed)
    }


@dataclass
class _MemberContextIndex:
    elig_steps: list[NormalizedAtifStep]
    obs_ends: list[float | None]
    norm_corpora: list[str]
    obs_ids_list: list[frozenset[str]]
    steps_by_obs_id: dict[str, list[int]]
    step_id_to_indices: dict[int, list[int]]
    all_obs_joined: str
    char_starts: list[int]
    char_ends: list[int]
    monotonic_ends: list[float] | None


def _build_member_context_index(steps: list[NormalizedAtifStep]) -> _MemberContextIndex:
    end_epochs = _step_end_epochs_by_obj(steps)
    elig_steps: list[NormalizedAtifStep] = []
    obs_ends: list[float | None] = []
    norm_corpora: list[str] = []
    obs_ids_list: list[frozenset[str]] = []
    steps_by_obs_id: dict[str, list[int]] = defaultdict(list)
    step_id_to_indices: dict[int, list[int]] = defaultdict(list)
    char_starts: list[int] = []
    char_ends: list[int] = []
    cursor = 0
    for step in steps:
        if step.source == "system":
            continue
        if (
            step.source == "user"
            and step.ts_epoch is None
            and step.step_id <= 2
            and not step.observations
        ):
            continue
        raw_c = step_observation_corpus(step)
        if not raw_c.strip():
            continue
        nc = normalize_text(raw_c)
        if not nc:
            continue
        k = len(elig_steps)
        elig_steps.append(step)
        if step.ts_epoch is None:
            obs_ends.append(None)
        else:
            nxt = end_epochs.get(id(step))
            obs_ends.append(nxt if nxt is not None else step.ts_epoch + _MAX_TS_WINDOW_SEC)
        norm_corpora.append(nc)
        ids = extract_id_tokens(nc)
        obs_ids_list.append(ids)
        for tok in ids:
            steps_by_obs_id[tok].append(k)
        step_id_to_indices[step.step_id].append(k)
        char_starts.append(cursor)
        cursor += len(nc)
        char_ends.append(cursor)
        cursor += 1
    monotonic_ends: list[float] | None = None
    if obs_ends and all(v is not None for v in obs_ends):
        vals = [v for v in obs_ends if v is not None]
        if all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)):
            monotonic_ends = vals
    return _MemberContextIndex(
        elig_steps=elig_steps,
        obs_ends=obs_ends,
        norm_corpora=norm_corpora,
        obs_ids_list=obs_ids_list,
        steps_by_obs_id=dict(steps_by_obs_id),
        step_id_to_indices=dict(step_id_to_indices),
        all_obs_joined="\x00".join(norm_corpora),
        char_starts=char_starts,
        char_ends=char_ends,
        monotonic_ends=monotonic_ends,
    )


def step_end_epochs(steps: list[NormalizedAtifStep]) -> dict[int, float | None]:
    timed = sorted(
        (s for s in steps if s.ts_epoch is not None),
        key=lambda s: (s.ts_epoch or 0.0, s.step_id),
    )
    return {
        step.step_id: (timed[idx + 1].ts_epoch if idx + 1 < len(timed) else None)
        for idx, step in enumerate(timed)
    }


def message_in_step_context(
    msg: NormalizedWorldEvent,
    step: NormalizedAtifStep,
    *,
    next_step_epoch: float | None = None,
    fetch_epoch: float | None = None,
    is_linked_fetch_step: bool = False,
    norm_corpus: str | None = None,
    norm_msg_text: str | None = None,
) -> bool:
    if step.source == "system":
        return False
    if (
        step.source == "user"
        and step.ts_epoch is None
        and step.step_id <= 2
        and not step.observations
    ):
        return False
    earliest_visible_ts = fetch_epoch if fetch_epoch is not None else msg.ts_epoch
    if earliest_visible_ts is not None and step.ts_epoch is not None:
        obs_end = (
            next_step_epoch if next_step_epoch is not None else step.ts_epoch + _MAX_TS_WINDOW_SEC
        )
        if obs_end < earliest_visible_ts - _CLOCK_SKEW_SEC:
            return False

    if norm_corpus is None:
        corpus = step_observation_corpus(step)
        if not corpus.strip():
            return False
        norm_corpus = normalize_text(corpus)
    elif not norm_corpus:
        return False
    norm_text = norm_msg_text if norm_msg_text is not None else normalize_text(msg.text or "")
    if len(norm_text) >= 6:
        needle = norm_text[:80] if len(norm_text) > 80 else norm_text
        if needle in norm_corpus:
            return True
    mid = msg.msg_id or ""
    senders = [
        s.lower()
        for s in (msg.actor, msg.detail.get("facing_actor"))
        if isinstance(s, str) and s.strip()
    ]
    sender_hit = any(s in norm_corpus for s in senders)
    id_hit = has_id_token(norm_corpus, mid)
    if id_hit and sender_hit:
        return True
    return bool(is_linked_fetch_step and (sender_hit or id_hit))


def _time_ok_at(idx: _MemberContextIndex, k: int, min_end: float | None) -> bool:
    if min_end is None:
        return True
    oe = idx.obs_ends[k]
    return oe is None or oe >= min_end


def _match_needle_index(
    idx: _MemberContextIndex,
    needle: str,
    start_k: int,
    min_end: float | None,
) -> int | None:
    n_steps = len(idx.elig_steps)
    if "\x00" not in needle:
        pos = idx.char_starts[start_k]
        for _ in range(n_steps):
            hit = idx.all_obs_joined.find(needle, pos)
            if hit == -1:
                break
            k = bisect_right(idx.char_ends, hit)
            if k >= n_steps:
                break
            if hit + len(needle) <= idx.char_ends[k] and _time_ok_at(idx, k, min_end):
                return k
            pos = idx.char_ends[k] + 1
        return None
    for k in range(start_k, n_steps):
        if _time_ok_at(idx, k, min_end) and needle in idx.norm_corpora[k]:
            return k
    return None


def _match_sender_id_index(
    idx: _MemberContextIndex,
    senders: list[str],
    mid: str,
    start_k: int,
    min_end: float | None,
    best_k: int | None,
) -> int | None:
    mid_low = mid.lower()
    if not (mid_low and senders):
        return best_k
    if is_simple_id_token(mid_low):
        for k in idx.steps_by_obs_id.get(mid_low, ()):
            if k < start_k:
                continue
            if best_k is not None and k >= best_k:
                break
            if _time_ok_at(idx, k, min_end) and any(s in idx.norm_corpora[k] for s in senders):
                return k
        return best_k
    limit_k = best_k if best_k is not None else len(idx.elig_steps)
    for k in range(start_k, limit_k):
        nc = idx.norm_corpora[k]
        if _time_ok_at(idx, k, min_end) and any(s in nc for s in senders) and has_id_token(nc, mid):
            return k
    return best_k


def _first_matching_index(
    idx: _MemberContextIndex,
    msg: NormalizedWorldEvent,
    norm_msg: str,
    fetch_epoch: float | None,
    linked_step: int | None,
) -> int | None:
    n_steps = len(idx.elig_steps)
    if n_steps == 0:
        return None
    earliest_ts = fetch_epoch if fetch_epoch is not None else msg.ts_epoch
    min_end = earliest_ts - _CLOCK_SKEW_SEC if earliest_ts is not None else None
    start_k = (
        bisect_left(idx.monotonic_ends, min_end)
        if (min_end is not None and idx.monotonic_ends is not None)
        else 0
    )
    if start_k >= n_steps:
        return None

    best_k: int | None = None
    if len(norm_msg) >= 6:
        needle = norm_msg[:80] if len(norm_msg) > 80 else norm_msg
        best_k = _match_needle_index(idx, needle, start_k, min_end)

    senders = [
        s.lower()
        for s in (msg.actor, msg.detail.get("facing_actor"))
        if isinstance(s, str) and s.strip()
    ]
    mid = msg.msg_id or ""
    mid_low = mid.lower()
    best_k = _match_sender_id_index(idx, senders, mid, start_k, min_end, best_k)

    if linked_step is not None:
        for k in idx.step_id_to_indices.get(linked_step, ()):
            if k < start_k:
                continue
            if best_k is not None and k >= best_k:
                break
            if not _time_ok_at(idx, k, min_end):
                continue
            nc = idx.norm_corpora[k]
            if any(s in nc for s in senders) or (
                bool(mid_low) and has_id_token(nc, mid, obs_ids=idx.obs_ids_list[k])
            ):
                best_k = k
                break
    return best_k


def find_in_context_steps(
    posted_by_id: dict[tuple[str, str], NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    steps_by_member: dict[str, list[NormalizedAtifStep]],
    event_to_step: dict[tuple[str, int], int],
) -> dict[tuple[str, str, str], int]:
    idx_by_member: dict[str, _MemberContextIndex] = {}
    norm_msg_cache = {key: normalize_text(msg.text or "") for key, msg in posted_by_id.items()}
    result: dict[tuple[str, str, str], int] = {}
    for (recipient, grp, msg_id), fetch_ev in sorted(first_fetch.items()):
        msg = posted_by_id.get((grp, msg_id))
        if msg is None:
            continue
        idx = idx_by_member.get(recipient)
        if idx is None:
            steps = steps_by_member.get(recipient, [])
            idx = _build_member_context_index(steps)
            idx_by_member[recipient] = idx
        linked_step = event_to_step.get((fetch_ev.source_format, fetch_ev.seq))
        k = _first_matching_index(
            idx, msg, norm_msg_cache.get((grp, msg_id), ""), fetch_ev.ts_epoch, linked_step
        )
        if k is not None:
            result[(recipient, grp, msg_id)] = idx.elig_steps[k].step_id
    return result


def extract_timed_atif_file_reads(
    steps_by_member: dict[str, list[NormalizedAtifStep]],
) -> list[tuple[float, str, str]]:
    reads: list[tuple[float, str, str]] = []
    for member_id, steps in sorted(steps_by_member.items()):
        for step in steps:
            if step.ts_epoch is None:
                continue
            for tc in step.tool_calls:
                fn = short_tool_name(tc.function_name)
                if fn in FILE_READ_TOOLS or (
                    fn == "str_replace_editor" and str(tc.arguments.get("command") or "") == "view"
                ):
                    for key in ("path", "file_path", "filename", "file"):
                        val = tc.arguments.get(key)
                        if isinstance(val, str) and val.strip():
                            reads.append((step.ts_epoch, member_id, normalize_repo_path(val)))
                elif fn in BASH_TOOLS:
                    cmd = shell_command_text(tc.arguments)
                    for match in _extract_bash_read_paths(cmd):
                        reads.append((step.ts_epoch, member_id, normalize_repo_path(match)))
    reads.sort()
    return reads
