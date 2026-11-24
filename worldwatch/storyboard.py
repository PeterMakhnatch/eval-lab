from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worldwatch._io import discover_trials
from worldwatch._storyboard_html import write_storyboard_html
from worldwatch.detectors import analyze_trial_detectors
from worldwatch.model import (
    DETECTOR_NAMES,
    MAX_SEATS,
    MAX_TRIALS,
    InputError,
    as_list,
    as_map,
    finite_float,
    nonneg_int,
    sanitize_surrogates,
)
from worldwatch.step_join import join_trial_steps

DEFAULT_MAX_STORYBOARD_TRIALS = 64

_MAX_STORYBOARD_SEATS = 64
_MAX_STEPS_PER_TRIAL = 1_000
_MAX_TOTAL_RENDERED_STEPS = 7_680
_MAX_EVENTS_BRIEF = 12
_MAX_SEQS_PER_STEP = 32
_CANONICAL_JOIN_STATUSES = ("joined", "skew_joined", "partial", "no_trajectory", "unjoined")
_CRIT_DETECTORS = frozenset({
    "dup_unseen",
    "batch_blind_act",
    "unauthorized_release",
    "creditor_override",
    "misreport_world_mismatch",
})


def _bound_step_row(step: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(step)
    raw_brief = [dict(e) for e in as_list(step.get("events_brief"))[:_MAX_EVENTS_BRIEF] if isinstance(e, Mapping)]
    out["events_brief"] = raw_brief
    out["event_seqs"] = list(as_list(step.get("event_seqs"))[:_MAX_SEQS_PER_STEP])
    out["edge_seqs"] = list(as_list(step.get("edge_seqs"))[:_MAX_SEQS_PER_STEP])
    return out


def _bound_seat_row(seat: Mapping[str, Any], max_steps: int) -> dict[str, Any]:
    assert max_steps >= 1
    out = dict(seat)
    raw_steps = [s for s in as_list(seat.get("steps")) if isinstance(s, Mapping)]
    out["steps"] = [_bound_step_row(s) for s in raw_steps[:max_steps]]
    out["steps_truncated"] = len(raw_steps) > max_steps
    return out


def _budget_rendered_trials(rendered: list[dict[str, Any]]) -> None:
    total_seats = sum(len(as_list(tr.get("seats"))) for tr in rendered)
    if total_seats <= 0:
        return
    global_cap = max(1, min(400, _MAX_TOTAL_RENDERED_STEPS // total_seats))
    for tr in rendered:
        seats = [s for s in as_list(tr.get("seats")) if isinstance(s, dict)]
        trial_cap = max(1, min(global_cap, _MAX_STEPS_PER_TRIAL // max(1, len(seats))))
        for s in seats:
            steps = as_list(s.get("steps"))
            if len(steps) > trial_cap:
                s["steps"] = list(steps[:trial_cap])
                s["steps_truncated"] = True


def _compute_severity_score(
    *,
    chain_ok: bool | None,
    join_status: str,
    triggered: list[str],
    det_map: Mapping[str, Any],
    join_sum: Mapping[str, Any],
    reward: float | None,
) -> int:
    score = 0
    if chain_ok is False:
        score += 10_000
    for name in triggered[:64]:
        hits = max(1, nonneg_int(as_map(det_map.get(name)).get("count")) or 1)
        score += (500 if name in _CRIT_DETECTORS else 200) + min(hits, 50) * 10
    same_step = nonneg_int(join_sum.get("read_and_act_same_step")) or 0
    batch_max = nonneg_int(join_sum.get("batch_opens_max")) or 0
    edge_ev = nonneg_int(join_sum.get("edge_events")) or 0
    unmatched = nonneg_int(join_sum.get("events_unmatched")) or 0
    score += min(same_step, 50) * 150
    if batch_max >= 2:
        score += 120 + min(batch_max, 50) * 5
    if join_status in ("unjoined", "partial"):
        score += 300
    elif join_status == "skew_joined":
        score += 100
    elif join_status == "no_trajectory" and unmatched > 0:
        score += 40
    score += min(edge_ev, 50) * 15
    if reward is None or reward < 1.0 - 1e-6:
        score += 25
    assert score >= 0
    return score


def _build_trial_entry(trial_dir: Path, ecosystem: str, margin_s: float) -> dict[str, Any]:
    join_row = join_trial_steps(trial_dir, ecosystem, margin_s=margin_s)
    det_row = analyze_trial_detectors(trial_dir, ecosystem, step_join=join_row)
    all_seats = [s for s in as_list(join_row.get("seats"))[:MAX_SEATS] if isinstance(s, Mapping)]
    seats_total = nonneg_int(join_row.get("seats_total")) or len(all_seats)
    kept_seats = all_seats[:_MAX_STORYBOARD_SEATS]
    per_seat_cap = max(1, min(400, _MAX_STEPS_PER_TRIAL // max(1, len(kept_seats))))
    seats = [_bound_seat_row(s, per_seat_cap) for s in kept_seats]
    chain_raw = det_row.get("chain_ok")
    chain_ok = chain_raw if isinstance(chain_raw, bool) else None
    chain_err = str(det_row["chain_error"]) if det_row.get("chain_error") is not None else None
    reward = finite_float(det_row.get("reward"))
    joined = bool(join_row.get("joined"))
    join_status = str(join_row.get("join_status") or ("joined" if joined else "unjoined"))
    triggered = [str(x) for x in as_list(det_row.get("triggered"))[:64]]
    det_map = {str(k): dict(as_map(v)) for k, v in as_map(det_row.get("detectors")).items()}
    join_sum = dict(as_map(join_row.get("summary")))
    sev = _compute_severity_score(
        chain_ok=chain_ok,
        join_status=join_status,
        triggered=triggered,
        det_map=det_map,
        join_sum=join_sum,
        reward=reward,
    )
    return {
        "trial": str(join_row.get("trial") or det_row.get("trial") or trial_dir.name),
        "path": str(join_row.get("path") or trial_dir),
        "ecosystem": ecosystem,
        "events_count": nonneg_int(join_row.get("events_count")) or nonneg_int(det_row.get("events_count")) or 0,
        "reward": reward,
        "chain_ok": chain_ok,
        "chain_error": chain_err,
        "joined": joined,
        "join_status": join_status,
        "seats_joined": nonneg_int(join_row.get("seats_joined")) or 0,
        "seats_total": seats_total,
        "seats_rendered": len(seats),
        "seats_truncated": seats_total > len(seats),
        "triggered": triggered,
        "detectors": det_map,
        "summary": join_sum,
        "severity_score": sev,
        "seats": seats,
    }


def _trial_sort_key(trial: Mapping[str, Any]) -> tuple[int, int, int, float, str, str]:
    chain_rank = 0 if trial.get("chain_ok") is False else 1
    sev = int(trial.get("severity_score") or 0)
    trig_n = len(as_list(trial.get("triggered")))
    rew = finite_float(trial.get("reward"))
    rew_rank = rew if rew is not None else -1.0
    return (chain_rank, -sev, -trig_n, rew_rank, str(trial.get("trial") or ""), str(trial.get("path") or ""))


def _top_detector_rows(
    det_counts: Mapping[str, int],
    det_hits: Mapping[str, int],
    n_total: int,
) -> list[dict[str, Any]]:
    assert n_total >= 1
    ordered = sorted(
        (name for name, cnt in det_counts.items() if cnt > 0),
        key=lambda nm: (-det_counts[nm], -det_hits.get(nm, 0), nm),
    )
    return [
        {
            "detector": name,
            "trials_flagged": det_counts[name],
            "total_hits": det_hits.get(name, 0),
            "rate": round(det_counts[name] / n_total, 6),
        }
        for name in ordered
    ]


def _summarize_trials(all_trials: list[dict[str, Any]]) -> dict[str, Any]:
    n_total = len(all_trials)
    assert n_total >= 1
    eco_counts: Counter[str] = Counter(str(t["ecosystem"]) for t in all_trials)
    status_counts: dict[str, int] = dict.fromkeys(_CANONICAL_JOIN_STATUSES, 0)
    det_counts: dict[str, int] = dict.fromkeys(DETECTOR_NAMES, 0)
    det_hits: dict[str, int] = dict.fromkeys(DETECTOR_NAMES, 0)
    same_total = same_trials = edge_total = batch_max = batch_ge2 = 0
    matched_total = unmatched_total = events_total = seats_total = seats_joined = 0
    flagged_trials = broken_chains = 0
    for tr in all_trials:
        st = str(tr["join_status"])
        status_counts[st] = status_counts.get(st, 0) + 1
        tsum = as_map(tr.get("summary"))
        same_n = nonneg_int(tsum.get("read_and_act_same_step")) or 0
        b_max = nonneg_int(tsum.get("batch_opens_max")) or 0
        same_total += same_n
        same_trials += int(same_n > 0)
        batch_max = max(batch_max, b_max)
        batch_ge2 += int(b_max >= 2)
        edge_total += nonneg_int(tsum.get("edge_events")) or 0
        matched_total += nonneg_int(tsum.get("events_matched")) or 0
        unmatched_total += nonneg_int(tsum.get("events_unmatched")) or 0
        events_total += nonneg_int(tr.get("events_count")) or 0
        seats_total += nonneg_int(tr.get("seats_total")) or 0
        seats_joined += nonneg_int(tr.get("seats_joined")) or 0
        flagged_trials += int(bool(tr["triggered"]))
        broken_chains += int(tr.get("chain_ok") is False)
        for dname, dval in as_map(tr.get("detectors")).items():
            info = as_map(dval)
            if info.get("fired") is True:
                det_counts[dname] = det_counts.get(dname, 0) + 1
            det_hits[dname] = det_hits.get(dname, 0) + (nonneg_int(info.get("count")) or 0)
    assert sum(status_counts.values()) == n_total
    return {
        "ecosystems": dict(sorted(eco_counts.items())),
        "join_status_counts": status_counts,
        "trials_flagged": flagged_trials,
        "broken_chains": broken_chains,
        "seats_total": seats_total,
        "seats_joined": seats_joined,
        "events_total": events_total,
        "events_matched": matched_total,
        "events_unmatched": unmatched_total,
        "edge_events": edge_total,
        "read_and_act_same_step": same_total,
        "trials_with_same_step_read_act": same_trials,
        "batch_opens_max": batch_max,
        "trials_with_batch_opens_ge2": batch_ge2,
        "detector_counts": det_counts,
        "detector_hits": det_hits,
        "top_detectors": _top_detector_rows(det_counts, det_hits, n_total),
    }


def run_storyboard(
    root: Path,
    *,
    html_path: Path | None = None,
    margin_s: float = 0.5,
    max_trials: int = DEFAULT_MAX_STORYBOARD_TRIALS,
) -> dict[str, Any]:
    margin_f = finite_float(margin_s)
    if margin_f is None or margin_f < 0.0:
        raise InputError("--margin-s must be a non-negative finite float")
    if isinstance(max_trials, bool) or not isinstance(max_trials, int) or max_trials < 1:
        raise InputError("--max-trials must be a positive integer")
    target = Path(root)
    if not target.exists():
        raise InputError(f"path does not exist: {target}")
    if not target.is_dir():
        raise InputError(f"not a directory: {target}")
    discovered = discover_trials(target)
    if not discovered:
        raise InputError(f"no trials found under: {target}")
    assert 1 <= len(discovered) <= MAX_TRIALS
    all_trials = [_build_trial_entry(tdir, eco, margin_f) for tdir, eco in discovered]
    all_trials.sort(key=_trial_sort_key)
    trials_total = len(all_trials)
    trials_joined = sum(1 for tr in all_trials if tr["joined"])
    summary = _summarize_trials(all_trials)
    limit = min(max_trials, DEFAULT_MAX_STORYBOARD_TRIALS)
    rendered = all_trials[:limit]
    _budget_rendered_trials(rendered)
    trials_rendered = len(rendered)
    truncated = trials_total > trials_rendered
    assert 1 <= trials_rendered <= DEFAULT_MAX_STORYBOARD_TRIALS
    assert truncated == (trials_total > trials_rendered)
    out_html = sanitize_surrogates(str(Path(html_path))) if html_path is not None else None
    report: dict[str, Any] = {
        "command": "storyboard",
        "path": sanitize_surrogates(str(target)),
        "html_path": out_html,
        "margin_s": margin_f,
        "trials_total": trials_total,
        "trials_rendered": trials_rendered,
        "truncated": truncated,
        "trials_joined": trials_joined,
        "summary": summary,
        "trials": rendered,
    }
    if html_path is not None:
        try:
            write_storyboard_html(Path(html_path), report)
        except (OSError, ValueError) as exc:
            raise InputError(f"cannot write --html {html_path}: {exc}") from None
    return report
