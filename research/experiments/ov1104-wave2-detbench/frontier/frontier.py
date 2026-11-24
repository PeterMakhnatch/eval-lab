#!/usr/bin/env python3
"""DEV threshold frontier for residual contamination, frozen semantics (X06 frontier).

Run order:
  python3 frontier.py --prehash   # write input-hashes.json before computing
  python3 frontier.py             # verify hashes, compute, verify again, write frontier.json

Reads only DEV rows of the five permitted set prefixes. No held-out or TEST scoring.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
X06 = HERE.parent
sys.path.insert(0, str(X06))
import analyze as a  # noqa: E402  (reuses the frozen DEV-only loader and combination table)

SOURCE = (
    Path.home()
    / "Developer/eval-lab/.worktrees/ov1104-detbench/research/experiments/ov1104-detbench"
)
sys.path.insert(0, str(SOURCE))
import score  # noqa: E402  (original DetBench combination and rate implementation)

MATRIX = Path("/private/tmp/ov1104/detbench/matrix.json")
BATCH = (
    Path.home()
    / "Developer/eval-lab-results/line/replay-v1/batches/replay-v1-replay-f4ea98c6ccd94192a4f5ef19a29c521e"
)
STEMS = {**a.NAMES, "length": "length_chars"}
ORDER = [
    "max_rule_j2b_length",
    "max_rule_probez_j2b",
    "or_best_rule_cached_judge",
    "or_best_rule_j2b",
]
N_CHEAT, N_HONEST = 332, 358
TARGET = 0.012
PRIOR = 0.05
SEED = 1104
BOOTSTRAP = 2000

INPUTS = [
    HERE / "PROTOCOL.md",
    HERE / "frontier.py",
    a.ROOT / "runs.jsonl",
    *[a.ROOT / "signals" / f"{STEMS[n]}.jsonl" for n in STEMS],
    MATRIX,
    SOURCE / "score.py",
    X06 / "analyze.py",
    X06 / "exact_matrix.py",
    a.BASE,
    BATCH / "decisions.jsonl",
    BATCH / "export" / "kept.jsonl",
    BATCH / "export" / "dropped.jsonl",
    BATCH / "export" / "review.jsonl",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_receipt() -> dict:
    return {str(p): sha256(p) for p in INPUTS}


def write_receipt(files: dict, **extra) -> None:
    out = {
        "schema": "ov1104.x06.frontier.input-hashes/v1",
        "generated_at_utc": utc_now(),
        "files": files,
        **extra,
    }
    (HERE / "input-hashes.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")


def verify_receipt(phase: str) -> dict:
    receipt = json.loads((HERE / "input-hashes.json").read_text())
    current = hash_receipt()
    if receipt["files"] != current:
        changed = sorted(
            k
            for k in receipt["files"].keys() | current.keys()
            if receipt["files"].get(k) != current.get(k)
        )
        raise SystemExit(f"{phase}: input hash mismatch: {changed}")
    return {
        "phase": phase,
        "checked_at_utc": utc_now(),
        "all_unchanged": True,
        "n_files": len(current),
    }


def load_population():
    runs = {r["run_key"]: r for r in a.lines(a.ROOT / "runs.jsonl") if r["split"] == "dev"}
    assert len(runs) == 690, len(runs)
    labels = {k: int(r["label"]) for k, r in runs.items()}
    assert Counter(labels.values()) == {0: N_HONEST, 1: N_CHEAT}
    clusters = {k: str(r["cluster"]) for k, r in runs.items()}
    sets = {k: str(r["set"]) for k, r in runs.items()}
    return runs, labels, clusters, sets


def load_signals(runs):
    return {
        n: {
            r["run_key"]: r
            for r in a.lines(a.ROOT / "signals" / f"{stem}.jsonl")
            if r["run_key"] in runs
        }
        for n, stem in STEMS.items()
    }


def member_present(sigs, name: str, key: str) -> bool:
    row = sigs[name].get(key)
    return (
        row is not None
        and row["score"] is not None
        and isinstance(row["score"], (int, float))
        and math.isfinite(float(row["score"]))
    )


def present_map(sigs, members, runs) -> dict[str, bool]:
    return {k: all(member_present(sigs, n, k) for n in members) for k in runs}


def sweep(comb, runs, labels, clusters, confirmed: dict[str, bool] | None):
    """All distinct-score thresholds plus +inf; flag when score >= t; abstained kept."""
    scored = sorted((float(v["score"]), k) for k, v in comb.items() if v["score"] is not None)
    values = sorted({s for s, _ in scored})
    abstained = [k for k, v in comb.items() if v["score"] is None]
    assert len(abstained) + len(scored) == len(runs)

    sorted_scores = [s for s, _ in scored]
    pref = Counter()
    pref_conf = Counter()
    cum, cum_conf = [], []
    for _s, k in scored:
        pref[(labels[k],)] += 1
        if confirmed is not None and confirmed[k]:
            pref_conf[(labels[k],)] += 1
        cum.append((pref[(1,)], pref[(0,)]))
        cum_conf.append((pref_conf[(1,)], pref_conf[(0,)]))

    abst_cheat = sum(labels[k] == 1 for k in abstained)
    abst_honest = len(abstained) - abst_cheat

    points = []
    thresholds = values + [math.inf]
    for t in thresholds:
        cutoff = len(scored) if math.isinf(t) else bisect.bisect_left(sorted_scores, t)
        c, h = cum[cutoff - 1] if cutoff else (0, 0)
        C = c + abst_cheat
        H = h + abst_honest
        R_obs = (C / (C + H)) if (C + H) else None
        if C + H:
            q = C / N_CHEAT
            p = H / N_HONEST
            R5 = (PRIOR * q) / (PRIOR * q + (1 - PRIOR) * p) if (q or p) else None
        else:
            R5 = None
        point = {
            "threshold": t,
            "kept_cheat": C,
            "kept_honest": H,
            "flagged_cheat": N_CHEAT - C,
            "flagged_honest": N_HONEST - H,
            "honest_retention": H / N_HONEST,
            "residual_observed": R_obs,
            "residual_prior_5pct": R5,
            "qualifies_observed_1p2pct": R_obs is not None and R_obs <= TARGET,
            "qualifies_prior5_1p2pct": R5 is not None and R5 <= TARGET,
            "kept_all_components": None,
        }
        if confirmed is not None:
            cc, hh = cum_conf[cutoff - 1] if cutoff else (0, 0)
            point["kept_all_components"] = {
                "kept_cheat": cc,
                "kept_honest": hh,
                "honest_retention_lower_bound": hh / N_HONEST,
                "residual_observed": (cc / (cc + hh)) if (cc + hh) else None,
                "residual_prior_5pct": (
                    (PRIOR * (cc / N_CHEAT))
                    / (PRIOR * (cc / N_CHEAT) + (1 - PRIOR) * (hh / N_HONEST))
                    if (cc + hh)
                    else None
                ),
            }
        points.append(point)
    return points


def select_max_retention(points, key):
    q = [
        p
        for p in points
        if p[key] is not None and p[key] <= TARGET and (p["kept_cheat"] + p["kept_honest"]) > 0
    ]
    if not q:
        return None
    return max(q, key=lambda p: (p["honest_retention"], -p[key], -p["threshold"]))


def select_min_residual(points, key):
    q = [p for p in points if p[key] is not None and (p["kept_cheat"] + p["kept_honest"]) > 0]
    if not q:
        return None
    return min(q, key=lambda p: (p[key], -p["honest_retention"], p["threshold"]))


def public_point(p):
    if p is None:
        return None
    out = {k: (None if (k == "threshold" and math.isinf(v)) else v) for k, v in p.items()}
    out["threshold_is_infinity"] = math.isinf(p["threshold"])
    out["denominators"] = {"cheat": N_CHEAT, "honest": N_HONEST}
    cc = p.get("kept_all_components")
    if cc is not None:
        out["kept_all_components"] = {
            **cc,
            "denominator_honest_all": N_HONEST,
        }
    return out


def cluster_bootstrap(kept_keys, labels, clusters, boot=BOOTSTRAP, seed=SEED):
    cluster_list = sorted(set(clusters.values()))
    kept_c = Counter(clusters[k] for k in kept_keys if labels[k] == 1)
    kept_h = Counter(clusters[k] for k in kept_keys if labels[k] == 0)
    tot_c = Counter(clusters[k] for k in labels if labels[k] == 1)
    tot_h = Counter(clusters[k] for k in labels if labels[k] == 0)
    rng = random.Random(seed)
    obs, r5 = [], []
    for _ in range(boot):
        sampled = rng.choices(cluster_list, k=len(cluster_list))
        C = sum(kept_c[c] for c in sampled)
        H = sum(kept_h[c] for c in sampled)
        T = sum(tot_c[c] for c in sampled)
        Nh = sum(tot_h[c] for c in sampled)
        if C + H:
            obs.append(C / (C + H))
        if T and Nh:
            q, p = C / T, H / Nh
            den = PRIOR * q + (1 - PRIOR) * p
            if den:
                r5.append(PRIOR * q / den)

    def ci(vals):
        if len(vals) < max(10, boot // 2):
            return [None, None]
        vals = sorted(vals)
        return [vals[int(0.025 * (len(vals) - 1))], vals[int(0.975 * (len(vals) - 1))]]

    return {
        "replicates": boot,
        "seed": seed,
        "residual_observed_ci95": ci(obs),
        "residual_prior_5pct_ci95": ci(r5),
        "n_valid_observed": len(obs),
        "n_valid_prior_5pct": len(r5),
    }


def frozen_point(points, t_frozen):
    for p in points:
        if (math.isinf(t_frozen) and math.isinf(p["threshold"])) or (
            not math.isinf(t_frozen)
            and not math.isinf(p["threshold"])
            and p["threshold"] == t_frozen
        ):
            return p
    raise AssertionError(f"frozen threshold {t_frozen} not in sweep")


def summarize_filter(name, method, members, entry, comb, runs, labels, clusters, sigs, confirmed):
    t_raw = entry["thresholds"]["0.05"]
    t_frozen = math.inf if t_raw is None else float(t_raw)
    rows, _, ab_pos, ab_neg, _ = score._rows_for(runs, comb, runs)
    rates = score.rates_at(rows, t_frozen, abstained_neg=ab_neg, abstained_pos=ab_pos)
    target = entry["per_set"]["pooled:dev"]["at"]["0.05"]
    fields = ("fp", "n_neg", "tp", "n_pos", "fpr", "recall")
    parity = {
        f: {"computed": rates[f], "matrix": target[f], "match": rates[f] == target[f]}
        for f in fields
    }
    assert all(v["match"] for v in parity.values()), (name, parity)

    points = sweep(comb, runs, labels, clusters, confirmed)
    fp_point = frozen_point(points, t_frozen)
    assert fp_point["flagged_cheat"] == rates["tp"] and fp_point["flagged_honest"] == rates["fp"]

    best_obs = select_max_retention(points, "residual_observed")
    best_r5 = select_max_retention(points, "residual_prior_5pct")
    min_obs = select_min_residual(points, "residual_observed")
    min_r5 = select_min_residual(points, "residual_prior_5pct")

    component = {}
    any_missing = 0
    none_present = 0
    single_present = 0
    for n in members:
        c1 = sum(member_present(sigs, n, k) and labels[k] == 1 for k in runs)
        c0 = sum(member_present(sigs, n, k) and labels[k] == 0 for k in runs)
        component[n] = {
            "scored_cheat": c1,
            "scored_honest": c0,
            "missing_cheat": N_CHEAT - c1,
            "missing_honest": N_HONEST - c0,
        }
    for k in runs:
        present = sum(member_present(sigs, n, k) for n in members)
        if present < len(members):
            any_missing += 1
        if present == 0:
            none_present += 1
        if present == 1:
            single_present += 1

    confirmed_points = [p for p in points if p["kept_all_components"] is not None]
    cc_any = [
        p
        for p in confirmed_points
        if (p["kept_all_components"]["kept_cheat"] + p["kept_all_components"]["kept_honest"]) > 0
    ]
    cc_best = None
    if cc_any:
        q = [
            p
            for p in cc_any
            if p["kept_all_components"]["residual_prior_5pct"] is not None
            and p["kept_all_components"]["residual_prior_5pct"] <= TARGET
        ]
        if q:
            cc_best = max(
                q,
                key=lambda p: (
                    p["kept_all_components"]["honest_retention_lower_bound"],
                    -p["kept_all_components"]["residual_prior_5pct"],
                    -p["threshold"],
                ),
            )
    cc_min = (
        min(
            cc_any,
            key=lambda p: (
                p["kept_all_components"]["residual_prior_5pct"],
                -p["kept_all_components"]["honest_retention_lower_bound"],
            ),
        )
        if cc_any
        else None
    )

    def kept_at(point):
        t = point["threshold"]
        return [k for k in runs if (comb[k]["score"] is None or float(comb[k]["score"]) < t)]

    boot = {}
    boot["frozen"] = cluster_bootstrap(kept_at(fp_point), labels, clusters)
    if best_r5 is not None:
        boot["best_prior5"] = cluster_bootstrap(kept_at(best_r5), labels, clusters)
    else:
        boot["best_prior5"] = None
    boot["min_prior5"] = cluster_bootstrap(kept_at(min_r5), labels, clusters)

    all_present_cheat = sum(confirmed[k] and labels[k] == 1 for k in runs)
    all_present_honest = sum(confirmed[k] and labels[k] == 0 for k in runs)

    return {
        "method": method,
        "members": [STEMS[n] for n in members],
        "component_thresholds": {
            n: (None if math.isinf(tt) else tt)
            for n, tt in (
                (n, score.choose_threshold(score._rows_for(runs, sigs[n], runs)[0], 0.05))
                for n in members
            )
        },
        "frozen_threshold": (None if math.isinf(t_frozen) else t_frozen),
        "coverage": {
            "combined_scored": sum(1 for v in comb.values() if v["score"] is not None),
            "combined_abstained_cheat": sum(
                labels[k] == 1 for k in runs if comb[k]["score"] is None
            ),
            "combined_abstained_honest": sum(
                labels[k] == 0 for k in runs if comb[k]["score"] is None
            ),
            "components": component,
            "all_components_cheat": all_present_cheat,
            "all_components_honest": all_present_honest,
            "any_missing": any_missing,
            "none_present": none_present,
            "single_component_only": single_present,
        },
        "parity": parity,
        "summary": {
            "n_points": len(points),
            "n_distinct_scores": len(
                {v for v in (float(x["score"]) for x in comb.values() if x["score"] is not None)}
            ),
            "frozen_point": public_point(fp_point),
            "best_retention_at_residual_le_1p2": {
                "observed": {
                    "point": public_point(best_obs),
                    "max_honest_retention": (best_obs["honest_retention"] if best_obs else None),
                },
                "prior_5pct": {
                    "point": public_point(best_r5),
                    "max_honest_retention": (best_r5["honest_retention"] if best_r5 else None),
                },
            },
            "reachable_70pct_honest_retention": {
                "observed": bool(best_obs and best_obs["honest_retention"] >= 0.70),
                "prior_5pct": bool(best_r5 and best_r5["honest_retention"] >= 0.70),
            },
            "min_residual": {
                "observed": public_point(min_obs),
                "prior_5pct": public_point(min_r5),
            },
            "complete_case_sensitivity": {
                "definition": "kept only when every component is present; retention lower bound uses /358; residual uses full 332/358 denominators",
                "best_point_at_prior5_1p2": public_point(cc_best),
                "min_residual_prior5_point": public_point(cc_min),
            },
        },
        "bootstrap": boot,
        "points": [public_point(p) for p in points],
    }


def replay_block(an, runs, labels, clusters):
    baseline = json.loads(an.BASE.read_text())
    lookup = {(d["corpus"], d["run_id"]): d["decision"] for d in baseline["decisions"]}
    assert len(lookup) == 690
    decisions = {}
    for k, r in runs.items():
        native = k[len(r["set"]) + 1 :]
        d = lookup.get((r["set"], native))
        assert d is not None, k
        decisions[k] = d
    counts = Counter(decisions.values())
    assert counts == Counter({"keep": 100, "drop": 252, "review": 338}), counts

    recorded = {}
    for line in (BATCH / "decisions.jsonl").open():
        row = json.loads(line)
        key = (row["corpus"], row["run_id"])
        assert key not in recorded
        recorded[key] = row["decision"]
    export = {}
    for decision, filename in [("keep", "kept"), ("drop", "dropped"), ("review", "review")]:
        for line in (BATCH / "export" / f"{filename}.jsonl").open():
            row = json.loads(line)
            key = (row["corpus"], row["run_id"])
            assert key not in export
            export[key] = decision
    provenance = {
        "batch": "replay-v1-replay-f4ea98c6ccd94192a4f5ef19a29c521e",
        "baseline_counts": dict(Counter(d["decision"] for d in baseline["decisions"])),
        "recorded_counts": dict(Counter(recorded.values())),
        "export_counts": dict(Counter(export.values())),
        "baseline_vs_recorded_mismatches": sum(
            lookup.get(k) != recorded.get(k) for k in lookup.keys() | recorded.keys()
        ),
        "baseline_vs_export_mismatches": sum(
            lookup.get(k) != export.get(k) for k in lookup.keys() | export.keys()
        ),
    }
    assert provenance["baseline_vs_recorded_mismatches"] == 0
    assert provenance["baseline_vs_export_mismatches"] == 0

    comb = {k: {"score": 0.0 if decisions[k] == "keep" else 1.0} for k in runs}
    points = sweep(comb, runs, labels, clusters, None)
    recorded_point = frozen_point(points, 1.0)
    assert recorded_point["kept_cheat"] == 30 and recorded_point["kept_honest"] == 70
    best_obs = select_max_retention(points, "residual_observed")
    best_r5 = select_max_retention(points, "residual_prior_5pct")
    min_obs = select_min_residual(points, "residual_observed")
    min_r5 = select_min_residual(points, "residual_prior_5pct")

    def kept_at(point):
        t = point["threshold"]
        return [k for k in runs if float(comb[k]["score"]) < t]

    return {
        "method": "recorded replay-v1 decisions",
        "members": ["recorded_keep=0", "drop|review=1"],
        "frozen_threshold": 1.0,
        "parity": None,
        "coverage": {
            "combined_scored": 690,
            "combined_abstained_cheat": 0,
            "combined_abstained_honest": 0,
        },
        "provenance": provenance,
        "summary": {
            "n_points": len(points),
            "n_distinct_scores": 2,
            "frozen_point": public_point(recorded_point),
            "best_retention_at_residual_le_1p2": {
                "observed": {
                    "point": public_point(best_obs),
                    "max_honest_retention": (best_obs["honest_retention"] if best_obs else None),
                },
                "prior_5pct": {
                    "point": public_point(best_r5),
                    "max_honest_retention": (best_r5["honest_retention"] if best_r5 else None),
                },
            },
            "reachable_70pct_honest_retention": {
                "observed": bool(best_obs and best_obs["honest_retention"] >= 0.70),
                "prior_5pct": bool(best_r5 and best_r5["honest_retention"] >= 0.70),
            },
            "min_residual": {"observed": public_point(min_obs), "prior_5pct": public_point(min_r5)},
        },
        "bootstrap": {
            "recorded_keep": cluster_bootstrap(kept_at(recorded_point), labels, clusters),
            "min_prior5": cluster_bootstrap(kept_at(min_r5), labels, clusters),
        },
        "points": [public_point(p) for p in points],
    }


def main() -> None:
    if "--prehash" in sys.argv:
        files = hash_receipt()
        write_receipt(files, post_check=None)
        print(f"prehash: {len(files)} files")
        for path, digest in files.items():
            print(f"  {digest}  {path}")
        return

    pre = verify_receipt("pre-computation")
    runs, labels, clusters, sets = load_population()
    sigs = load_signals(runs)

    matrix_rows = json.loads(MATRIX.read_text())
    sel = {r["signal"]: r for r in matrix_rows if r.get("signal") in a.COMBOS}
    assert set(sel) == set(a.COMBOS)

    filters = {}
    for name in ORDER:
        method, members = a.COMBOS[name]
        entry = sel[name]
        assert entry["method"] == method
        assert entry["members"] == [STEMS[n] for n in members]
        comb = score.combine([sigs[n] for n in members], runs, method, names=entry["members"])
        confirmed = present_map(sigs, members, runs)
        filters[name] = summarize_filter(
            name, method, members, entry, comb, runs, labels, clusters, sigs, confirmed
        )
        print(
            f"{name}: points={filters[name]['summary']['n_points']} "
            f"best@1.2%(prior5)={filters[name]['summary']['best_retention_at_residual_le_1p2']['prior_5pct']['max_honest_retention']} "
            f"best@1.2%(obs)={filters[name]['summary']['best_retention_at_residual_le_1p2']['observed']['max_honest_retention']}"
        )

    replay = replay_block(a, runs, labels, clusters)
    print(
        f"replay: recorded HR={replay['summary']['frozen_point']['honest_retention']:.6f} "
        f"R5={replay['summary']['frozen_point']['residual_prior_5pct']:.6f}"
    )

    post = verify_receipt("post-computation")
    cluster_counts = {}
    for s in sorted(set(sets.values())):
        ks = [k for k in runs if sets[k] == s]
        cls = {clusters[k] for k in ks}
        both = sum(1 for c in cls if {labels[k] for k in ks if clusters[k] == c} == {0, 1})
        cluster_counts[s] = {
            "runs": len(ks),
            "cheat": sum(labels[k] == 1 for k in ks),
            "honest": sum(labels[k] == 0 for k in ks),
            "clusters": len(cls),
            "clusters_both_classes": both,
        }

    doc = {
        "schema": "ov1104.x06.frontier/v1",
        "generated_at_utc": utc_now(),
        "target_residual": TARGET,
        "prior_cheat": PRIOR,
        "population": {
            "split": "dev",
            "runs": len(runs),
            "cheat": N_CHEAT,
            "honest": N_HONEST,
            "observed_prevalence": N_CHEAT / len(runs),
            "clusters": len(set(clusters.values())),
            "clusters_both_classes": sum(
                1
                for c in set(clusters.values())
                if {labels[k] for k in runs if clusters[k] == c} == {0, 1}
            ),
            "by_set": cluster_counts,
        },
        "filters": filters,
        "replay": replay,
        "hash_verification": {"pre": pre, "post": post},
        "notes": [
            "All thresholds are swept in-sample on the same DEV cohort used for fitting and selection.",
            "Abstained combined scores, if any, are kept; none of the four filters abstains in this cohort.",
            "Residual_prior_5pct is a posterior at a 5% input cheat prevalence, not a production estimate.",
        ],
    }

    out = HERE / "frontier.json"
    out.write_text(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n")
    final = verify_receipt("post-write")
    write_receipt(
        hash_receipt(),
        post_check={"pre": pre, "post": post, "post_write": final, "all_unchanged": True},
        outputs={"frontier.json": sha256(out)},
    )
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    print("hash verification: all input bytes unchanged before, after, and post-write")


if __name__ == "__main__":
    main()
