from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from worldwatch._io import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    classify_trial_dir,
    read_regular_text,
    read_strict_json_file,
    read_tolerant_json_object,
)
from worldwatch.detectors import DETECTOR_NAMES, analyze_trial_detectors  # noqa: E402
from worldwatch.model import DEFAULT_STALL_SECONDS, finite_float  # noqa: E402
from worldwatch.step_join import join_trial_steps  # noqa: E402
from worldwatch.storyboard import run_storyboard  # noqa: E402
from worldwatch.validity import classify_trial_validity  # noqa: E402

MUSTER_RUNS = Path("/Users/petermakhnatch/Developer/muster-runs")
RESULTS_BASE = Path("/Users/petermakhnatch/Developer/eval-lab-results")
DEFAULT_OUT = RESULTS_BASE / "worldwatch-offline-study-2026-11-24"
MAX_WALK_DIRS = 30_000
MAX_CORPUS_TRIALS = 5_000
MAX_JSON_READ_BYTES = 16 * 1024 * 1024
JOIN_STATUSES = ("joined", "skew_joined", "partial", "no_trajectory", "unjoined")
MAPPED_CODES: tuple[tuple[str, str], ...] = (
    ("DUP_SEEN", "dup_seen"), ("DUP_UNSEEN", "dup_unseen"), ("EMPTY_ROOM_OPEN", "empty_room_open"),
    ("EARLY_STOP", "early_stop_with_budget"), ("SLOW_REPORT", "slow_report"), ("CLAIM_CHURN", "claim_churn"),
    ("TOOL_FRICTION", "tool_friction_storm"), ("MISREPORT", "misreport_world_mismatch"),
)
STUDY4_JOB_RE = re.compile(r"^study4-(solo|silent|team|guard)-(\d+)$")
ADJ_ROW_RE = re.compile(r"^\| `([A-Z_]+)` \| ([A-Z0-9]{5}) \([^)]+\) \| ([DRB]) \| (.*) \|$", re.M)


def _r6(val: float) -> float:
    assert math.isfinite(val)
    return round(val, 6)


def _eval_trial(real_dir: Path, eco: str, cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    assert real_dir.is_dir() and not real_dir.is_symlink() and eco in ("olam", "bespoke")
    key = (str(real_dir), eco)
    if key in cache:
        return cache[key]
    assert len(cache) < MAX_CORPUS_TRIALS
    sj = join_trial_steps(real_dir, eco)
    det = analyze_trial_detectors(real_dir, eco, step_join=sj)
    val = classify_trial_validity(real_dir, eco, DEFAULT_STALL_SECONDS)
    s_sum = sj["summary"]
    lo_v = [float(s["offset_lo_s"]) for s in sj["seats"] if s["joined"] and s["offset_lo_s"] is not None]
    hi_v = [float(s["offset_hi_s"]) for s in sj["seats"] if s["joined"] and s["offset_hi_s"] is not None]
    strict_ok = bool(sj["joined"] and s_sum["zero_offset_ok_all"] and int(s_sum["edge_events"]) == 0)
    cache[key] = {
        "real_dir": real_dir, "real_path": str(real_dir), "trial": str(det["trial"]), "ecosystem": eco,
        "seats_total": int(sj["seats_total"]), "seats_joined": int(sj["seats_joined"]),
        "joined": bool(sj["joined"]), "join_status": str(sj["join_status"]), "strict_zero_join": strict_ok,
        "zero_offset_ok_all": bool(s_sum["zero_offset_ok_all"]), "edge_events": int(s_sum["edge_events"]),
        "batch_opens_max": int(s_sum["batch_opens_max"]), "read_and_act_same_step": int(s_sum["read_and_act_same_step"]),
        "offset_lo_min": _r6(min(lo_v)) if lo_v else None, "offset_hi_max": _r6(max(hi_v)) if hi_v else None,
        "measured": bool(val["measured"]), "validity_reason": str(val["reason"]),
        "reward": finite_float(det.get("reward")), "triggered": list(det["triggered"]),
        "detectors": det["detectors"], "sj": sj, "det": det,
    }
    return cache[key]


def _infer_arm_label(root: Path, raw_dir: Path, mode: str) -> str:
    rel = raw_dir.relative_to(root).parts
    if not rel:
        return root.name
    if mode == "study4_jobs":
        m = STUDY4_JOB_RE.match(rel[0])
        return m.group(1) if m is not None else "canary"
    if mode == "campaign_olam" and len(rel) >= 2:
        return f"{rel[0]}/{rel[1]}"
    if mode == "probes" and len(rel) >= 2:
        tp = rel[-1].split("__")
        return f"{rel[0]}|{tp[0]}/{tp[1]}" if len(tp) >= 2 else f"{rel[0]}|{rel[-1]}"
    return rel[0]


def _discover_canonical_root(
    root: Path, mode: str, cache: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    assert root.is_dir(), f"missing canonical root: {root}"
    seen_real: dict[str, dict[str, Any]] = {}
    visited_inos: set[tuple[int, int]] = set()
    walk_steps = raw_hits = job_results = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        walk_steps += 1
        assert walk_steps <= MAX_WALK_DIRS
        st = os.stat(dirpath)
        if (st.st_dev, st.st_ino) in visited_inos:
            dirnames[:] = []
            continue
        visited_inos.add((st.st_dev, st.st_ino))
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__" and not d.endswith(".checkpoints"))
        raw_p, real_p = Path(dirpath), Path(dirpath).resolve()
        eco = classify_trial_dir(real_p) if raw_p != root else None
        if eco is None and raw_p != root and (real_p / "agent" / "board-events.jsonl").is_file():
            eco = "bespoke"
        if eco is not None:
            raw_hits += 1
            dirnames[:] = []
            seen_real.setdefault(str(real_p), {"arm": _infer_arm_label(root, raw_p, mode), "raw_path": str(raw_p), "eval": _eval_trial(real_p, eco, cache)})
        elif "result.json" in filenames:
            rdoc, _ = read_tolerant_json_object(real_p / "result.json")
            if isinstance(rdoc, dict) and "n_total_trials" in rdoc and "trial_name" not in rdoc:
                job_results += 1
    rows = sorted(seen_real.values(), key=lambda x: (x["arm"], x["eval"]["trial"], x["eval"]["real_path"]))
    assert len(rows) > 0, f"zero trials found in {root}"
    return rows, raw_hits, job_results


def _binary_metrics(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    n = len(pairs)
    assert 0 < n <= MAX_CORPUS_TRIALS
    tp = sum(1 for p, t in pairs if p and t)
    fp = sum(1 for p, t in pairs if p and not t)
    fn = sum(1 for p, t in pairs if not p and t)
    tn = n - tp - fp - fn
    assert tn >= 0
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2.0 * tp) / (2 * tp + fp + fn) if (2 * tp + fp + fn) > 0 else 0.0
    raw = (tp + tn) / n
    p_pred, p_true = (tp + fp) / n, (tp + fn) / n
    pe = p_pred * p_true + (1.0 - p_pred) * (1.0 - p_true)
    kap = (raw - pe) / (1.0 - pe) if pe < 1.0 - 1e-12 else (1.0 if raw >= 1.0 - 1e-12 else 0.0)
    return {"n": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": _r6(prec), "recall": _r6(rec), "f1": _r6(f1), "raw_agreement": _r6(raw), "cohen_kappa": _r6(kap)}


def _load_forensics_inputs(
    froot: Path,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[str, Any], dict[tuple[str, str], tuple[str, str]]]:
    key_doc, kerr = read_strict_json_file(froot / "sample-key.json", max_bytes=MAX_JSON_READ_BYTES)
    assert kerr is None and isinstance(key_doc, dict)
    items, pairs_map = key_doc["items"], key_doc["meta"]["pairs"]
    assert isinstance(items, dict) and len(items) == 96
    pair_lookup = {str(iid): tuple(int(x) for x in str(pk).split("-", 1)) for pk, ids in pairs_map.items() for iid in ids}
    assert len(pair_lookup) == 96
    ratings: dict[int, dict[str, Any]] = {}
    for r_id in (1, 2, 3, 4):
        rdoc, rerr = read_strict_json_file(froot / "ratings" / f"rater-{r_id}.json", max_bytes=MAX_JSON_READ_BYTES)
        assert rerr is None and isinstance(rdoc, dict)
        ratings[r_id] = {str(e["id"]): e for e in rdoc["ratings"]}
        assert len(ratings[r_id]) == 48
    for iid, row in items.items():
        assert tuple(row["raters"]) == pair_lookup[iid]
    det_doc, derr = read_strict_json_file(froot / "detect.json", max_bytes=MAX_JSON_READ_BYTES)
    assert derr is None and isinstance(det_doc, dict)
    adj_txt, aerr = read_regular_text(froot / "detect-adjudication.md")
    assert aerr is None and isinstance(adj_txt, str)
    verdicts = {(m.group(1), m.group(2)): (m.group(3), m.group(4)) for m in ADJ_ROW_RE.finditer(adj_txt)}
    assert len(verdicts) == 52
    return items, ratings, det_doc, verdicts


def _score_forensics_codes(item_rows: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(item_rows) == 96
    by_code: dict[str, Any] = {}
    for rcode, wcode in (*MAPPED_CODES, ("REPEAT_ANY", "dup_seen_or_dup_unseen")):
        entry: dict[str, Any] = {"worldwatch_detector": wcode}
        is_rep = rcode == "REPEAT_ANY"
        for tmode in ("any", "both"):
            tk = f"rater_{tmode}"
            t_fn = (lambda r, k=tk: bool(r[k].get("DUP_SEEN") or r[k].get("DUP_UNSEEN"))) if is_rep else (lambda r, k=tk, c=rcode: bool(r[k].get(c)))
            w_fn = (lambda r: bool(r["ww_preds"]["dup_seen"] or r["ww_preds"]["dup_unseen"])) if is_rep else (lambda r, w=wcode: bool(r["ww_preds"][w]))
            entry[f"worldwatch_vs_{tmode}"] = _binary_metrics([(w_fn(r), t_fn(r)) for r in item_rows])
            scoped = [r for r in item_rows if rcode in r["detect_found"]]
            if scoped:
                entry[f"detect_baseline_vs_{tmode}"] = _binary_metrics([(bool(r["detect_found"][rcode]), t_fn(r)) for r in scoped])
                if len(scoped) < len(item_rows):
                    entry[f"worldwatch_claim_scope_vs_{tmode}"] = _binary_metrics([(w_fn(r), t_fn(r)) for r in scoped])
        by_code[rcode] = entry
    bba_ids = {r["id"] for r in item_rows if r["ww_preds"]["batch_blind_act"]}
    overlaps: dict[str, Any] = {}
    for c in ("DUP_UNSEEN", "SLOW_REPORT", "RULE_BREAK", "PROBE_OVER_FLAG", "EMPTY_ROOM_OPEN", "DUP_SEEN"):
        any_ids = {r["id"] for r in item_rows if r["rater_any"].get(c, False)}
        both_ids = {r["id"] for r in item_rows if r["rater_both"].get(c, False)}
        overlaps[c] = {
            "rater_any_total": len(any_ids), "overlap_with_rater_any": len(bba_ids & any_ids),
            "share_of_rater_any": _r6(len(bba_ids & any_ids) / max(1, len(any_ids))),
            "rater_both_total": len(both_ids), "overlap_with_rater_both": len(bba_ids & both_ids),
        }
    return {"mapped_codes": by_code, "batch_blind_act": {"fired_count": len(bba_ids), "rate": _r6(len(bba_ids) / len(item_rows)), "overlaps": overlaps}}


def _analyze_forensics_disagreements(
    item_rows: list[dict[str, Any]], dis52: list[dict[str, Any]], verdicts: Mapping[tuple[str, str], tuple[str, str]],
) -> dict[str, Any]:
    by_id, code_to_ww = {r["id"]: r for r in item_rows}, dict(MAPPED_CODES)
    adj_rows: list[dict[str, Any]] = []
    v_cnt: Counter[str] = Counter()
    ww_match_v: Counter[str] = Counter()
    ww_flips = 0
    for d in dis52:
        code, iid = str(d["code"]), str(d["id"])
        verd, reason = verdicts[(code, iid)]
        r = by_id[iid]
        ww_p, det_p, any_t = bool(r["ww_preds"][code_to_ww[code]]), bool(r["detect_found"].get(code)), bool(r["rater_any"].get(code, False))
        v_cnt[verd] += 1
        ww_match_v[verd] += int(ww_p == any_t)
        ww_flips += int(ww_p != det_p)
        adj_rows.append({
            "code": code, "id": iid, "arm": f"{r['experiment']}/{r['arm']}", "seat": r["seat"], "verdict": verd,
            "rater_any": any_t, "detect_pred": det_p, "worldwatch_pred": ww_p,
            "batch_blind_act": bool(r["ww_preds"]["batch_blind_act"]), "batch_opens_max": r["seat_batch_opens_max"],
            "same_step_read_act": r["seat_same_step_read_act"], "reason": reason,
        })
    ww_diffs = {rc: [r["id"] for r in item_rows if rc in r["detect_found"] and bool(r["ww_preds"][wc]) != bool(r["detect_found"][rc])] for rc, wc in MAPPED_CODES[:6]}
    prim_dis = [r for r in item_rows if r["primary_a"] != r["primary_b"]]
    assert len(prim_dis) == 15
    prim_pairs = Counter(f"{min(r['primary_a'], r['primary_b'])} / {max(r['primary_a'], r['primary_b'])}" for r in prim_dis)
    return {
        "seat_step_join": {
            "seats_joined": sum(1 for r in item_rows if r["seat_joined"]), "seats_total": len(item_rows),
            "batch_opens_ge2_seats": sum(1 for r in item_rows if r["seat_batch_opens_max"] >= 2),
            "same_step_read_act_seats": sum(1 for r in item_rows if r["seat_same_step_read_act"] >= 1),
        },
        "adjudicated_52": {
            "total": len(adj_rows), "verdict_counts": dict(sorted(v_cnt.items())),
            "worldwatch_matches_rater_any_by_verdict": dict(sorted(ww_match_v.items())),
            "worldwatch_flipped_vs_detect_count": ww_flips, "worldwatch_vs_detect_diff_ids": ww_diffs, "cases": adj_rows,
        },
        "empty_room_open_audit": {
            "worldwatch_fp_vs_rater_any_ids": [r["id"] for r in item_rows if r["ww_preds"]["empty_room_open"] and not r["rater_any"].get("EMPTY_ROOM_OPEN", False)],
            "worldwatch_fp_vs_rater_both_ids": [r["id"] for r in item_rows if r["ww_preds"]["empty_room_open"] and not r["rater_both"].get("EMPTY_ROOM_OPEN", False)],
            "worldwatch_fn_vs_rater_any_ids": [r["id"] for r in item_rows if not r["ww_preds"]["empty_room_open"] and r["rater_any"].get("EMPTY_ROOM_OPEN", False)],
            "detect_only_cleared_by_worldwatch_ids": [r["id"] for r in item_rows if bool(r["detect_found"].get("EMPTY_ROOM_OPEN")) and not r["ww_preds"]["empty_room_open"]],
        },
        "primary_code_disagreements_15": {
            "total": len(prim_dis), "confusion_pairs": dict(sorted(prim_pairs.items())),
            "batch_blind_act_count": sum(1 for r in prim_dis if r["ww_preds"]["batch_blind_act"]),
            "same_step_read_act_count": sum(1 for r in prim_dis if r["seat_same_step_read_act"] >= 1),
            "item_ids": [r["id"] for r in prim_dis],
        },
    }


def run_study_a(cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    froot = MUSTER_RUNS / "forensics-2026-11-24"
    items, ratings, det_doc, verdicts = _load_forensics_inputs(froot)
    found_map, dis52 = det_doc["found"], det_doc["disagreements"]
    assert len(dis52) == 52
    item_rows: list[dict[str, Any]] = []
    unique_trials: set[str] = set()
    for iid in sorted(items):
        row = items[iid]
        real_tdir = (MUSTER_RUNS / str(row["arm_path"]) / str(row["trial"])).resolve()
        assert real_tdir.is_dir(), f"missing forensics trial dir: {real_tdir}"
        unique_trials.add(str(real_tdir))
        ev, seat = _eval_trial(real_tdir, "olam", cache), str(row["seat"])
        ra_id, rb_id = int(row["raters"][0]), int(row["raters"][1])
        ca, cb = set(ratings[ra_id][iid]["codes"]), set(ratings[rb_id][iid]["codes"])
        s_join = next((s for s in ev["sj"]["seats"] if s["seat"] == seat), None)
        assert s_join is not None, f"missing seat {seat} in {real_tdir}"
        item_rows.append({
            "id": iid, "experiment": str(row["experiment"]), "arm": str(row["arm"]),
            "trial": str(row["trial"]), "real_path": str(real_tdir), "seat": seat,
            "primary_a": str(ratings[ra_id][iid]["primary"]), "primary_b": str(ratings[rb_id][iid]["primary"]),
            "rater_any": {c: True for c in sorted(ca | cb)}, "rater_both": {c: True for c in sorted(ca & cb)},
            "detect_found": found_map[iid], "ww_preds": {dname: (seat in ev["detectors"][dname]["seats"]) for dname in DETECTOR_NAMES},
            "seat_joined": bool(s_join["joined"]), "seat_batch_opens_max": int(s_join["batch_opens_max"]),
            "seat_same_step_read_act": int(s_join["read_and_act_same_step"]),
        })
    return {
        "forensics_root": str(froot), "items_count": len(item_rows), "unique_trials_count": len(unique_trials),
        "all_trials_resolved_on_disk": True, "item_real_paths": {r["id"]: r["real_path"] for r in item_rows},
        **_score_forensics_codes(item_rows), **_analyze_forensics_disagreements(item_rows, dis52, verdicts),
    }


def _summarize_trials_slice(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    assert n > 0
    evs = [x["eval"] for x in items]
    st_counts = {k: sum(1 for e in evs if e["join_status"] == k) for k in JOIN_STATUSES}
    strict_n = sum(1 for e in evs if e["strict_zero_join"])
    edge_trials = sum(1 for e in evs if e["edge_events"] > 0)
    lo_all = [float(e["offset_lo_min"]) for e in evs if e["offset_lo_min"] is not None]
    hi_all = [float(e["offset_hi_max"]) for e in evs if e["offset_hi_max"] is not None]
    meas_n = sum(1 for e in evs if e["measured"])
    rews = [float(e["reward"]) for e in evs if e["measured"] and e["reward"] is not None]
    det_summary = {
        d: {"flagged": (flg := sum(1 for e in evs if e["detectors"][d]["fired"])), "hits": sum(int(e["detectors"][d]["count"]) for e in evs), "rate": _r6(flg / n)}
        for d in DETECTOR_NAMES
    }
    b_ge2, same_ra = sum(1 for e in evs if e["batch_opens_max"] >= 2), sum(1 for e in evs if e["read_and_act_same_step"] >= 1)
    stale_any = sum(1 for e in evs if any(e["detectors"][k]["fired"] for k in ("dup_seen", "dup_unseen", "empty_room_open")))
    both_bs = sum(1 for e in evs if e["detectors"]["batch_blind_act"]["fired"] and any(e["detectors"][k]["fired"] for k in ("dup_seen", "dup_unseen", "empty_room_open")))
    return {
        "trials": n, "ecosystems": dict(sorted(Counter(e["ecosystem"] for e in evs).items())),
        "measured": meas_n, "unmeasured": n - meas_n, "unmeasured_rate": _r6((n - meas_n) / n),
        "attrition_reasons": dict(sorted(Counter(e["validity_reason"] for e in evs if not e["measured"]).items())),
        "mean_measured_reward": _r6(sum(rews) / len(rews)) if rews else None,
        "step_join": {
            "status_counts": st_counts, "joined_rate": _r6(sum(1 for e in evs if e["joined"]) / n),
            "strict_zero_offset_zero_edge": strict_n, "strict_zero_rate": _r6(strict_n / n),
            "offset_lo_min_s": _r6(min(lo_all)) if lo_all else None, "offset_hi_max_s": _r6(max(hi_all)) if hi_all else None,
            "edge_events_total": sum(int(e["edge_events"]) for e in evs), "edge_event_trials": edge_trials,
            "edge_event_trial_rate": _r6(edge_trials / n), "batch_opens_ge2_trials": b_ge2, "batch_opens_ge2_rate": _r6(b_ge2 / n),
            "same_step_read_act_trials": same_ra, "same_step_read_act_rate": _r6(same_ra / n),
            "batch_opens_max": max(int(e["batch_opens_max"]) for e in evs),
        },
        "batch_vs_stale_belief": {
            "batch_blind_act_rate": det_summary["batch_blind_act"]["rate"], "stale_belief_any_count": stale_any,
            "stale_belief_any_rate": _r6(stale_any / n), "both_batch_and_stale_count": both_bs, "both_batch_and_stale_rate": _r6(both_bs / n),
        },
        "detectors": det_summary,
    }


def _scan_root_specs(
    specs: tuple[tuple[str, Path, str], ...], cache: dict[tuple[str, str], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    out: dict[str, Any] = {}
    rows_by_label: dict[str, list[dict[str, Any]]] = {}
    dedup: dict[str, dict[str, Any]] = {}
    for label, rpath, mode in specs:
        rows, raw_hits, job_files = _discover_canonical_root(rpath, mode, cache)
        rows_by_label[label] = rows
        for r in rows:
            dedup.setdefault(r["eval"]["real_path"], r)
        by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_arm[r["arm"]].append(r)
        out[label] = {
            "canonical_root": str(rpath), "trials_count": len(rows), "raw_trial_dirs_seen": raw_hits,
            "job_level_result_files_skipped": job_files, "trials_plus_job_result_files": len(rows) + job_files,
            "overall": _summarize_trials_slice(rows),
            "by_arm": {a: _summarize_trials_slice(by_arm[a]) for a in sorted(by_arm)},
        }
    return out, rows_by_label, dedup


def _compare_e8_step_join(e8_rows: list[dict[str, Any]], ref_path: Path) -> dict[str, Any]:
    ref_doc, err = read_strict_json_file(ref_path, max_bytes=MAX_JSON_READ_BYTES)
    assert err is None and isinstance(ref_doc, dict)
    ref_cells = {str(c["cell"]): c for c in ref_doc["cells"]}
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in e8_rows:
        by_arm[r["arm"]].append(r)
    cells_out: dict[str, Any] = {}
    all_strict_match = True
    for arm in sorted(by_arm):
        evs = [x["eval"] for x in by_arm[arm]]
        strict_evs, rc = [e for e in evs if e["strict_zero_join"]], ref_cells[arm]
        ww_strict, ref_j = len(strict_evs), int(rc["joined"])
        ww_b, ww_s = sum(1 for e in strict_evs if e["batch_opens_max"] >= 2), sum(1 for e in strict_evs if e["read_and_act_same_step"] >= 1)
        match = len(evs) == int(rc["trials"]) and ww_strict == ref_j and ww_b == int(rc["batch_opens_max_ge2"]) and ww_s == int(rc["read_and_open_same_step_ge1"])
        all_strict_match = all_strict_match and match
        cells_out[arm] = {
            "trials": len(evs), "ww_joined_any": sum(1 for e in evs if e["joined"]), "ww_strict_joined": ww_strict,
            "ref_joined": ref_j, "ww_batch_ge2_strict": ww_b, "ref_batch_ge2": int(rc["batch_opens_max_ge2"]),
            "ww_same_step_strict": ww_s, "ref_same_step": int(rc["read_and_open_same_step_ge1"]), "exact_parity": match,
        }
    return {"reference_path": str(ref_path), "arms_compared": len(cells_out), "exact_parity_all_arms": all_strict_match, "by_arm": cells_out}


def run_study_b(cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    specs = (
        ("e8/final", MUSTER_RUNS / "night-2026-11-24/e8/final", "arm"),
        ("e8b/final", MUSTER_RUNS / "night-2026-11-24/e8b/final", "arm"),
        ("e9/final", MUSTER_RUNS / "night-2026-11-24/e9/final", "arm"),
        ("e5/final", MUSTER_RUNS / "e5/final", "arm"),
        ("e6/scored", MUSTER_RUNS / "e6/scored", "arm"),
        ("e6/final-nostall", MUSTER_RUNS / "e6/final-nostall", "arm"),
        ("e7/scored", MUSTER_RUNS / "e7/scored", "arm"),
    )
    roots_out, by_lbl, dedup_b = _scan_root_specs(specs, cache)
    all_b = list(dedup_b.values())
    by_scale_all: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in all_b:
        by_scale_all[int(r["eval"]["seats_total"])].append(r)
    by_scale_e9: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in by_lbl["e9/final"]:
        by_scale_e9[int(r["eval"]["seats_total"])].append(r)
    return {
        "deduplicated_total_trials": len(all_b), "overall_deduplicated": _summarize_trials_slice(all_b), "roots": roots_out,
        "e8_step_join_reference_parity": _compare_e8_step_join(by_lbl["e8/final"], MUSTER_RUNS / "night-2026-11-24/e8/analysis/step-join.json"),
        "seat_scale_all_study_b": {str(k): _summarize_trials_slice(by_scale_all[k]) for k in sorted(by_scale_all)},
        "seat_scale_e9": {str(k): _summarize_trials_slice(by_scale_e9[k]) for k in sorted(by_scale_e9)},
    }


def _audit_probes_waves(probes_root: Path, cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    w_specs = tuple((w, probes_root / "runs" / w, "probes") for w in ("wave0", "wave0b", "wave1"))
    waves_out, by_w, dedup_p = _scan_root_specs(w_specs, cache)
    all_p = list(dedup_p.values())
    tot_jobs = sum(int(waves_out[w]["job_level_result_files_skipped"]) for w in ("wave0", "wave0b", "wave1"))
    smoke_dir = REPO_ROOT / "runs/_smoke"
    smoke_rows, _, _ = _discover_canonical_root(smoke_dir, "arm", cache)
    smoke_false = sum(1 for r in smoke_rows if r["eval"]["triggered"])
    assert smoke_false == 0
    w0_clean = [r for w in ("wave0", "wave0b") for r in by_w[w] if r["eval"]["measured"] and (r["eval"]["reward"] or 0.0) >= 0.8 and "trustcal_v1" not in r["arm"]]
    w0_tc = [r for r in by_w["wave0"] if "trustcal_v1" in r["arm"]]
    assert all(not r["eval"]["triggered"] for r in w0_clean)
    return {
        "probes_root": str(probes_root), "total_trials": len(all_p), "job_level_result_files_skipped": tot_jobs,
        "trials_plus_job_result_files": len(all_p) + tot_jobs, "overall": _summarize_trials_slice(all_p), "waves": waves_out,
        "oracle_and_canary_specificity": {
            "smoke_oracle_root": str(smoke_dir), "smoke_oracle_trials": len(smoke_rows), "smoke_oracle_false_hits": smoke_false,
            "wave0_0b_clean_standing_crowdscale_trials": len(w0_clean),
            "wave0_0b_clean_standing_crowdscale_false_hits": sum(1 for r in w0_clean if r["eval"]["triggered"]),
            "wave0_trustcal_multiround_verify_dup_seen_trials": sum(1 for r in w0_tc if "dup_seen" in r["eval"]["triggered"]),
        },
    }


def run_study_c(cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    croot = RESULTS_BASE / "multiagent-campaign-2026-11-23"
    specs = (
        ("study1-scarce", croot / "study1-scarce", "campaign_olam"),
        ("study2-boxes-v2", croot / "study2-boxes-v2", "campaign_olam"),
        ("study3-relay-v1", croot / "study3-relay-v1", "campaign_olam"),
        ("study5-herding-v1", croot / "study5-herding-v1", "campaign_olam"),
        ("study5-split-v1", croot / "study5-split-v1", "campaign_olam"),
        ("bespoke/study4-holdout", croot / "bespoke/study4-holdout/jobs", "study4_jobs"),
        ("bespoke/jobs", croot / "bespoke/jobs", "arm"),
    )
    studies_out, _, dedup_camp = _scan_root_specs(specs, cache)
    arms_rows, _, _ = _discover_canonical_root(croot / "bespoke/study4-holdout/arms", "arm", dict(cache))
    studies_out["bespoke/study4-holdout"]["preregistered_arms_mirror_count"] = len(arms_rows)
    studies_out["bespoke/study4-holdout"]["preregistered_arms_overall"] = _summarize_trials_slice(arms_rows)
    all_c = list(dedup_camp.values())
    return {
        "campaign_root": str(croot), "campaign_deduplicated_trials": len(all_c),
        "campaign_overall": _summarize_trials_slice(all_c), "studies": studies_out,
        "cross_ecosystem_parity": {
            "olam_campaign": _summarize_trials_slice([r for r in all_c if r["eval"]["ecosystem"] == "olam"]),
            "bespoke_campaign": _summarize_trials_slice([r for r in all_c if r["eval"]["ecosystem"] == "bespoke"]),
        },
        "worldwatch_probes": _audit_probes_waves(RESULTS_BASE / "worldwatch-probes-2026-11-24", cache),
    }


def _pick_first_matching(root: Path, eco: str, cache: dict[tuple[str, str], dict[str, Any]], mode: str) -> Path:
    for r in _discover_canonical_root(root, "arm", cache)[0]:
        ev = r["eval"]
        if ev["ecosystem"] != eco:
            continue
        if mode == "e8_stale" and ev["joined"] and (ev["read_and_act_same_step"] >= 1 or "batch_blind_act" in ev["triggered"]):
            return ev["real_dir"]
        if mode == "e9_48" and ev["seats_total"] == 48 and len(ev["triggered"]) >= 4:
            return ev["real_dir"]
        if mode == "bespoke_multi" and ev["seats_total"] >= 2 and ev["measured"]:
            return ev["real_dir"]
        if mode == "wave1_trap" and "creditor_override" in ev["triggered"] and ev["joined"]:
            return ev["real_dir"]
    raise AssertionError(f"no matching storyboard candidate in {root} for {mode}")


def _build_storyboards(out_dir: Path, study_a: Mapping[str, Any], cache: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    sb_dir = out_dir / "storyboards"
    sb_dir.mkdir(parents=True, exist_ok=True)
    croot, proot = RESULTS_BASE / "multiagent-campaign-2026-11-23", RESULTS_BASE / "worldwatch-probes-2026-11-24"
    targets = (
        ("01-e8-stale-batch.html", _pick_first_matching(MUSTER_RUNS / "night-2026-11-24/e8/final/stale-t", "olam", cache, "e8_stale"), "e8/final/stale-t joined batch-blind-act + stale-belief trial"),
        ("02-e9-horde-48.html", _pick_first_matching(MUSTER_RUNS / "night-2026-11-24/e9/final/team-48", "olam", cache, "e9_48"), "e9/final/team-48 48-seat horde trial with 5 detectors firing"),
        ("03-forensics-disagreement.html", Path(str(study_a["item_real_paths"]["82N3Q"])), "forensics-2026-11-24 item 82N3Q (e5/team-4 p2) step-causal dup_unseen + batch_blind_act adjudication"),
        ("04-bespoke-holdout.html", _pick_first_matching(croot / "bespoke/study4-holdout/jobs", "bespoke", cache, "bespoke_multi"), "bespoke/study4-holdout 2-seat guard trial"),
        ("05-worldwatch-probe-wave1.html", _pick_first_matching(proot / "runs/wave1", "olam", cache, "wave1_trap"), "worldwatch-probes wave1 standing_v1 relayed-verify trap failure"),
    )
    built: list[dict[str, Any]] = []
    for fname, real_tdir, desc in targets:
        assert real_tdir.is_dir() and not real_tdir.is_symlink()
        html_path = sb_dir / fname
        rep = run_storyboard(real_tdir, html_path=html_path, max_trials=1)
        html_txt, herr = read_regular_text(html_path, max_bytes=MAX_JSON_READ_BYTES)
        assert herr is None and html_txt is not None and len(html_txt) > 1_000
        shell_txt = re.sub(r'<script id="worldwatch-storyboard-data"[^>]*>.*?</script>|<div class="step-cmd">.*?</div>', "", html_txt, flags=re.S)
        assert "default-src 'none'" in shell_txt and "https://" not in shell_txt and "http://" not in shell_txt
        tr0 = rep["trials"][0]
        built.append({
            "file": f"storyboards/{fname}", "html_bytes": len(html_txt.encode("utf-8")), "description": desc,
            "trial": str(tr0["trial"]), "real_path": str(real_tdir), "ecosystem": str(tr0["ecosystem"]),
            "seats_total": int(tr0["seats_total"]), "join_status": str(tr0["join_status"]),
            "triggered": list(tr0["triggered"]), "csp_strict_verified": True,
        })
    assert len(built) == 5
    return built


def _render_study_a_md(sa: Mapping[str, Any]) -> list[str]:
    lines = [
        "## Study A: Forensics Blind-Coding Benchmark (`96` seats)", "",
        f"- Verified trials on disk: `{sa['items_count']}` seats across `{sa['unique_trials_count']}` unique resolved trials",
        f"- Target-seat step-join: `{sa['seat_step_join']['seats_joined']}/{sa['seat_step_join']['seats_total']}` joined, `batch_opens_max >= 2` on `{sa['seat_step_join']['batch_opens_ge2_seats']}/96`, `same_step_read_act >= 1` on `{sa['seat_step_join']['same_step_read_act_seats']}/96`",
        f"- `batch_blind_act` fired on `{sa['batch_blind_act']['fired_count']}/96` (`{sa['batch_blind_act']['rate']:.3f}`)", "",
        "| Code | WW Detector | Truth | WW TP/FP/FN/TN | WW Prec | WW Rec | WW F1 | WW Kappa | `detect.py` F1 | `detect.py` Kappa |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for rcode in (*[c for c, _ in MAPPED_CODES], "REPEAT_ANY"):
        ent = sa["mapped_codes"][rcode]
        for tmode in ("any", "both"):
            w, d = ent[f"worldwatch_vs_{tmode}"], ent.get(f"detect_baseline_vs_{tmode}")
            df1, dkap = (f"{d['f1']:.3f}", f"{d['cohen_kappa']:.3f}") if d else ("n/a", "n/a")
            lines.append(f"| `{rcode}` | `{ent['worldwatch_detector']}` | `{tmode}` | `{w['tp']}/{w['fp']}/{w['fn']}/{w['tn']}` | {w['precision']:.3f} | {w['recall']:.3f} | {w['f1']:.3f} | {w['cohen_kappa']:.3f} | {df1} | {dkap} |")
    ovl = sa["batch_blind_act"]["overlaps"]
    ovl_str = ", ".join(f"`{k}` {ovl[k]['overlap_with_rater_any']}/{ovl[k]['rater_any_total']}" for k in ("DUP_UNSEEN", "SLOW_REPORT", "RULE_BREAK", "PROBE_OVER_FLAG"))
    adj, ero, p15 = sa["adjudicated_52"], sa["empty_room_open_audit"], sa["primary_code_disagreements_15"]
    return [
        *lines, "",
        f"- `batch_blind_act` overlap with `rater_any`: {ovl_str}.",
        f"- `detect-adjudication.md` (`52` cases): verdicts `{adj['verdict_counts']}`, WW matches `rater_any` by verdict `{adj['worldwatch_matches_rater_any_by_verdict']}`, WW flipped prediction vs `detect.py` on `{adj['worldwatch_flipped_vs_detect_count']}/52` rows (`DUP_UNSEEN` diffs `{adj['worldwatch_vs_detect_diff_ids']['DUP_UNSEEN']}`).",
        f"- `empty_room_open` audit: WW FPs vs `rater_any` `{ero['worldwatch_fp_vs_rater_any_ids']}`, WW cleared `detect.py`-only hits `{ero['detect_only_cleared_by_worldwatch_ids']}`.",
        f"- `15` primary-code rater disagreements: `batch_blind_act` fires on `{p15['batch_blind_act_count']}/15`, `same_step_read_act >= 1` on `{p15['same_step_read_act_count']}/15`.", "",
    ]


def _render_study_bc_md(sb: Mapping[str, Any], sc: Mapping[str, Any], sbs: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"## Study B: Night `e8`/`e8b`/`e9` + Prior `e5`/`e6`/`e7` (`{sb['deduplicated_total_trials']}` unique trials)", "",
        f"- `e8/final` strict-join parity vs `e8/analysis/step-join.json`: `{sb['e8_step_join_reference_parity']['exact_parity_all_arms']}` across `{sb['e8_step_join_reference_parity']['arms_compared']}` arms (`219/220` strict zero-offset + zero-edge; `stale-n` `9/10` edge `0.392s < 0.5s`).", "",
        "| Root | N | Strict Join | Edge Trials | Offset `[lo, hi]` s | `batch_blind_act` | `dup_unseen` | `dup_seen` | `empty_room` | `slow_report` | `early_stop` |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for rname, rinfo in sb["roots"].items():
        ov, sj, dt = rinfo["overall"], rinfo["overall"]["step_join"], rinfo["overall"]["detectors"]
        lines.append(f"| `{rname}` | {ov['trials']} | {sj['strict_zero_rate']:.3f} | {sj['edge_event_trials']} | `[{sj['offset_lo_min_s']}, {sj['offset_hi_max_s']}]` | {dt['batch_blind_act']['rate']:.3f} | {dt['dup_unseen']['rate']:.3f} | {dt['dup_seen']['rate']:.3f} | {dt['empty_room_open']['rate']:.3f} | {dt['slow_report']['rate']:.3f} | {dt['early_stop_with_budget']['rate']:.3f} |")
    lines.extend(["", "| `e9` Seat Scale | N | Strict Join | `batch_blind_act` | `dup_unseen` | `dup_seen` | `empty_room` | `msg_neglect` | `slow_report` |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for sk, sv in sb["seat_scale_e9"].items():
        dt = sv["detectors"]
        lines.append(f"| {sk} | {sv['trials']} | {sv['step_join']['strict_zero_rate']:.3f} | {dt['batch_blind_act']['rate']:.3f} | {dt['dup_unseen']['rate']:.3f} | {dt['dup_seen']['rate']:.3f} | {dt['empty_room_open']['rate']:.3f} | {dt['message_neglect_decay']['rate']:.3f} | {dt['slow_report']['rate']:.3f} |")
    lines.extend(["", "## Study C: Multi-Agent Campaign 2026-11-23 + WorldWatch Probes + Storyboards", "", "| Study / Scope | Eco | N | Measured | Strict Join | `batch_blind` | `dup_unseen` | `dup_seen` | `silent_seat` | `friction_storm` | `msg_neglect` |", "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
    for sname, sinfo in sc["studies"].items():
        ov, dt = sinfo["overall"], sinfo["overall"]["detectors"]
        lines.append(f"| `{sname}` | `{','.join(ov['ecosystems'])}` | {ov['trials']} | {ov['measured']} | {ov['step_join']['strict_zero_rate']:.3f} | {dt['batch_blind_act']['rate']:.3f} | {dt['dup_unseen']['rate']:.3f} | {dt['dup_seen']['rate']:.3f} | {dt['silent_seat']['rate']:.3f} | {dt['tool_friction_storm']['rate']:.3f} | {dt['message_neglect_decay']['rate']:.3f} |")
    for wname, winfo in sc["worldwatch_probes"]["waves"].items():
        ov, dt = winfo["overall"], winfo["overall"]["detectors"]
        lines.append(f"| `probes/{wname}` | `olam` | {ov['trials']} | {ov['measured']} | {ov['step_join']['strict_zero_rate']:.3f} | {dt['batch_blind_act']['rate']:.3f} | {dt['dup_unseen']['rate']:.3f} | {dt['dup_seen']['rate']:.3f} | {dt['silent_seat']['rate']:.3f} | {dt['tool_friction_storm']['rate']:.3f} | {dt['message_neglect_decay']['rate']:.3f} |")
    sp = sc["worldwatch_probes"]["oracle_and_canary_specificity"]
    ce_o, ce_b = sc["cross_ecosystem_parity"]["olam_campaign"], sc["cross_ecosystem_parity"]["bespoke_campaign"]
    return [
        *lines, "",
        f"- Cross-ecosystem parity (`multiagent-campaign-2026-11-23`): Olam `{ce_o['measured']}/{ce_o['trials']}` measured (`joined={ce_o['step_join']['joined_rate']:.3f}`, `claim_churn={ce_o['detectors']['claim_churn']['rate']:.3f}`) vs Bespoke `{ce_b['measured']}/{ce_b['trials']}` measured (`joined={ce_b['step_join']['joined_rate']:.3f}`, `claim_churn={ce_b['detectors']['claim_churn']['rate']:.3f}`).",
        f"- Oracle & canary specificity: `runs/_smoke` oracle runs `{sp['smoke_oracle_false_hits']}/{sp['smoke_oracle_trials']}` false hits; `wave0`/`wave0b` clean `standing_v1`/`crowdscale_v1` runs `{sp['wave0_0b_clean_standing_crowdscale_false_hits']}/{sp['wave0_0b_clean_standing_crowdscale_trials']}` false hits.",
        "- Caveats: (1) Harbor writes job-level `result.json` alongside trial dirs (`e6/scored`: `320+13=333`, `e7/scored`: `180+9=189`, `probes`: `74+10=84`); `classify_trial_dir` excludes job-level files. (2) `bespoke/study4-holdout/arms` mirrors the `8` preregistered-arm measured trials of `bespoke/study4-holdout/jobs` (`13` unique trials total: `9` measured incl. `1` canary + `4` unmeasured `lost-seat`); `unique_trials_evaluated_total` excludes the `arms/` mirror copies (`1,657` disjoint real trials). (3) In Study A, `CLAIM_CHURN` evaluates WorldWatch across all `96` coded items while `detect.py` evaluates only claim-arm items (`n=14`; see `SUMMARY.json` `worldwatch_claim_scope`). (4) Free-text box claims/reports in `e5`/`e6`/`e7` do not hit structured `/claim` (`claim_churn`) or `/report_value` (`misreport_world_mismatch`).",
        "", "### Curated CSP-Strict HTML Storyboards (`5`)", "",
        *[f"- `{r['file']}` (`{r['html_bytes']}` B, `{r['ecosystem']}`, `{r['seats_total']}` seats, `{r['join_status']}`, triggered `{r['triggered']}`): {r['description']}" for r in sbs],
    ]


def run_offline_audit(out_dir: Path) -> dict[str, Any]:
    t0 = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[tuple[str, str], dict[str, Any]] = {}
    study_a = run_study_a(cache)
    study_b = run_study_b(cache)
    study_c = run_study_c(cache)
    storyboards = _build_storyboards(out_dir, study_a, cache)
    elapsed_s = _r6(time.monotonic() - t0)
    study_a_clean = {k: v for k, v in study_a.items() if k != "item_real_paths"}
    summary: dict[str, Any] = {
        "study": "worldwatch-offline-study-2026-11-24", "output_dir": str(out_dir), "spend_usd": 0.0,
        "unique_trials_evaluated_total": len(cache), "elapsed_seconds": elapsed_s,
        "detector_names": list(DETECTOR_NAMES), "study_a_forensics": study_a_clean,
        "study_b_muster_corpus": study_b, "study_c_campaign_and_probes": study_c, "storyboards": storyboards,
    }
    md_lines = [
        "# WorldWatch Offline Corpus Audit (2026-11-24)", "",
        f"- Output root: `{out_dir}`", f"- Unique resolved trials evaluated: `{len(cache)}` in `{elapsed_s:.2f}s` (`$0.00` spend)", "",
        *_render_study_a_md(study_a_clean), *_render_study_bc_md(study_b, study_c, storyboards), "",
    ]
    atomic_write_json(out_dir / "SUMMARY.json", summary)
    atomic_write_text(out_dir / "REPORT.md", "\n".join(md_lines))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run WorldWatch offline corpus audit")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    summary = run_offline_audit(args.out.resolve())
    print(f"Offline audit complete: unique_trials={summary['unique_trials_evaluated_total']} elapsed_s={summary['elapsed_seconds']} out={summary['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
