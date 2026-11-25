from __future__ import annotations

import math
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import Any

from worldwatch._compare_html import write_compare_html
from worldwatch._compare_triad import compute_triad_decomposition, extract_trial_tokens
from worldwatch._io import (
    check_no_symlink_ancestors,
    discover_trials,
    read_regular_text,
    read_tolerant_json_object,
)
from worldwatch._trial import TrialBundle, load_trial_bundle
from worldwatch.detectors import analyze_trial_detectors
from worldwatch.model import (
    DEFAULT_STALL_SECONDS,
    DETECTOR_NAMES,
    MAX_TRIALS,
    InputError,
    as_map,
    finite_float,
)
from worldwatch.validity import classify_trial_validity, fisher_exact_two_sided

_PASS_THRESHOLD = 1.0 - 1e-6
_MAX_ARMS = 64
_MAX_SEED = 2**64 - 1
_MAX_METRIC_ABS = 1e9
_EXACT_FLIP_LIMIT = 16
_MC_FLIPS = 10_000
_FAM_INST_RE = re.compile(r"^([A-Za-z0-9_.-]+?)-(i\d+)$")
_HARBOR_TAIL_RE = re.compile(r"^[A-Za-z0-9]{6,12}$")
_HARBOR_HASH7_RE = re.compile(r"^[A-Za-z0-9]{7}$")
_INST_TOKEN_RE = re.compile(r"^i\d+$")


@dataclass(frozen=True, slots=True)
class _TrialEntry:
    trial: str
    path: str
    key: tuple[str, str, str]
    measured: bool
    unmeasured_reason: str
    reward: float | None
    reward_valid: bool
    cost_usd: float | None
    triggered: frozenset[str]
    tokens: int | None = None


@dataclass(frozen=True, slots=True)
class _ArmEval:
    summary: dict[str, Any]
    entries: tuple[_TrialEntry, ...]
    by_exact_key: dict[tuple[str, str, str], tuple[_TrialEntry, ...]]


def _parse_arm_spec(spec: str) -> tuple[str | None, Path]:
    if "=" in spec and not os.path.exists(spec):
        label, _, raw_path = spec.partition("=")
        if label.strip() and raw_path.strip():
            return label.strip(), Path(raw_path.strip())
    return None, Path(spec)


def _normalize_instance_token(family: str, raw_inst: Any) -> str:
    if isinstance(raw_inst, int) and not isinstance(raw_inst, bool):
        return f"i{raw_inst:02d}"
    txt = str(raw_inst or "").strip()
    if family and txt.startswith(f"{family}-") and len(txt) > len(family) + 1:
        txt = txt[len(family) + 1 :]
    if txt.isdigit():
        return f"i{int(txt):02d}"
    if len(txt) == 2 and txt[0] == "i" and txt[1].isdigit():
        return f"i{int(txt[1]):02d}"
    return txt or "default"


def _key_from_task_toml(toml_path: Path, *, root: Path | None = None) -> tuple[str, str, str] | None:
    text, err = read_regular_text(toml_path, max_bytes=262_144, root=root)
    if err is not None or text is None:
        return None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    meta = as_map(doc.get("metadata"))
    fam, cond, inst = meta.get("family"), meta.get("condition"), meta.get("instance")
    if isinstance(fam, str) and fam.strip() and isinstance(cond, str) and cond.strip() and inst is not None:
        fam_clean = fam.strip()
        return (fam_clean, cond.strip(), _normalize_instance_token(fam_clean, inst))
    return None


def _key_from_task_path(raw_path: str) -> tuple[str, str, str] | None:
    clean = raw_path.strip().rstrip("/")
    if not clean or clean in ("sim-task", "bespoke-sim"):
        return None
    tp = Path(clean)
    parts3 = tp.name.split("__")
    if len(parts3) == 3 and all(parts3):
        return (parts3[0], parts3[1], _normalize_instance_token(parts3[0], parts3[2]))
    toml_cand = tp / "task.toml"
    if check_no_symlink_ancestors(toml_cand) is None and not os.path.islink(tp) and os.path.isfile(toml_cand):
        meta_key = _key_from_task_toml(toml_cand, root=tp)
        if meta_key is not None:
            return meta_key
    segs = [s for s in tp.parts if s not in ("", "/", ".")]
    if len(segs) >= 3 and segs[-3] == "build":
        base, cond = segs[-1], segs[-2]
        m = _FAM_INST_RE.match(base)
        fam = segs[-4] if len(segs) >= 4 else (m.group(1) if m else cond)
        return (fam, cond, _normalize_instance_token(fam, base))
    return None


def _is_harbor_hash7(tok: str) -> bool:
    return (
        bool(_HARBOR_HASH7_RE.fullmatch(tok))
        and not _INST_TOKEN_RE.fullmatch(tok)
        and any(c.isdigit() or c.isupper() for c in tok)
    )


def _extract_task_name_hint(t_clean: str) -> tuple[str, str | None]:
    m = _FAM_INST_RE.match(t_clean)
    if m:
        return m.group(1), _normalize_instance_token(m.group(1), m.group(2))
    if _INST_TOKEN_RE.fullmatch(t_clean):
        return "", _normalize_instance_token("", t_clean)
    return "", None


def _key_from_trial_name(trial_name: str, task_name: str) -> tuple[str, str, str]:
    t_clean = task_name.strip().rsplit("/", 1)[-1]
    t_parts = t_clean.split("__")
    if len(t_parts) == 3 and all(t_parts):
        return (t_parts[0], t_parts[1], _normalize_instance_token(t_parts[0], t_parts[2]))
    fam_hint, inst_hint = _extract_task_name_hint(t_clean)
    parts = [p for p in trial_name.strip().split("__") if p]
    if len(parts) >= 4 and _HARBOR_TAIL_RE.fullmatch(parts[-1]) and not _INST_TOKEN_RE.fullmatch(parts[-1]):
        parts = parts[:-1]
    elif (
        len(parts) == 3
        and _HARBOR_HASH7_RE.fullmatch(parts[2])
        and not _INST_TOKEN_RE.fullmatch(parts[2])
        and parts[2] != "default"
        and (any(c.isdigit() or c.isupper() for c in parts[2]) or len(f"{parts[0]}__{parts[1]}") >= 30)
    ):
        inst_resolved = inst_hint if (inst_hint and (not fam_hint or fam_hint == parts[0])) else "default"
        return (parts[0], parts[1], inst_resolved)
    elif len(parts) == 2 and _is_harbor_hash7(parts[1]):
        parts = [parts[0]]
    if len(parts) >= 3:
        fam, cond, raw_inst = parts[0], parts[1], "__".join(parts[2:])
        if inst_hint and (not fam_hint or fam_hint == fam) and inst_hint.startswith(raw_inst):
            return (fam, cond, inst_hint)
        return (fam, cond, _normalize_instance_token(fam, raw_inst))
    if len(parts) == 2:
        p0, p1 = parts
        if _INST_TOKEN_RE.fullmatch(p1) or p1.isdigit() or p1.startswith(f"{p0}-") or p1 == "default":
            return (p0, "default", _normalize_instance_token(p0, p1))
        inst_resolved = inst_hint if (inst_hint and (not fam_hint or fam_hint == p0)) else "default"
        return (p0, p1, inst_resolved)
    stem = parts[0] if parts else (t_clean or "trial")
    m = _FAM_INST_RE.match(stem)
    if m:
        return (m.group(1), "default", _normalize_instance_token(m.group(1), m.group(2)))
    if inst_hint and fam_hint:
        return (fam_hint, "default", inst_hint)
    return ("default", "default", stem)


def _extract_pairing_key(trial_dir: Path, bundle: TrialBundle) -> tuple[str, str, str]:
    cfg_doc, _ = read_tolerant_json_object(trial_dir / "config.json", root=trial_dir)
    res_doc, _ = read_tolerant_json_object(trial_dir / "result.json", root=trial_dir)
    cfg_map, res_map = as_map(cfg_doc), as_map(res_doc)
    candidates = (
        as_map(cfg_map.get("task")).get("path"),
        as_map(as_map(res_map.get("config")).get("task")).get("path"),
        as_map(res_map.get("task_id")).get("path"),
    )
    for cand in candidates:
        if isinstance(cand, str) and cand.strip():
            key = _key_from_task_path(cand)
            if key is not None:
                return key
    if os.path.lexists(trial_dir / "task.toml"):
        meta_key = _key_from_task_toml(trial_dir / "task.toml", root=trial_dir)
        if meta_key is not None:
            return meta_key
    tname = str(
        res_map.get("task_name")
        or as_map(cfg_map.get("task")).get("name")
        or as_map(as_map(res_map.get("config")).get("task")).get("name")
        or bundle.task_name
    )
    return _key_from_trial_name(bundle.trial_name, tname)


def _evaluate_compare_arm(name: str, path: Path, stall_seconds: float) -> _ArmEval:
    if not path.exists():
        raise InputError(f"arm path does not exist: {path}")
    if not path.is_dir():
        raise InputError(f"arm path is not a directory: {path}")
    discovered = discover_trials(path)
    if not discovered:
        raise InputError(f"arm {name!r} ({path}) contains no trials")
    assert len(discovered) <= MAX_TRIALS
    entries: list[_TrialEntry] = []
    for tdir, eco in discovered:
        val_row = classify_trial_validity(tdir, eco, stall_seconds)
        det_row = analyze_trial_detectors(tdir, eco)
        bundle = load_trial_bundle(tdir, eco)
        key = _extract_pairing_key(tdir, bundle)
        toks = extract_trial_tokens(tdir, eco, bundle)
        rew = bundle.reward
        rew_valid = bundle.reward_valid
        meas = bool(val_row["measured"])
        reason = str(val_row["reason"])
        if rew is not None and (not math.isfinite(rew) or abs(rew) > _MAX_METRIC_ABS):
            rew, rew_valid, meas, reason = None, False, False, "invalid_reward_magnitude"
        cost_v = bundle.cost_usd
        if cost_v is not None and (not math.isfinite(cost_v) or cost_v < 0.0 or cost_v > _MAX_METRIC_ABS):
            cost_v = None
        entries.append(
            _TrialEntry(
                trial=bundle.trial_name, path=str(tdir), key=key, measured=meas,
                unmeasured_reason=reason, reward=rew, reward_valid=rew_valid,
                cost_usd=cost_v, triggered=frozenset(str(x) for x in det_row["triggered"]), tokens=toks,
            )
        )
    entries.sort(key=lambda item: (item.key, item.trial, item.path))
    return _summarize_arm(name, path, tuple(entries))


def _summarize_arm(name: str, path: Path, entries: tuple[_TrialEntry, ...]) -> _ArmEval:
    assert 1 <= len(entries) <= MAX_TRIALS
    total = len(entries)
    measured_rews = [float(e.reward) for e in entries if e.measured and e.reward is not None]
    all_valid_rews = [float(e.reward) for e in entries if e.reward_valid and e.reward is not None]
    measured_n = len(measured_rews)
    unmeasured_n = total - measured_n
    by_reason = Counter(e.unmeasured_reason for e in entries if not e.measured)
    costs = [float(e.cost_usd) for e in entries if e.cost_usd is not None and 0.0 <= e.cost_usd <= _MAX_METRIC_ABS]
    meas_costs = [float(e.cost_usd) for e in entries if e.measured and e.cost_usd is not None and 0.0 <= e.cost_usd <= _MAX_METRIC_ABS]
    toks = [int(e.tokens) for e in entries if e.tokens is not None and e.tokens > 0]
    meas_toks = [int(e.tokens) for e in entries if e.measured and e.tokens is not None and e.tokens > 0]
    pass_n = sum(1 for r in measured_rews if r >= _PASS_THRESHOLD)
    det_counts = {d: sum(1 for e in entries if d in e.triggered) for d in DETECTOR_NAMES}
    det_rates = {d: round(det_counts[d] / total, 6) for d in DETECTOR_NAMES}
    grouped: dict[tuple[str, str, str], list[_TrialEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.key, []).append(entry)
    exact_groups = {k: tuple(v) for k, v in sorted(grouped.items())}
    summary: dict[str, Any] = {
        "arm": name,
        "path": str(path),
        "total_trials": total,
        "valid_reward_trials": len(all_valid_rews),
        "measured": measured_n,
        "unmeasured": unmeasured_n,
        "unmeasured_rate": round(unmeasured_n / total, 6),
        "attrition_by_reason": dict(sorted(by_reason.items())),
        "mean_reward": round(math.fsum(all_valid_rews) / len(all_valid_rews), 6) if all_valid_rews else None,
        "mean_measured_reward": round(math.fsum(measured_rews) / measured_n, 6) if measured_rews else None,
        "binary_pass_rate": round(pass_n / measured_n, 6) if measured_rews else None,
        "mean_cost_usd": round(math.fsum(costs) / len(costs), 6) if costs else None,
        "mean_measured_cost_usd": round(math.fsum(meas_costs) / len(meas_costs), 6) if meas_costs else None,
        "total_cost_usd": round(math.fsum(costs), 6) if costs else None,
        "mean_tokens": round(sum(toks) / len(toks), 3) if toks else None,
        "mean_measured_tokens": round(sum(meas_toks) / len(meas_toks), 3) if meas_toks else None,
        "total_tokens": int(sum(toks)) if toks else None,
        "detector_rates": det_rates,
        "detector_counts": det_counts,
        "unique_keys": len(exact_groups),
    }
    return _ArmEval(summary=summary, entries=entries, by_exact_key=exact_groups)


def _sign_flip_two_sided(deltas: list[float], seed: int) -> tuple[float, bool]:
    assert 0 <= seed <= _MAX_SEED and len(deltas) <= MAX_TRIALS
    if any(not math.isfinite(d) for d in deltas):
        raise InputError("non-finite reward delta in paired sign-flip test")
    nonzero = [d for d in deltas if abs(d) > 1e-15]
    if not nonzero:
        return 1.0, True
    m = len(nonzero)
    scale = max(abs(v) for v in nonzero)
    unit = [v / scale for v in nonzero]
    abs_sum = math.fsum(abs(v) for v in unit)
    min_abs = min(abs(v) for v in unit)
    tol = min(1e-12 * abs_sum, 0.25 * min_abs)
    if m <= _EXACT_FLIP_LIMIT:
        sums = [0.0]
        for val in unit:
            sums = [s + val for s in sums] + [s - val for s in sums]
        assert len(sums) == (1 << m)
        obs = abs(sums[0])
        hits = sum(1 for s in sums if abs(s) >= obs - tol)
        p = min(1.0, max(0.0, hits / float(1 << m)))
        assert 0.0 < p <= 1.0 and math.isfinite(p)
        return p, True
    rng = Random(seed)
    obs = abs(math.fsum(unit))
    hits = 0
    for _ in range(_MC_FLIPS):
        mask = rng.getrandbits(m)
        flipped = math.fsum(val if ((mask >> idx) & 1) else -val for idx, val in enumerate(unit))
        if abs(flipped) >= obs - tol:
            hits += 1
    p = min(1.0, max(0.0, (hits + 1) / float(_MC_FLIPS + 1)))
    assert 0.0 < p <= 1.0 and math.isfinite(p)
    return p, False


def _mcnemar_two_sided(pass_pairs: list[tuple[bool, bool]]) -> tuple[float, int, int]:
    assert len(pass_pairs) <= MAX_TRIALS
    a_only = sum(1 for pa, pb in pass_pairs if pa and not pb)
    b_only = sum(1 for pa, pb in pass_pairs if not pa and pb)
    n_disc = a_only + b_only
    if n_disc == 0:
        return 1.0, 0, 0
    k_min = min(a_only, b_only)
    tail = sum(math.comb(n_disc, idx) for idx in range(k_min + 1))
    p = min(1.0, max(0.0, float((2 * tail) / (1 << n_disc))))
    assert 0.0 <= p <= 1.0 and math.isfinite(p)
    return p, a_only, b_only


def _pair_key_groups(
    left: _ArmEval,
    right: _ArmEval,
) -> tuple[str, list[tuple[tuple[str, str, str], tuple[_TrialEntry, ...], tuple[_TrialEntry, ...]]], int, int]:
    all_fams = sorted({k[0] for k in left.by_exact_key} | {k[0] for k in right.by_exact_key})
    matched: list[tuple[tuple[str, str, str], tuple[_TrialEntry, ...], tuple[_TrialEntry, ...]]] = []
    modes_used: set[str] = set()
    only_a = only_b = 0
    for fam in all_fams:
        l_keys = {k for k in left.by_exact_key if k[0] == fam}
        r_keys = {k for k in right.by_exact_key if k[0] == fam}
        if not l_keys or not r_keys:
            only_a += len(l_keys)
            only_b += len(r_keys)
            continue
        shared_exact = sorted(l_keys & r_keys)
        if shared_exact:
            modes_used.add("family_condition_instance")
            for k in shared_exact:
                matched.append((k, left.by_exact_key[k], right.by_exact_key[k]))
            only_a += len(l_keys - r_keys)
            only_b += len(r_keys - l_keys)
            continue
        l_conds = {k[1] for k in l_keys}
        r_conds = {k[1] for k in r_keys}
        if len(l_conds) == 1 and len(r_conds) == 1 and l_conds != r_conds:
            l_inst: dict[tuple[str, str, str], list[_TrialEntry]] = {}
            r_inst: dict[tuple[str, str, str], list[_TrialEntry]] = {}
            for k in sorted(l_keys):
                l_inst.setdefault((fam, "*", k[2]), []).extend(left.by_exact_key[k])
            for k in sorted(r_keys):
                r_inst.setdefault((fam, "*", k[2]), []).extend(right.by_exact_key[k])
            shared_inst = sorted(set(l_inst) & set(r_inst))
            if shared_inst:
                modes_used.add("family_instance")
                for ik in shared_inst:
                    matched.append((ik, tuple(l_inst[ik]), tuple(r_inst[ik])))
                only_a += len(set(l_inst) - set(r_inst))
                only_b += len(set(r_inst) - set(l_inst))
                continue
        only_a += len(l_keys)
        only_b += len(r_keys)
    mode = "family_instance" if modes_used == {"family_instance"} else ("mixed" if len(modes_used) > 1 else "family_condition_instance")
    return mode, matched, only_a, only_b


def _compare_pair(left: _ArmEval, right: _ArmEval, *, alpha: float, seed: int) -> dict[str, Any]:
    mode, matched, only_a, only_b = _pair_key_groups(left, right)
    paired_a: list[float] = []
    paired_b: list[float] = []
    deltas: list[float] = []
    pass_pairs: list[tuple[bool, bool]] = []
    for _, l_rows, r_rows in matched:
        l_meas = [float(e.reward) for e in l_rows if e.measured and e.reward is not None]
        r_meas = [float(e.reward) for e in r_rows if e.measured and e.reward is not None]
        if not l_meas or not r_meas:
            continue
        ma = math.fsum(l_meas) / len(l_meas)
        mb = math.fsum(r_meas) / len(r_meas)
        delta = ma - mb
        if not math.isfinite(delta):
            continue
        paired_a.append(ma)
        paired_b.append(mb)
        deltas.append(delta)
        pass_pairs.append((ma >= _PASS_THRESHOLD, mb >= _PASS_THRESHOLD))
    n_paired = len(deltas)
    if n_paired > 0:
        sf_p_raw, sf_exact = _sign_flip_two_sided(deltas, seed)
        mc_p_raw, a_only, b_only = _mcnemar_two_sided(pass_pairs)
        sf_p: float | None = round(sf_p_raw, 8)
        mc_p: float | None = round(mc_p_raw, 8)
    else:
        sf_p, sf_exact, mc_p, a_only, b_only = None, False, None, 0, 0
    ls, rs = left.summary, right.summary
    fisher_p = fisher_exact_two_sided(ls["unmeasured"], ls["measured"], rs["unmeasured"], rs["measured"])
    mc_a, mc_b = finite_float(ls["mean_cost_usd"]), finite_float(rs["mean_cost_usd"])
    tc_a, tc_b = finite_float(ls["total_cost_usd"]), finite_float(rs["total_cost_usd"])
    det_deltas = {
        d: round(ls["detector_rates"][d] - rs["detector_rates"][d], 6)
        for d in DETECTOR_NAMES
        if ls["detector_rates"][d] > 0.0 or rs["detector_rates"][d] > 0.0
    }
    return {
        "arm_a": ls["arm"],
        "arm_b": rs["arm"],
        "pairing_mode": mode,
        "n_paired": n_paired,
        "zero_paired_keys": n_paired == 0,
        "only_in_a": only_a,
        "only_in_b": only_b,
        "mean_a": round(math.fsum(paired_a) / n_paired, 6) if n_paired else None,
        "mean_b": round(math.fsum(paired_b) / n_paired, 6) if n_paired else None,
        "mean_delta": round(math.fsum(deltas) / n_paired, 6) if n_paired else None,
        "sign_flip_p": sf_p,
        "sign_flip_exact": sf_exact,
        "discordant_a_only": a_only,
        "discordant_b_only": b_only,
        "mcnemar_p": mc_p,
        "attrition_table": [[ls["unmeasured"], ls["measured"]], [rs["unmeasured"], rs["measured"]]],
        "attrition_fisher_p": round(fisher_p, 8),
        "differential_attrition": fisher_p < alpha,
        "cost_delta_usd": round(mc_a - mc_b, 6) if (mc_a is not None and mc_b is not None) else None,
        "total_cost_delta_usd": round(tc_a - tc_b, 6) if (tc_a is not None and tc_b is not None) else None,
        "detector_deltas": det_deltas,
    }


def run_compare(
    arm_specs: list[str],
    *,
    html_path: Path | None = None,
    alpha: float = 0.05,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    seed: int = 0,
    triad: bool = False,
) -> tuple[dict[str, Any], int]:
    if not isinstance(arm_specs, list) or len(arm_specs) < 2:
        raise InputError("at least two arm paths are required")
    if len(arm_specs) > _MAX_ARMS:
        raise InputError(f"at most {_MAX_ARMS} arms may be compared at once")
    alpha_f = finite_float(alpha)
    stall_f = finite_float(stall_seconds)
    if alpha_f is None or not (0.0 < alpha_f < 1.0):
        raise InputError("--alpha must be in (0.0, 1.0)")
    if stall_f is None or stall_f <= 0.0:
        raise InputError("--stall-seconds must be positive")
    if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= seed <= _MAX_SEED):
        raise InputError("--seed must be in [0, 2**64 - 1]")
    parsed = [_parse_arm_spec(s) for s in arm_specs]
    raw_names = [lbl or p.resolve().name for lbl, p in parsed]
    names = raw_names if len(set(raw_names)) == len(raw_names) else [lbl or str(p) for lbl, p in parsed]
    if len(set(names)) != len(names):
        raise InputError("arm labels must be distinct")
    evaluated = [_evaluate_compare_arm(nm, p, stall_f) for nm, (_, p) in zip(names, parsed, strict=True)]
    pairs: list[dict[str, Any]] = []
    for idx_a in range(len(evaluated)):
        for idx_b in range(idx_a + 1, len(evaluated)):
            pairs.append(_compare_pair(evaluated[idx_a], evaluated[idx_b], alpha=alpha_f, seed=seed))
    arm_summaries = [ev.summary for ev in evaluated]
    cond_hints = {ev.summary["arm"]: {k[1] for k in ev.by_exact_key if k[1] != "default"} for ev in evaluated}
    triad_summary = compute_triad_decomposition(
        arm_summaries, pairs=pairs, condition_hints=cond_hints, require_triad=bool(triad)
    )
    diff_pairs = [f"{p['arm_a']} vs {p['arm_b']}" for p in pairs if p["differential_attrition"]]
    zero_pairs = [f"{p['arm_a']} vs {p['arm_b']}" for p in pairs if p["zero_paired_keys"]]
    rc = 1 if (diff_pairs or zero_pairs) else 0
    report: dict[str, Any] = {
        "command": "compare", "alpha": alpha_f, "stall_seconds": stall_f, "seed": seed,
        "passed": rc == 0, "differential_pairs": diff_pairs, "zero_paired_pairs": zero_pairs,
        "arms": arm_summaries, "pairs": pairs, "triad": triad_summary,
    }
    if html_path is not None:
        try:
            write_compare_html(Path(html_path), report)
        except (OSError, ValueError) as exc:
            raise InputError(f"cannot write --html {html_path}: {exc}") from None
    return report, rc
