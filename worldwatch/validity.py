from __future__ import annotations

import math
import os
from collections import Counter
from pathlib import Path
from typing import Any

from worldwatch._chain import find_olam_event_log, read_jsonl_static
from worldwatch._io import discover_trials
from worldwatch._trial import (
    SeatInfo,
    TrialBundle,
    load_trial_bundle,
)
from worldwatch.model import DEFAULT_STALL_SECONDS, InputError

_EXACT_COMB_LIMIT = 25_000
_MAX_SUPPORT = 1_000_000


def _log_comb(n: int, k: int) -> float:
    assert 0 <= k <= n
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _fisher_log_space(r1: int, r2: int, c1: int, n: int, a: int, k_min: int, k_max: int) -> float:
    assert k_max - k_min + 1 <= _MAX_SUPPORT
    log_denom = _log_comb(n, c1)
    log_obs = _log_comb(r1, a) + _log_comb(r2, c1 - a) - log_denom
    selected: list[float] = []
    for k in range(k_min, k_max + 1):
        lp = _log_comb(r1, k) + _log_comb(r2, c1 - k) - log_denom
        if lp <= log_obs + 1e-12:
            selected.append(lp)
    if not selected:
        return 0.0
    m = max(selected)
    prob = math.exp(m) * sum(math.exp(lp - m) for lp in selected)
    return min(1.0, max(0.0, prob))


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    assert a >= 0 and b >= 0 and c >= 0 and d >= 0
    r1, r2 = a + b, c + d
    c1, c2 = a + c, b + d
    n = r1 + r2
    if n == 0 or r1 == 0 or r2 == 0 or c1 == 0 or c2 == 0:
        return 1.0
    k_min = max(0, c1 - r2)
    k_max = min(r1, c1)
    assert 0 <= k_min <= a <= k_max <= r1
    if k_max - k_min + 1 > _EXACT_COMB_LIMIT:
        return _fisher_log_space(r1, r2, c1, n, a, k_min, k_max)
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
    if ecosystem == "bespoke":
        events, chain_ok, _ = read_jsonl_static(trial_dir / "agent" / "board-events.jsonl", require_seq=False)
        _, px_ok, _ = read_jsonl_static(trial_dir / "agent" / "proxy-usage.jsonl", require_seq=False)
        log_bad = chain_ok is False or px_ok is False
        log_missing = False
    else:
        ev_path = find_olam_event_log(trial_dir)
        events, chain_ok, _ = read_jsonl_static(ev_path, require_seq=True) if ev_path else ([], None, None)
        _, px_ok, _ = read_jsonl_static(trial_dir / "proxy-usage.jsonl", require_seq=False)
        log_bad = chain_ok is False or px_ok is False
        has_multi = os.path.isdir(trial_dir / "seats") or len(bundle.seats) > 1
        log_missing = has_multi and ev_path is None
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
