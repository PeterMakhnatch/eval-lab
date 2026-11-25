from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from worldwatch._chain import find_olam_proxy_log, read_jsonl_static
from worldwatch._io import read_tolerant_json_object
from worldwatch._trial import TrialBundle
from worldwatch.model import InputError, as_list, as_map, finite_float, nonneg_int

_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_GUARD_TOKENS = frozenset({
    "sk_guard", "skguard", "claimlock", "guard", "guarded", "fenced", "fence", "barrier", "interlock", "lockguard",
})
_S0K_TOKENS = frozenset({"indep", "independent", "silent", "uncoordinated", "nochannel"})
_SK_TOKENS = frozenset({"team", "coord", "coordinated", "channel", "shared", "swarm", "chief", "zones"})


def _row_tokens(row: Mapping[str, Any]) -> int:
    direct = nonneg_int(row.get("total_tokens"))
    if direct is not None and direct > 0:
        return direct
    inp = nonneg_int(row.get("prompt_tokens"))
    if inp is None:
        inp = nonneg_int(row.get("input_tokens"))
    if inp is None:
        inp = nonneg_int(row.get("n_input_tokens"))
    out = nonneg_int(row.get("completion_tokens"))
    if out is None:
        out = nonneg_int(row.get("output_tokens"))
    if out is None:
        out = nonneg_int(row.get("n_output_tokens"))
    if inp is None and out is None:
        return 0
    return (inp or 0) + (out or 0)


def _extract_atif_tokens(traj_path: Path, root: Path) -> int | None:
    if not os.path.lexists(traj_path):
        return None
    doc, err = read_tolerant_json_object(traj_path, root=root)
    if err is not None or doc is None:
        return None
    fm = as_map(doc.get("final_metrics"))
    fm_tot = (nonneg_int(fm.get("total_prompt_tokens")) or 0) + (nonneg_int(fm.get("total_completion_tokens")) or 0)
    if fm_tot > 0:
        return fm_tot
    step_sum = 0
    for st in as_list(doc.get("steps"))[:100_000]:
        st_map = as_map(st)
        if st_map.get("is_copied_context") is True or st_map.get("source") != "agent":
            continue
        step_sum += _row_tokens(as_map(st_map.get("metrics")))
    return step_sum if step_sum > 0 else None


def extract_trial_tokens(trial_dir: Path, ecosystem: str, bundle: TrialBundle) -> int | None:
    proxy_log = (trial_dir / "agent" / "proxy-usage.jsonl") if ecosystem == "bespoke" else find_olam_proxy_log(trial_dir)
    if proxy_log is not None and os.path.lexists(proxy_log):
        p_rows, _, _ = read_jsonl_static(proxy_log, require_seq=False, root=trial_dir)
        p_sum = sum(_row_tokens(r) for r in p_rows)
        if p_sum > 0:
            return p_sum
    for rel in ("agent/proxy-usage.json", "proxy-usage.json", "artifacts/proxy-usage.json"):
        pdoc, perr = read_tolerant_json_object(trial_dir / rel, root=trial_dir)
        if perr is None and pdoc is not None:
            tot_t = _row_tokens(as_map(pdoc.get("total"))) or _row_tokens(pdoc)
            if tot_t > 0:
                return tot_t
    res_doc, rerr = read_tolerant_json_object(trial_dir / "result.json", root=trial_dir)
    res_map = as_map(res_doc) if rerr is None else {}
    if ecosystem == "bespoke":
        ag_map = as_map(as_map(as_map(res_map.get("agent_result")).get("metadata")).get("agents"))
        b_sum = sum(_row_tokens(as_map(v)) for v in ag_map.values())
        if b_sum > 0:
            return b_sum
    sr_list = as_list(res_map.get("seat_results"))
    if sr_list:
        sr_sum = sum(_row_tokens(as_map(as_map(item).get("agent_result"))) for item in sr_list)
        if sr_sum > 0:
            return sr_sum
    single_sum = _row_tokens(as_map(res_map.get("agent_result")))
    if single_sum > 0:
        return single_sum
    atif_vals = [_extract_atif_tokens(s.atif.path, trial_dir) for s in bundle.seats if s.is_model and s.atif is not None]
    pos_atif = [v for v in atif_vals if v is not None and v > 0]
    return sum(pos_atif) if pos_atif else None


def _score_token_string(text: str) -> tuple[str, int] | None:
    low = text.strip().lower()
    if not low:
        return None
    segs = tuple(s for s in _SPLIT_RE.split(low) if s)
    norm = "_".join(segs)
    if norm in ("s1", "s_1"):
        return ("S1", 100)
    if norm in ("s0k", "s0_k", "s_0_k", "s0"):
        return ("S0k", 100)
    if norm in ("sk_guard", "s_k_guard", "skguard"):
        return ("Sk_guard", 100)
    if norm in ("sk", "s_k"):
        return ("Sk", 100)
    if not segs:
        return None
    has_solo = "solo" in segs or segs[0] == "s1" or "1seat" in segs or ("single" in segs and "seat" in segs)
    has_indep = bool(set(segs) & _S0K_TOKENS) or "s0k" in segs or ("no" in segs and "channel" in segs)
    has_guard = bool(set(segs) & _GUARD_TOKENS) or "sk_guard" in norm or "claimlock" in low
    if has_guard and not has_solo and not has_indep:
        return ("Sk_guard", 88 if ("sk_guard" in norm or "guard" in segs or "claimlock" in segs) else 78)
    if has_solo and not has_indep:
        if norm == "solo":
            return ("S1", 92)
        if any(seg.startswith("x") and seg[1:].isdigit() for seg in segs):
            return ("S1", 86)
        return ("S1", 82)
    if has_indep:
        if "s0k" in segs:
            return ("S0k", 95)
        if "salt" in segs:
            return ("S0k", 72)
        return ("S0k", 90)
    if "sk" in segs or bool(set(segs) & _SK_TOKENS):
        return ("Sk", 90 if ("sk" in segs or "team" in segs or "coord" in segs or "coordinated" in segs) else 76)
    return None


def _classify_arm_role(arm: Mapping[str, Any], cond_hints: Mapping[str, set[str]]) -> tuple[str, int] | None:
    arm_name = str(arm.get("arm") or "")
    explicit_role = str(arm.get("role") or "")
    if explicit_role and (hit := _score_token_string(explicit_role)) is not None:
        return (hit[0], hit[1] + 10)
    if (hit := _score_token_string(arm_name)) is not None:
        return hit
    conds = cond_hints.get(arm_name, set())
    if len(conds) == 1 and (hit := _score_token_string(next(iter(conds)))) is not None:
        return (hit[0], hit[1] - 2)
    raw_path = str(arm.get("path") or "").strip()
    if raw_path:
        leaf = Path(raw_path).name
        if (hit := _score_token_string(leaf)) is not None:
            return (hit[0], hit[1] - 5)
    return None


def _pos_metric(value: Any) -> float | None:
    num = finite_float(value)
    return num if (num is not None and num > 0.0) else None


def _safe_div(num: float | None, den: float | None, digits: int = 6) -> float | None:
    if num is None or den is None or den <= 1e-12:
        return None
    val = num / den
    return round(val, digits) if finite_float(val) is not None else None


def _build_role_entry(arm: Mapping[str, Any]) -> dict[str, Any]:
    rew_m = finite_float(arm.get("mean_measured_reward"))
    if rew_m is None:
        rew_m = finite_float(arm.get("mean_reward"))
    pass_r = finite_float(arm.get("binary_pass_rate"))
    cost_m = _pos_metric(arm.get("mean_cost_usd"))
    tok_m = _pos_metric(arm.get("mean_tokens"))
    pos_pass = pass_r if (pass_r is not None and pass_r > 0.0) else None
    pos_rew = rew_m if (rew_m is not None and rew_m > 0.0) else None
    return {
        "arm": str(arm.get("arm") or ""),
        "measured": nonneg_int(arm.get("measured")) or 0,
        "mean_reward": round(rew_m, 6) if rew_m is not None else None,
        "binary_pass_rate": round(pass_r, 6) if pass_r is not None else None,
        "mean_cost_usd": round(cost_m, 6) if cost_m is not None else None,
        "mean_tokens": round(tok_m, 3) if tok_m is not None else None,
        "cost_per_success_usd": _safe_div(cost_m, pos_pass, 6),
        "cost_per_reward_usd": _safe_div(cost_m, pos_rew, 6),
        "tokens_per_success": _safe_div(tok_m, pos_pass, 3),
        "tokens_per_reward": _safe_div(tok_m, pos_rew, 3),
    }


def _find_pair_p(pairs: Sequence[Mapping[str, Any]], arm_x: str | None, arm_y: str | None) -> float | None:
    if not arm_x or not arm_y:
        return None
    for row in pairs:
        a, b = row.get("arm_a"), row.get("arm_b")
        if (a == arm_x and b == arm_y) or (a == arm_y and b == arm_x):
            return finite_float(row.get("sign_flip_p"))
    return None


def _build_pace_block(
    e_s1: Mapping[str, Any],
    e_s0k: Mapping[str, Any],
    e_sk: Mapping[str, Any],
    e_guard: Mapping[str, Any] | None,
    coord_delta: float | None,
) -> dict[str, Any]:
    cps_s1, cps_s0k, cps_sk = e_s1["cost_per_success_usd"], e_s0k["cost_per_success_usd"], e_sk["cost_per_success_usd"]
    cps_g = e_guard["cost_per_success_usd"] if e_guard is not None else None
    cpr_s1, cpr_s0k, cpr_sk = e_s1["cost_per_reward_usd"], e_s0k["cost_per_reward_usd"], e_sk["cost_per_reward_usd"]
    cpr_g = e_guard["cost_per_reward_usd"] if e_guard is not None else None
    tps_s1, tps_s0k, tps_sk = e_s1["tokens_per_success"], e_s0k["tokens_per_success"], e_sk["tokens_per_success"]
    tps_g = e_guard["tokens_per_success"] if e_guard is not None else None
    tpr_s1, tpr_s0k, tpr_sk = e_s1["tokens_per_reward"], e_s0k["tokens_per_reward"], e_sk["tokens_per_reward"]
    tpr_g = e_guard["tokens_per_reward"] if e_guard is not None else None
    c_s0k, c_sk = e_s0k["mean_cost_usd"], e_sk["mean_cost_usd"]
    marg_cost: float | None = None
    if c_sk is not None and c_s0k is not None and coord_delta is not None and coord_delta > 1e-6:
        marg_cost = round((c_sk - c_s0k) / coord_delta, 6)
    return {
        "cost_per_success_usd": {"S1": cps_s1, "S0k": cps_s0k, "Sk": cps_sk, "Sk_guard": cps_g},
        "cost_per_reward_usd": {"S1": cpr_s1, "S0k": cpr_s0k, "Sk": cpr_sk, "Sk_guard": cpr_g},
        "tokens_per_success": {"S1": tps_s1, "S0k": tps_s0k, "Sk": tps_sk, "Sk_guard": tps_g},
        "tokens_per_reward": {"S1": tpr_s1, "S0k": tpr_s0k, "Sk": tpr_sk, "Sk_guard": tpr_g},
        "sk_vs_s1_cost_ratio": _safe_div(cps_sk, cps_s1, 6),
        "sk_vs_s0k_cost_ratio": _safe_div(cps_sk, cps_s0k, 6),
        "sk_guard_vs_sk_cost_ratio": _safe_div(cps_g, cps_sk, 6),
        "sk_vs_s1_cost_per_reward_ratio": _safe_div(cpr_sk, cpr_s1, 6),
        "sk_vs_s0k_cost_per_reward_ratio": _safe_div(cpr_sk, cpr_s0k, 6),
        "marginal_cost_per_coord_gain_usd": marg_cost,
        "sk_vs_s1_token_ratio": _safe_div(tps_sk, tps_s1, 6),
        "sk_vs_s0k_token_ratio": _safe_div(tps_sk, tps_s0k, 6),
        "sk_guard_vs_sk_token_ratio": _safe_div(tps_g, tps_sk, 6),
    }


def compute_triad_decomposition(
    arms: Sequence[Mapping[str, Any]],
    *,
    pairs: Sequence[Mapping[str, Any]] = (),
    condition_hints: Mapping[str, set[str]] | None = None,
    require_triad: bool = False,
) -> dict[str, Any] | None:
    hints = condition_hints or {}
    best_for_role: dict[str, tuple[int, int, Mapping[str, Any]]] = {}
    for idx, arm in enumerate(arms):
        if not isinstance(arm, Mapping):
            continue
        cls_res = _classify_arm_role(arm, hints)
        if cls_res is None:
            continue
        role, prio = cls_res
        prev = best_for_role.get(role)
        if prev is None or prio > prev[0]:
            best_for_role[role] = (prio, idx, arm)
    missing = [r for r in ("S1", "S0k", "Sk") if r not in best_for_role]
    if missing:
        if require_triad:
            raise InputError(f"PACE triad requires S1, S0k, and Sk arms (missing: {', '.join(missing)})")
        return None
    e_s1 = _build_role_entry(best_for_role["S1"][2])
    e_s0k = _build_role_entry(best_for_role["S0k"][2])
    e_sk = _build_role_entry(best_for_role["Sk"][2])
    e_guard = _build_role_entry(best_for_role["Sk_guard"][2]) if "Sk_guard" in best_for_role else None
    r_s1, r_s0k, r_sk = e_s1["mean_reward"], e_s0k["mean_reward"], e_sk["mean_reward"]
    if require_triad and (r_s1 is None or r_s0k is None or r_sk is None):
        raise InputError("PACE triad arms must each have at least one measured reward")
    r_g = e_guard["mean_reward"] if e_guard is not None else None
    head_delta = round(r_s0k - r_s1, 6) if (r_s0k is not None and r_s1 is not None) else None
    coord_delta = round(r_sk - r_s0k, 6) if (r_sk is not None and r_s0k is not None) else None
    team_delta = round(r_sk - r_s1, 6) if (r_sk is not None and r_s1 is not None) else None
    guard_delta = round(r_g - r_sk, 6) if (r_g is not None and r_sk is not None) else None
    coord_eff: float | None = None
    if r_sk is not None and r_s0k is not None and (1.0 - r_s0k) > 1e-9:
        coord_eff = round((r_sk - r_s0k) / (1.0 - r_s0k), 6)
    coord_share: float | None = None
    if coord_delta is not None and team_delta is not None and abs(team_delta) > 1e-9:
        coord_share = round(coord_delta / team_delta, 6)
    n_s1, n_s0k, n_sk = e_s1["arm"], e_s0k["arm"], e_sk["arm"]
    n_g = e_guard["arm"] if e_guard is not None else None
    return {
        "roles": {"S1": n_s1, "S0k": n_s0k, "Sk": n_sk, "Sk_guard": n_g},
        "S1": e_s1,
        "S0k": e_s0k,
        "Sk": e_sk,
        "Sk_guard": e_guard,
        "headcount_delta": head_delta,
        "coordination_delta": coord_delta,
        "team_over_solo_delta": team_delta,
        "guard_delta": guard_delta,
        "coordination_efficiency": coord_eff,
        "coordination_share": coord_share,
        "paired_p_values": {
            "s0k_vs_s1": _find_pair_p(pairs, n_s0k, n_s1),
            "sk_vs_s0k": _find_pair_p(pairs, n_sk, n_s0k),
            "sk_vs_s1": _find_pair_p(pairs, n_sk, n_s1),
            "sk_guard_vs_sk": _find_pair_p(pairs, n_g, n_sk),
        },
        "pace": _build_pace_block(e_s1, e_s0k, e_sk, e_guard, coord_delta),
    }
