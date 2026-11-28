from __future__ import annotations

import gzip
import json
import re
import stat
import zlib
from pathlib import Path
from typing import Any

from ._multiagent_io import (
    NormalizedAtifStep,
    NormalizedObservation,
    _extract_message_text,
    _parse_atif_observations,
    _parse_atif_tool_calls,
    as_str,
    epoch_to_iso_utc,
    parse_finite_json_float,
    safe_finite_float,
    safe_regular_file,
)
from .world_events import NormalizedWorldEvent, WorldEventLog

_MD_SECTION_RE = re.compile(r"^###[ \t]+(\S[^\r\n]*)\r?$", re.MULTILINE)
_JSON_FENCE_OPEN_RE = re.compile(r"```json[ \t]*\r?\n")
_SEQ_INT_RE = re.compile(r'"seq"\s*:\s*(\d+)')
_FETCH_BOARD_TOOLS = frozenset({"inbox", "history", "mcp__board__inbox", "mcp__board__history"})
_MAX_STEPS = 50_000
_MAX_PUBLIC_BYTES = 64 * 1024 * 1024


def _reject_constant(_name: str) -> object:
    raise ValueError("non-finite JSON constant")


def as_finite_float(val: Any) -> float | None:
    return safe_finite_float(val)


def read_json_or_gz(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            return None, f"not_regular:{path.name}"
        if st.st_size > _MAX_PUBLIC_BYTES:
            return None, f"too_large:{path.name}"
        if path.suffix == ".gz":
            with gzip.open(path, "rb") as fh:
                raw_bytes = fh.read(_MAX_PUBLIC_BYTES + 1)
            if len(raw_bytes) > _MAX_PUBLIC_BYTES:
                return None, f"too_large:{path.name}"
            data = json.loads(
                raw_bytes.decode("utf-8", errors="replace"),
                parse_constant=_reject_constant,
                parse_float=parse_finite_json_float,
            )
        else:
            data = json.loads(
                path.read_bytes().decode("utf-8", errors="replace"),
                parse_constant=_reject_constant,
                parse_float=parse_finite_json_float,
            )
    except (OSError, ValueError, EOFError, zlib.error, RecursionError) as exc:
        return None, f"{path.name}:{exc}"
    return (data, None) if isinstance(data, dict) else (None, f"{path.name}:not_json_object")


def extract_markdown_sources(
    md_path: Path,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], list[str]]:
    try:
        st = md_path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_PUBLIC_BYTES:
            return None, {}, [f"markdown_unreadable:{md_path.name}"]
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, {}, [f"markdown_read_error:{exc}"]
    sections = list(_MD_SECTION_RE.finditer(text))
    index_obj: dict[str, Any] | None = None
    attachments: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for idx, match in enumerate(sections):
        header = match.group(1).strip()
        end = sections[idx + 1].start() if idx + 1 < len(sections) else len(text)
        section = text[match.end() : end]
        open_m = _JSON_FENCE_OPEN_RE.search(section)
        if open_m is None:
            continue
        close_idx = section.find("\n```", open_m.end())
        if close_idx < 0:
            continue
        try:
            obj = json.loads(
                section[open_m.end() : close_idx],
                parse_constant=_reject_constant,
                parse_float=parse_finite_json_float,
            )
        except (ValueError, RecursionError) as exc:
            errors.append(f"markdown_json_parse:{header}:{exc}")
            continue
        if not isinstance(obj, dict):
            continue
        if header.endswith("index.json") or ("tasks" in obj and "markHours" in obj):
            index_obj = obj
            continue
        parts = header.strip("/").split("/")
        if len(parts) >= 2:
            run_key = f"{parts[-2]}/{parts[-1]}"
            attachments[run_key] = obj
            if run_key.endswith(".gz"):
                attachments[run_key[:-3]] = obj
    return index_obj, attachments, errors


def load_public_sources(
    path: Path,
) -> tuple[dict[str, Any] | None, Path | None, dict[str, dict[str, Any]], list[str]]:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None, None, {}, [f"not_found:{path}"]
    if resolved.is_file() and resolved.suffix.lower() == ".md":
        idx, att, errs = extract_markdown_sources(resolved)
        return idx, None, att, errs
    if resolved.is_file():
        idx, err = read_json_or_gz(resolved)
        return idx, resolved.parent, {}, [err] if err else []
    if resolved.is_dir():
        idx_path = safe_regular_file(resolved, "index.json")
        if idx_path is None:
            idx_path = safe_regular_file(resolved, "public-rollout-data", "index.json")
        if idx_path is None:
            return None, resolved, {}, [f"missing:index.json under {resolved}"]
        idx, err = read_json_or_gz(idx_path)
        return idx, idx_path.parent if idx is not None else resolved, {}, [err] if err else []
    return None, None, {}, [f"not_found:{path}"]


def resolve_attachment(
    run_id: str,
    traj_path: str | None,
    base_dir: Path | None,
    attachments: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not traj_path or "\x00" in traj_path:
        return None
    fname = Path(traj_path).name
    for key in (f"{run_id}/{fname}", f"{run_id}/{fname.removesuffix('.gz')}"):
        if key in attachments:
            return attachments[key]
    if base_dir is not None:
        base_res = base_dir.resolve()
        rel = traj_path.removeprefix("public-rollout-data/").lstrip("/")
        for rel_cand in (rel, traj_path.lstrip("/"), rel.removesuffix(".gz")):
            parts = [p for p in Path(rel_cand).parts if p not in ("", ".")]
            if not parts or any(p == ".." for p in parts):
                continue
            cand = safe_regular_file(base_res, *parts)
            if cand is None:
                continue
            try:
                resolved = cand.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if not resolved.is_relative_to(base_res):
                continue
            data, _ = read_json_or_gz(cand)
            if data is not None:
                return data
    return None


def _next_step_id(raw_sid: object, idx: int, prev_sid: int | None, zero_based: bool) -> int:
    if type(raw_sid) is int and raw_sid >= 0:
        return raw_sid
    base = idx - 1 if zero_based else idx
    return max(base, prev_sid + 1 if prev_sid is not None else base)


def _uses_zero_based_ids(items: list[Any]) -> bool:
    explicit = [r.get("step_id") for r in items if isinstance(r, dict)]
    if any(type(v) is int and v == 0 for v in explicit):
        return True
    return (
        bool(explicit)
        and explicit[0] is None
        and any(type(v) is int and v == 1 for v in explicit[1:])
    )


def parse_public_agent_steps(member_id: str, payload: dict[str, Any]) -> list[NormalizedAtifStep]:
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return []
    sliced = raw_steps[:_MAX_STEPS]
    zero_based = _uses_zero_based_ids(sliced)
    out: list[NormalizedAtifStep] = []
    prev_sid: int | None = None
    for idx, raw in enumerate(sliced, start=1):
        if not isinstance(raw, dict):
            continue
        step_id = _next_step_id(raw.get("step_id"), idx, prev_sid, zero_based)
        prev_sid = step_id
        ts_iso, ts_epoch = epoch_to_iso_utc(raw.get("timestamp"))
        issues: list[str] = []
        tcs = _parse_atif_tool_calls(raw.get("tool_calls"), issues)
        obs_list = [o for o in _parse_atif_observations(raw.get("observation")) if o.content]
        src = as_str(raw.get("source")) or "agent"
        msg = _extract_message_text(raw.get("message"))
        if src == "tool" and not obs_list and msg:
            tc_id = as_str(raw.get("tool_call_id")) or as_str(raw.get("source_call_id"))
            if tc_id is not None:
                obs_list.append(NormalizedObservation(source_call_id=tc_id, content=msg))
        out.append(
            NormalizedAtifStep(
                member_id=member_id,
                step_id=step_id,
                source=src,
                timestamp=ts_iso,
                ts_epoch=ts_epoch,
                message=msg,
                reasoning_content=as_str(raw.get("reasoning_content")),
                tool_calls=tcs,
                observations=obs_list,
            )
        )
    return out


StagedPublicEvent = tuple[int, float, int, int, dict[str, Any]]


def _public_post_routing(
    raw_to_val: Any, member_aliases: dict[str, str]
) -> tuple[str, list[str], str]:
    if isinstance(raw_to_val, list):
        recips = [
            member_aliases.get(clean, clean)
            for item in raw_to_val
            if (clean := as_str(item)) is not None and clean.lower() not in ("everyone", "all", "*")
        ]
        if not recips:
            return "public", [], "board:public"
        if len(recips) == 1:
            return "direct", recips, f"dm:{recips[0]}"
        return "multi", recips, f"dm:{','.join(recips)}"
    raw_to = as_str(raw_to_val)
    if not raw_to or raw_to.lower() in ("everyone", "all", "*"):
        return "public", [], "board:public"
    canon_to = member_aliases.get(raw_to, raw_to)
    return "direct", [canon_to], f"dm:{canon_to}"


_BoardPostStage = tuple[
    list[StagedPublicEvent],
    set[str],
    dict[str, float],
    dict[str, tuple[int, float]],
    dict[str, set[str] | None],
]


def _stage_public_board_posts(
    board_payload: dict[str, Any], member_aliases: dict[str, str], *, comms_disabled: bool
) -> _BoardPostStage:
    staged: list[StagedPublicEvent] = []
    valid_post_ids: set[str] = set()
    post_epochs: dict[str, float] = {}
    post_sort_keys: dict[str, tuple[int, float]] = {}
    post_participants: dict[str, set[str] | None] = {}
    sliced_posts = board_payload["steps"][:_MAX_STEPS]
    zero_based, prev_sid = _uses_zero_based_ids(sliced_posts), None
    for idx, raw in enumerate(sliced_posts, start=1):
        if not isinstance(raw, dict):
            continue
        prev_sid = _next_step_id(raw.get("step_id"), idx, prev_sid, zero_based)
        bid, raw_src = str(prev_sid), as_str(raw.get("source"))
        actor = member_aliases.get(raw_src, raw_src) if raw_src else None
        ts_iso, ts_epoch = epoch_to_iso_utc(raw.get("timestamp"))
        raw_kind = as_str(raw.get("kind")) or "board"
        is_refusal = comms_disabled or raw_kind in ("refused", "policy_refusal", "rate_limited")
        visibility, recipients, target = _public_post_routing(raw.get("to"), member_aliases)
        detail: dict[str, Any] = {"facing_actor": raw_src} if raw_src and raw_src != actor else {}
        if (minute := as_finite_float(raw.get("minute"))) is not None:
            detail["minute"] = minute
        p_tier = 0 if ts_epoch is not None else 1
        p_ts = ts_epoch if ts_epoch is not None else float(idx)
        if not is_refusal:
            valid_post_ids.add(bid)
            if ts_epoch is not None:
                post_epochs[bid] = ts_epoch
            prev_pk = post_sort_keys.get(bid)
            if prev_pk is None or (p_tier, p_ts) > prev_pk:
                post_sort_keys[bid] = (p_tier, p_ts)
            if not recipients:
                post_participants[bid] = None
            elif bid not in post_participants or post_participants[bid] is not None:
                parts = post_participants.setdefault(bid, set())
                assert parts is not None
                if actor is not None:
                    parts.add(actor)
                parts.update(recipients)
        row = {
            "ts": ts_iso,
            "ts_epoch": ts_epoch,
            "actor": actor,
            "kind": "policy_refusal" if is_refusal else "message_post",
            "target": target,
            "msg_id": None if is_refusal else bid,
            "recipients": recipients,
            "visibility": "world_audit" if is_refusal else visibility,
            "accepted": not is_refusal,
            "phase": "play",
            "source_format": "bespoke_public_board",
            "raw_kind": raw_kind,
            "text": as_str(raw.get("message")),
            "authenticated": actor is not None,
            "detail": detail,
        }
        staged.append((p_tier, p_ts, 0, idx, row))
    return staged, valid_post_ids, post_epochs, post_sort_keys, post_participants


def _build_public_fetch_event(
    member_id: str,
    step: NormalizedAtifStep,
    rk: str,
    found_ids: list[str],
    post_sort_keys: dict[str, tuple[int, float]],
    fetch_ord: int,
) -> StagedPublicEvent:
    f_key = (
        0 if step.ts_epoch is not None else 2,
        step.ts_epoch if step.ts_epoch is not None else float(fetch_ord),
    )
    for mid in found_ids:
        if mid in post_sort_keys and post_sort_keys[mid] > f_key:
            f_key = post_sort_keys[mid]
    row = {
        "ts": step.timestamp,
        "ts_epoch": step.ts_epoch,
        "actor": member_id,
        "kind": "message_fetch",
        "target": rk,
        "visibility": "world_audit",
        "accepted": True,
        "phase": "play",
        "source_format": "bespoke_public_board",
        "raw_kind": rk,
        "fetched_msg_ids": found_ids,
        "authenticated": True,
    }
    return (f_key[0], f_key[1], 1, fetch_ord, row)


def _stage_step_board_fetches(
    member_id: str,
    step: NormalizedAtifStep,
    *,
    has_board_payload: bool,
    valid_post_ids: set[str],
    post_epochs: dict[str, float],
    post_sort_keys: dict[str, tuple[int, float]],
    post_participants: dict[str, set[str] | None],
    fetch_ord: int,
) -> tuple[list[StagedPublicEvent], int]:
    fetch_tcs = [tc for tc in step.tool_calls if tc.function_name.lower() in _FETCH_BOARD_TOOLS]
    if not fetch_tcs or not step.observations:
        return [], fetch_ord
    has_call_ids = any(obs.source_call_id is not None for obs in step.observations)
    obs_by_call_id: dict[str, list[Any]] = {}
    if has_call_ids:
        for obs in step.observations:
            if obs.source_call_id is not None:
                obs_by_call_id.setdefault(obs.source_call_id, []).append(obs)
    seen_call_ids: set[str] = set()
    shared_blob_used = False
    out: list[StagedPublicEvent] = []
    for fetch_tc in fetch_tcs:
        if has_call_ids and fetch_tc.tool_call_id is not None:
            if fetch_tc.tool_call_id in seen_call_ids:
                continue
            seen_call_ids.add(fetch_tc.tool_call_id)
            matched_obs = obs_by_call_id.get(fetch_tc.tool_call_id, [])
        else:
            if shared_blob_used:
                continue
            shared_blob_used = True
            matched_obs = (
                [obs for obs in step.observations if obs.source_call_id is None]
                if has_call_ids
                else step.observations
            )
        obs_text = "\n".join(obs.content for obs in matched_obs if obs.content)
        found_ids = list(dict.fromkeys(_SEQ_INT_RE.findall(obs_text)))
        if has_board_payload:
            found_ids = [
                mid
                for mid in found_ids
                if mid in valid_post_ids
                and (
                    mid not in post_epochs
                    or step.ts_epoch is None
                    or post_epochs[mid] <= step.ts_epoch + 2.0
                )
                and ((parts := post_participants.get(mid)) is None or member_id in parts)
            ]
        if not found_ids:
            continue
        fetch_ord += 1
        rk = fetch_tc.function_name.lower().rsplit("__", 1)[-1]
        out.append(
            _build_public_fetch_event(member_id, step, rk, found_ids, post_sort_keys, fetch_ord)
        )
    return out, fetch_ord


def _with_paired_tool_obs(steps: list[NormalizedAtifStep], i: int) -> NormalizedAtifStep:
    step = steps[i]
    if step.observations or not step.tool_calls:
        return step
    extra: list[NormalizedObservation] = []
    j = i + 1
    while j < len(steps) and steps[j].source == "tool":
        t_st = steps[j]
        if t_st.observations:
            extra.extend(t_st.observations)
        elif t_st.message:
            extra.append(NormalizedObservation(content=t_st.message))
        j += 1
    return step.model_copy(update={"observations": extra}) if extra else step


def build_public_world_log(
    board_payload: dict[str, Any] | None,
    member_steps: dict[str, list[NormalizedAtifStep]],
    member_aliases: dict[str, str],
    *,
    comms_disabled: bool = False,
) -> WorldEventLog:
    has_board_payload = isinstance(board_payload, dict) and isinstance(
        board_payload.get("steps"), list
    )
    if has_board_payload:
        assert isinstance(board_payload, dict)
        staged, valid_post_ids, post_epochs, post_sort_keys, post_participants = (
            _stage_public_board_posts(board_payload, member_aliases, comms_disabled=comms_disabled)
        )
    else:
        staged = []
        valid_post_ids = set()
        post_epochs = {}
        post_sort_keys = {}
        post_participants = {}

    fetch_ord = 0
    for member_id, steps in sorted(member_steps.items()) if not comms_disabled else ():
        for s_idx in range(len(steps)):
            fetches, fetch_ord = _stage_step_board_fetches(
                member_id,
                _with_paired_tool_obs(steps, s_idx),
                has_board_payload=has_board_payload,
                valid_post_ids=valid_post_ids,
                post_epochs=post_epochs,
                post_sort_keys=post_sort_keys,
                post_participants=post_participants,
                fetch_ord=fetch_ord,
            )
            staged.extend(fetches)

    staged.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    events = [
        NormalizedWorldEvent(seq=seq, **row) for seq, (_, _, _, _, row) in enumerate(staged, 1)
    ]
    return WorldEventLog(
        event_log_present=board_payload is not None or bool(events),
        source_formats=["bespoke_public_board"] if events else [],
        events=events,
    )
