from __future__ import annotations

import math
import os
from collections import Counter
from pathlib import Path
from typing import Any

from worldwatch._chain import (
    find_bespoke_board_log,
    find_olam_event_log,
    find_olam_proxy_log,
    read_jsonl_static,
)
from worldwatch._io import discover_trials
from worldwatch._trial import (
    SeatInfo,
    TrialBundle,
    load_trial_bundle,
)
from worldwatch.model import DEFAULT_STALL_SECONDS, InputError

_EXACT_COMB_LIMIT = 25_000
_MAX_SUPPORT = 1_000_000


def _canonicalize_2x2(a: int, b: int, c: int, d: int) -> tuple[int, int, int, int]:
    symmetries = (
        (a, b, c, d),
        (c, d, a, b),
        (b, a, d, c),
        (d, c, b, a),
        (a, c, b, d),
        (b, d, a, c),
        (c, a, d, b),
        (d, b, c, a),
    )
    best = symmetries[0]
    best_key = (best[0] + best[2], best[0] + best[1], best)
    for cand in symmetries[1:]:
        key = (cand[0] + cand[2], cand[0] + cand[1], cand)
        if key < best_key:
            best = cand
            best_key = key
    return best


def _fisher_log_recurrence(r1: int, r2: int, c1: int, n: int, a: int, k_min: int, k_max: int) -> float:
    support = k_max - k_min + 1
    assert 1 <= support <= _MAX_SUPPORT
    k_mode = min(k_max, max(k_min, ((r1 + 1) * (c1 + 1)) // (n + 2)))
    lp = [0.0] * support
    for k in range(k_mode, k_max):
        ratio = ((r1 - k) * (c1 - k)) / ((k + 1) * (r2 - c1 + k + 1))
        lp[k + 1 - k_min] = lp[k - k_min] + math.log(ratio)
    for k in range(k_mode - 1, k_min - 1, -1):
        ratio = ((k + 1) * (r2 - c1 + k + 1)) / ((r1 - k) * (c1 - k))
        lp[k - k_min] = lp[k + 1 - k_min] + math.log(ratio)
    total_mass = math.fsum(math.exp(x) for x in lp)
    if total_mass <= 0.0 or not math.isfinite(total_mass):
        return 1.0
    lp_obs = lp[a - k_min]
    tail_mass = math.fsum(math.exp(x) for x in lp if x <= lp_obs + 1e-13)
    p = min(1.0, max(0.0, tail_mass / total_mass))
    assert 0.0 <= p <= 1.0 and math.isfinite(p)
    return p


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    if (
        isinstance(a, bool)
        or isinstance(b, bool)
        or isinstance(c, bool)
        or isinstance(d, bool)
        or not (isinstance(a, int) and isinstance(b, int) and isinstance(c, int) and isinstance(d, int))
        or a < 0
        or b < 0
        or c < 0
        or d < 0
    ):
        raise InputError("Fisher table counts must be non-negative integers")
    a, b, c, d = _canonicalize_2x2(a, b, c, d)
    r1, r2 = a + b, c + d
    c1, c2 = a + c, b + d
    n = r1 + r2
    if n == 0 or r1 == 0 or r2 == 0 or c1 == 0 or c2 == 0 or a * d == b * c:
        return 1.0
    k_min = max(0, c1 - r2)
    k_max = min(r1, c1)
    assert 0 <= k_min <= a <= k_max <= r1
    support = k_max - k_min + 1
    if support > _MAX_SUPPORT:
        raise InputError(f"Fisher table support {support} exceeds limit {_MAX_SUPPORT}")
    if n > _EXACT_COMB_LIMIT:
        return _fisher_log_recurrence(r1, r2, c1, n, a, k_min, k_max)
    w_obs = math.comb(r1, a) * math.comb(r2, c1 - a)
    denom = math.comb(n, c1)
    total_w = 0
    tail_w = 0
    for k in range(k_min, k_max + 1):
        w = math.comb(r1, k) * math.comb(r2, c1 - k)
        total_w += w
        if w <= w_obs:
            tail_w += w
    assert total_w == denom
    p = min(1.0, max(0.0, tail_w / denom))
    assert 0.0 <= p <= 1.0 and math.isfinite(p)
    return p


def _seat_stalled(seat: SeatInfo, bundle: TrialBundle, stall_seconds: float) -> bool:
    if seat.exception != "AgentTimeoutError":
        return False
    if seat.is_model and (seat.atif is None or seat.atif.problem is not None or seat.atif.last_step_at is None):
        return True
    end = seat.finished_at or bundle.agent_finished_at or bundle.finished_at
    last = (seat.atif.last_step_at if seat.atif and seat.atif.problem is None else None) or seat.started_at or bundle.agent_started_at
    if end is None or last is None:
        return seat.is_model
    return max(0.0, (end - last).total_seconds()) >= stall_seconds


def _world_seat_counts(events: list[dict[str, Any]], bundle: TrialBundle) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in events:
        if bundle.ecosystem == "bespoke":
            raw = row.get("agent") or row.get("from")
            if isinstance(raw, str) and raw and str(row.get("event") or "") not in ("config", "register"):
                counts[bundle.facing_to_seat.get(raw, raw)] += 1
        else:
            raw = row.get("seat")
            if isinstance(raw, str) and raw:
                counts[raw] += 1
    return counts


def _load_validity_logs(
    trial_dir: Path,
    ecosystem: str,
    bundle: TrialBundle,
) -> tuple[list[dict[str, Any]], bool, bool]:
    if ecosystem == "bespoke":
        b_path = find_bespoke_board_log(trial_dir)
        events, chain_ok, _ = (
            read_jsonl_static(b_path, require_seq=False, root=trial_dir)
            if b_path is not None
            else ([], None, None)
        )
        px_path = trial_dir / "agent" / "proxy-usage.jsonl"
        if not os.path.lexists(px_path) and os.path.lexists(trial_dir / "proxy-usage.jsonl"):
            px_path = trial_dir / "proxy-usage.jsonl"
        _, px_ok, _ = read_jsonl_static(px_path, require_seq=False, root=trial_dir)
        return events, (chain_ok is False or px_ok is False), (b_path is None)
    ev_path = find_olam_event_log(trial_dir)
    events, chain_ok, _ = (
        read_jsonl_static(ev_path, require_seq=True, root=trial_dir)
        if ev_path is not None
        else ([], None, None)
    )
    px_path = find_olam_proxy_log(trial_dir)
    _, px_ok, _ = (
        read_jsonl_static(px_path, require_seq=False, root=trial_dir)
        if px_path is not None
        else ([], None, None)
    )
    has_multi = os.path.isdir(trial_dir / "seats") or len(bundle.seats) > 1
    return events, (chain_ok is False or px_ok is False), (has_multi and ev_path is None)


def classify_trial_validity(
    trial_dir: Path,
    ecosystem: str,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
) -> dict[str, Any]:
    assert stall_seconds > 0
    bundle = load_trial_bundle(trial_dir, ecosystem)
    if bundle.result_state == "missing":
        return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "unfinished-trial"}
    if bundle.result_state == "unreadable":
        return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "unreadable-result"}
    if bundle.not_started is not None:
        return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "not-started"}
    events, log_bad, log_missing = _load_validity_logs(trial_dir, ecosystem, bundle)
    if log_bad:
        return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "corrupt-world-log"}
    if log_missing:
        return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "missing-world-log"}
    if bundle.rewards.get("script_faithful") == 0.0:
        return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "void-trial"}
    ev_counts = _world_seat_counts(events, bundle)
    for seat in bundle.seats:
        if not seat.in_seat_results:
            return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "missing-seat"}
        if seat.exit_error is not None or (seat.exit_code not in (None, 0) and seat.exception is None):
            return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "lost-seat"}
        if _seat_stalled(seat, bundle, stall_seconds):
            return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "stalled-seat"}
        if seat.under_test and seat.is_model:
            steps = seat.atif.agent_steps if seat.atif and seat.atif.problem is None else 0
            calls = seat.atif.llm_calls if seat.atif and seat.atif.problem is None else 0
            if steps == 0 and calls == 0 and ev_counts[seat.seat] == 0:
                return {
                    "trial": bundle.trial_name,
                    "path": str(trial_dir),
                    "measured": False,
                    "reason": "zero-activity-model-seat",
                }
    if not bundle.reward_valid or bundle.reward is None:
        return {"trial": bundle.trial_name, "path": str(trial_dir), "measured": False, "reason": "invalid-reward"}
    return {
        "trial": bundle.trial_name,
        "path": str(trial_dir),
        "measured": True,
        "reason": "measured",
        "reward": bundle.reward,
    }


def _parse_arm_spec(spec: str) -> tuple[str | None, Path]:
    if "=" in spec and not os.path.exists(spec):
        label, _, raw_path = spec.partition("=")
        if label.strip() and raw_path.strip():
            return label.strip(), Path(raw_path.strip())
    return None, Path(spec)


def _evaluate_arm(name: str, path: Path, stall_seconds: float, max_rate: float) -> dict[str, Any]:
    if not path.exists():
        raise InputError(f"arm path does not exist: {path}")
    if not path.is_dir():
        raise InputError(f"arm path is not a directory: {path}")
    discovered = discover_trials(path)
    if not discovered:
        raise InputError(f"arm {name!r} ({path}) contains no trials")
    rows = [classify_trial_validity(tdir, eco, stall_seconds) for tdir, eco in discovered]
    measured = sum(1 for r in rows if r["measured"])
    unmeasured = len(rows) - measured
    by_reason = Counter(r["reason"] for r in rows if not r["measured"])
    rate = unmeasured / len(rows)
    rewards = [float(r["reward"]) for r in rows if r["measured"] and r.get("reward") is not None]
    mean_rew = round(sum(rewards) / len(rewards), 6) if rewards else None
    return {
        "arm": name,
        "path": str(path),
        "total_trials": len(rows),
        "measured": measured,
        "unmeasured": unmeasured,
        "unmeasured_rate": round(rate, 6),
        "exceeds_max_unmeasured": rate > max_rate,
        "mean_measured_reward": mean_rew,
        "attrition_by_reason": dict(sorted(by_reason.items())),
        "unmeasured_trials": [
            {"trial": r["trial"], "reason": r["reason"]} for r in rows if not r["measured"]
        ][:200],
    }


def run_validity(
    arm_specs: list[str],
    *,
    max_unmeasured_rate: float = 0.10,
    alpha: float = 0.05,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
) -> tuple[dict[str, Any], int]:
    if not arm_specs:
        raise InputError("at least one arm path is required")
    if not (0.0 <= max_unmeasured_rate <= 1.0 and math.isfinite(max_unmeasured_rate)):
        raise InputError("--max-unmeasured-rate must be in [0.0, 1.0]")
    if not (0.0 < alpha < 1.0 and math.isfinite(alpha)):
        raise InputError("--alpha must be in (0.0, 1.0)")
    if not (stall_seconds > 0 and math.isfinite(stall_seconds)):
        raise InputError("--stall-seconds must be positive")
    parsed = [_parse_arm_spec(s) for s in arm_specs]
    raw_names = [lbl or p.resolve().name for lbl, p in parsed]
    if len(set(raw_names)) != len(raw_names):
        names = [lbl or str(p) for lbl, p in parsed]
    else:
        names = raw_names
    arms = [_evaluate_arm(nm, p, stall_seconds, max_unmeasured_rate) for nm, (_, p) in zip(names, parsed, strict=True)]
    pairs: list[dict[str, Any]] = []
    for i in range(len(arms)):
        for j in range(i + 1, len(arms)):
            left, right = arms[i], arms[j]
            pval = fisher_exact_two_sided(
                left["unmeasured"], left["measured"], right["unmeasured"], right["measured"]
            )
            pairs.append({
                "arm_a": left["arm"],
                "arm_b": right["arm"],
                "table": [[left["unmeasured"], left["measured"]], [right["unmeasured"], right["measured"]]],
                "rate_diff": round(left["unmeasured_rate"] - right["unmeasured_rate"], 6),
                "fisher_p": round(pval, 8),
                "differential_attrition": pval < alpha,
            })
    min_p = min((pair["fisher_p"] for pair in pairs), default=None)
    exceeded = [a["arm"] for a in arms if a["exceeds_max_unmeasured"]]
    diff_pairs = [f"{p['arm_a']} vs {p['arm_b']}" for p in pairs if p["differential_attrition"]]
    passed = not exceeded and not diff_pairs
    report: dict[str, Any] = {
        "command": "validity",
        "max_unmeasured_rate": max_unmeasured_rate,
        "alpha": alpha,
        "stall_seconds": stall_seconds,
        "passed": passed,
        "exceeded_arms": exceeded,
        "differential_pairs": diff_pairs,
        "min_fisher_p": min_p,
        "arms": arms,
        "pairs": pairs,
    }
    return report, (0 if passed else 1)
