from __future__ import annotations

import math
import stat
from pathlib import Path
from typing import Any

from . import _multiagent_io as _io
from ._multiagent_io import (
    as_dict,
    as_nonneg_float,
    as_nonneg_int,
    as_str,
    safe_regular_file,
    valid_member_name,
)

_COMMS_OFF_POLICIES = frozenset({"nomessagespolicy", "no_messages_policy", "silentpolicy"})


def is_regular_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def safe_iterdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def _cache_tokens(
    toks: dict[str, Any], keys: tuple[str, ...] = ("cache_read", "cache_write", "cache_write_1h")
) -> int | None:
    vals = [v for k in keys if (v := as_nonneg_int(toks.get(k))) is not None]
    return sum(vals) if vals else None


def _has_unpriced(value: object) -> bool:
    stack: list[object] = [value]
    for _ in range(256):
        if not stack:
            return False
        cur = stack.pop()
        if cur is True or (isinstance(cur, list) and bool(cur)):
            return True
        if not isinstance(cur, bool) and isinstance(cur, (int, float)) and math.isfinite(cur) and cur > 0:
            return True
        if isinstance(cur, dict):
            vals = list(cur.values())[:256]
            stack.extend(vals[: max(0, 256 - len(stack))])
    return False


def _usage_from_proxy_rows(
    p_rows: list[dict[str, Any]],
) -> tuple[
    int | None, int | None, int | None, float | None, str | None, _io.CostStatus, int, int, int
]:
    usd_vals: list[float] = []
    in_sum = ca_sum = ou_sum = n_rl = n_err = 0
    has_in = has_ca = has_ou = partial = False
    for row in p_rows:
        status_code = row.get("status")
        reason = as_str(row.get("reason"))
        if reason in ("rate_limit", "subscription_rate_limit"):
            n_rl += 1
        if reason == "stream_error" or (type(status_code) is int and status_code >= 500):
            n_err += 1
        toks = as_dict(row.get("tokens"))
        v_in = as_nonneg_int(toks.get("input"))
        if v_in is None:
            v_in = as_nonneg_int(row.get("input_tokens"))
        if v_in is not None:
            has_in, in_sum = True, in_sum + v_in
        v_ou = as_nonneg_int(toks.get("output"))
        if v_ou is None:
            v_ou = as_nonneg_int(row.get("output_tokens"))
        if v_ou is not None:
            has_ou, ou_sum = True, ou_sum + v_ou
        v_ca = _cache_tokens(toks) if toks else None
        if v_ca is None:
            v_ca = as_nonneg_int(row.get("cache_tokens"))
        if v_ca is None:
            v_ca = _cache_tokens(
                row, ("cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens")
            )
        if v_ca is not None:
            has_ca, ca_sum = True, ca_sum + v_ca
        usd = as_nonneg_float(row.get("usd"))
        if usd is None:
            usd = as_nonneg_float(row.get("cost_usd"))
        if usd is not None:
            usd_vals.append(usd)
        elif status_code == 200:
            partial = True
    n_in = in_sum if has_in else None
    n_ca = ca_sum if has_ca else None
    n_ou = ou_sum if has_ou else None
    cost: float | None = None
    cost_source: str | None = None
    cost_status: _io.CostStatus = "missing"
    if usd_vals:
        total_u = _io.safe_fsum(usd_vals)
        cost_source = "proxy_jsonl"
        if total_u is not None:
            cost, cost_status = round(total_u, 9), ("partial" if partial else "recorded")
        else:
            cost_status = "partial"
    elif partial:
        cost_status, cost_source = "partial", "proxy_jsonl"
    return n_in, n_ca, n_ou, cost, cost_source, cost_status, len(p_rows), n_rl, n_err


def _usage_from_summary(
    src_map: dict[str, Any], src_name: str, *, top_unpriced: bool = False
) -> tuple[
    int | None, int | None, int | None, int | None, float | None, str | None, _io.CostStatus
]:
    toks = as_dict(src_map.get("tokens"))
    n_in, n_ou = as_nonneg_int(toks.get("input")), as_nonneg_int(toks.get("output"))
    n_ca = _cache_tokens(toks) if toks else None
    n_reqs = as_nonneg_int(src_map.get("requests"))
    raw_u = src_map.get("usd") if src_map.get("usd") is not None else src_map.get("cost_usd")
    unp = _has_unpriced(src_map.get("unpriced")) or top_unpriced
    cost = None if unp else as_nonneg_float(raw_u)
    cost_status: _io.CostStatus = "partial" if unp else ("recorded" if cost is not None else "missing")
    cost_source = src_name if (cost is not None or cost_status == "partial") else None
    return n_in, n_ca, n_ou, n_reqs, cost, cost_source, cost_status


def _bespoke_usage_for_member(
    p_rows: list[dict[str, Any]],
    p_sum: dict[str, Any],
    s_sum: dict[str, Any],
    mrec: dict[str, Any],
    f_metrics: dict[str, Any],
    *,
    is_unbounded_sub: bool = False,
    top_unpriced: bool = False,
    is_known_no_model: bool = False,
    meta_cost_source: str | None = None,
) -> _io.MemberUsage:
    n_in = n_ca = n_ou = n_reqs = None
    cost: float | None = None
    cost_source: str | None = None
    cost_status: _io.CostStatus = "missing"
    n_rl = n_err = 0
    if p_rows:
        n_in, n_ca, n_ou, cost, cost_source, cost_status, n_reqs, n_rl, n_err = (
            _usage_from_proxy_rows(p_rows)
        )
    elif p_sum or s_sum:
        src_map, src_name = (p_sum, "proxy_json") if p_sum else (s_sum, "subscription_json")
        n_in, n_ca, n_ou, n_reqs, cost, cost_source, cost_status = _usage_from_summary(
            src_map, src_name, top_unpriced=top_unpriced
        )
    elif mrec:
        n_in = as_nonneg_int(mrec.get("n_input_tokens"))
        n_ca = as_nonneg_int(mrec.get("n_cache_tokens"))
        n_ou = as_nonneg_int(mrec.get("n_output_tokens"))
        raw_c = as_nonneg_float(mrec.get("cost_usd"))
        fake0 = raw_c == 0.0 and meta_cost_source == "trajectories" and (n_in or 0) > 0 and not is_known_no_model
        if raw_c is not None and not fake0:
            cost, cost_source, cost_status = raw_c, "result_metadata", "recorded"

    n_in = n_in if n_in is not None else as_nonneg_int(f_metrics.get("total_prompt_tokens"))
    n_ca = n_ca if n_ca is not None else as_nonneg_int(f_metrics.get("total_cached_tokens"))
    n_ou = n_ou if n_ou is not None else as_nonneg_int(f_metrics.get("total_completion_tokens"))
    if is_unbounded_sub:
        cost, cost_source, cost_status = None, None, "missing"
    elif (
        cost is None
        and cost_status == "missing"
        and (fb := as_nonneg_float(f_metrics.get("total_cost_usd"))) is not None
    ):
        cost, cost_source, cost_status = fb, "trajectory_fallback", "recorded"
    elif cost is None and cost_status == "missing" and is_known_no_model:
        cost_status, cost_source = "zero_no_model", "none_no_model"
    exp = {
        "n_input_tokens": n_in,
        "n_cache_tokens": n_ca,
        "n_output_tokens": n_ou,
        "cost_usd": cost,
    }
    return _io.MemberUsage(
        n_input_tokens=n_in,
        n_cache_tokens=n_ca,
        n_output_tokens=n_ou,
        cost_usd=cost,
        cost_status=cost_status,
        cost_source=cost_source,
        n_model_requests=n_reqs,
        n_proxy_rate_limited=n_rl,
        n_proxy_errors=n_err,
        usage_mismatches=_io.usage_mismatches(exp, f_metrics),
    )


def _read_mark_dir_reward(root: Path, label: str) -> float | None:
    rj, err = _io.read_json_object(safe_regular_file(root, "marks", label, "reward.json"))
    if err is None and rj is not None:
        val = _io.safe_finite_float(rj.get("reward"))
        if val is not None:
            return val
    rt = safe_regular_file(root, "marks", label, "reward.txt")
    if rt is None:
        return None
    try:
        if rt.lstat().st_size > 65_536:
            return None
        return _io.safe_finite_float(float(rt.read_text("utf-8", "replace").strip()))
    except (OSError, ValueError, OverflowError):
        return None


def _parse_grade_marks(root: Path, meta: dict[str, Any]) -> list[_io.GradeMarkRecord]:
    out: list[_io.GradeMarkRecord] = []
    seen_labels: set[str] = set()
    marks_root = root / "marks"
    marks_root_ok = is_regular_dir(marks_root)
    raw_marks = meta.get("marks")
    for item in (raw_marks if isinstance(raw_marks, list) else ())[:500]:
        rec = as_dict(item)
        if not (label := as_str(rec.get("mark"))):
            continue
        seen_labels.add(label)
        safe_lbl = valid_member_name(label)
        dir_present = (
            marks_root_ok and safe_lbl is not None and is_regular_dir(marks_root / safe_lbl)
        )
        reward = _io.safe_finite_float(rec.get("reward"))
        if reward is None and dir_present and safe_lbl is not None:
            reward = _read_mark_dir_reward(root, safe_lbl)
        out.append(
            _io.GradeMarkRecord(
                mark=label,
                mark_s=as_nonneg_float(rec.get("mark_s")),
                elapsed_s=as_nonneg_float(rec.get("elapsed_s")),
                commit=as_str(rec.get("commit")),
                reward=reward,
                verifier_s=as_nonneg_float(rec.get("verifier_s")),
                status=as_str(rec.get("status")),
                final=rec.get("final") is True,
                ended_early=rec.get("ended_early") is True,
                mark_dir_present=dir_present,
            )
        )
    for child in safe_iterdir(marks_root)[:500] if marks_root_ok else ():
        safe_lbl = valid_member_name(child.name)
        if safe_lbl is not None and is_regular_dir(child) and safe_lbl not in seen_labels:
            out.append(
                _io.GradeMarkRecord(
                    mark=safe_lbl,
                    reward=_read_mark_dir_reward(root, safe_lbl),
                    status="from_marks_dir",
                    mark_dir_present=True,
                )
            )
    return out


def _detect_board_policy(board_rows: list[dict[str, Any]]) -> tuple[str | None, bool]:
    policy_name: str | None = None
    send_count = refused_sends = 0
    refused_comms_off = False
    for ev in board_rows:
        ev_name = ev.get("event") or ev.get("type")
        if ev_name == "config":
            policy_name = as_str(ev.get("policy")) or policy_name
        elif ev_name in ("send", "post"):
            send_count += 1
        elif ev_name == "refused" and ev.get("action") == "send":
            refused_sends += 1
            reason = (as_str(ev.get("reason")) or "").lower()
            refused_comms_off = refused_comms_off or (
                "messaging is off" in reason or "refuses every message" in reason
            )
    pol_low = (policy_name or "").lower().replace("-", "").replace("_", "")
    comms_off = (
        any(tok in pol_low for tok in ("nomessages", "silent"))
        or (policy_name or "").lower() in _COMMS_OFF_POLICIES
        or (refused_comms_off and send_count == 0 and refused_sends > 0)
    )
    return policy_name, comms_off
