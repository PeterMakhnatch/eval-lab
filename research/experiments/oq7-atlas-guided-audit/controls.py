#!/usr/bin/env python3
"""OQ-7: label-free normalized rankings and dataset/run-stratified controls."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from evaluate import CAVEAT, load

SEED = 0
PERMUTATIONS = 10000
BOOTSTRAPS = 1000
MIN_BIN = 10
RAW = (
    "pass_tests_modified",
    "pass_suspicion_ge2",
    "shortcut_passes",
    "pass_answer_material",
    "pass_grader_touched",
    "pass_eval_awareness_grader",
    "pass_success_claim_no_evidence",
)
NORMALIZED_BASE = RAW[:5]
# Fixed, equal-weight rank average, specified without consulting labels.
COMBINED_SIGNALS = tuple(
    f"{denom}_{name}" for denom in ("share", "per_run") for name in NORMALIZED_BASE
)
SIGNALS = RAW + COMBINED_SIGNALS + ("COMBINED_NORMALIZED", "runs")
METRICS = ("AP", "AUROC", "yield@10", "yield@20")


def bins(rows):
    """Within dataset, equal-run groups, approximately quartiles; never split ties.

    Sequentially close a bin after reaching ceil(N/4) when at least 10 remain;
    any tail is merged. Datasets below 10 are a single bin.
    """
    result = []
    for dataset in sorted({r["dataset_id"] for r in rows}):
        ids = sorted(
            (i for i, r in enumerate(rows) if r["dataset_id"] == dataset),
            key=lambda i: (rows[i]["runs"], rows[i]["task_id"]),
        )
        target = max(MIN_BIN, (len(ids) + 3) // 4)
        groups, pending = [], []
        for j, idx in enumerate(ids):
            pending.append(idx)
            end_tie = j + 1 == len(ids) or rows[ids[j + 1]]["runs"] != rows[idx]["runs"]
            if end_tie and len(pending) >= target and len(ids) - j - 1 >= MIN_BIN:
                groups.append(pending)
                pending = []
        if pending:
            if len(pending) < MIN_BIN and groups:
                groups[-1].extend(pending)
            else:
                groups.append(pending)
        for group in groups:
            result.append((dataset, np.array(group, dtype=int)))
    return result


def scores(rows):
    n = len(rows)
    rng = np.random.default_rng(SEED)
    tie = rng.random(n)
    values = {}
    for name in RAW:
        values[name] = [r[name] if r["facet_coverage"] > 0 else None for r in rows]
    for name in NORMALIZED_BASE:
        for denom, field in (("share", "passes"), ("per_run", "runs")):
            values[f"{denom}_{name}"] = [
                None
                if r["facet_coverage"] == 0 or r[name] is None or not r[field]
                else r[name] / r[field]
                for r in rows
            ]
    values["runs"] = [r["runs"] for r in rows]
    result = {}
    for name, vals in values.items():
        arr = np.array([np.nan if v is None else v for v in vals], dtype=float)
        known = np.flatnonzero(np.isfinite(arr))
        unknown = np.flatnonzero(~np.isfinite(arr))
        order = known[np.lexsort((tie[known], -arr[known]))]
        pct = np.empty(n)
        pct[order] = (np.arange(len(order)) + 0.5) / max(len(order), 1)
        pct[unknown] = rng.random(len(unknown))
        result[name] = pct
    result["COMBINED_NORMALIZED"] = np.mean([result[x] for x in COMBINED_SIGNALS], axis=0)
    return result, tie


def metric_rows(y):
    """Metrics of each row of ranked binary labels, with exact random-null mean."""
    y = np.atleast_2d(y).astype(float)
    n = y.shape[1]
    p = y.sum(axis=1)
    cum = np.cumsum(y, axis=1)
    positions = np.arange(1, n + 1)
    ap = np.divide((cum / positions * y).sum(axis=1), p, out=np.full(len(y), np.nan), where=p > 0)
    denom = p * (n - p)
    auc = np.divide(
        (y * (n - positions)).sum(axis=1) - p * (p - 1) / 2,
        denom,
        out=np.full(len(y), np.nan),
        where=denom > 0,
    )
    h = np.sum(1 / positions)
    null_ap = np.where(p > 0, (h + (p - 1) / max(n - 1, 1) * (n - h)) / n, np.nan)
    null_auc = np.where(denom > 0, 0.5, np.nan)
    out = np.stack(
        (ap, auc, cum[:, min(10, n) - 1] / min(10, n), cum[:, min(20, n) - 1] / min(20, n)),
        axis=1,
    )
    null = np.stack((null_ap, null_auc, p / n, p / n), axis=1)
    return out, null


def aggregate(parts, weights):
    a = np.stack(parts, axis=0)
    w = np.asarray(weights)[:, None, None]
    valid = np.isfinite(a)
    numerator = np.where(valid, a, 0) * w
    denominator = (valid * w).sum(axis=0)
    return np.divide(
        numerator.sum(axis=0),
        denominator,
        out=np.full_like(numerator.sum(axis=0), np.nan),
        where=denominator > 0,
    )


def group_analysis(rows, strata, score, tie, permutations, bootstraps):
    rng = np.random.default_rng(SEED)
    observed, expected, perms, boot_effect = [], [], [], []
    weights = []
    for _, ids in strata:
        order = ids[np.lexsort((tie[ids], score[ids]))]
        y = np.array([rows[i]["keyed"] for i in order], dtype=int)
        n = len(y)
        weights.append(n)
        obs, null = metric_rows(y)
        observed.append(obs)
        expected.append(null)
        # Random keys generate independent uniform permutations within the stratum.
        pp = np.argsort(rng.random((permutations, n)), axis=1)
        pm, pn = metric_rows(y[pp])
        perms.append(pm - pn)
        # Bootstrap tasks inside each fixed stratum, preserving the original ranking.
        draws = np.sort(rng.integers(0, n, size=(bootstraps, n)), axis=1)
        bm, bn = metric_rows(y[draws])
        boot_effect.append(bm - bn)
    obs = aggregate(observed, weights)[0]
    null = aggregate(expected, weights)[0]
    perm = aggregate(perms, weights)
    boot = aggregate(boot_effect, weights)
    result = {}
    for j, name in enumerate(METRICS):
        if not np.isfinite(obs[j]):
            result[name] = None
            continue
        effect = obs[j] - null[j]
        pvals = perm[:, j][np.isfinite(perm[:, j])]
        b = boot[:, j][np.isfinite(boot[:, j])]
        result[name] = {
            "value": float(obs[j]),
            "null_mean": float(null[j]),
            "effect": float(effect),
            "p_one_sided": float((1 + np.count_nonzero(pvals >= effect - 1e-12)) / (1 + len(pvals))),
            "bootstrap_effect_ci95": [float(v) for v in np.quantile(b, [0.025, 0.975])]
            if len(b)
            else None,
            "bootstrap_valid": int(len(b)),
        }
    return result


def main():
    parser = argparse.ArgumentParser()
    for name in ("labels", "features", "context", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    rows = load(args.labels, args.features, args.context)
    ranking, tie = scores(rows)  # no labels used in constructing scores or bins
    strata = bins(rows)
    metadata = []
    for dataset, ids in strata:
        metadata.append(
            {
                "dataset": dataset,
                "n": len(ids),
                "keyed": sum(rows[i]["keyed"] for i in ids),
                "min_runs": min(rows[i]["runs"] for i in ids),
                "max_runs": max(rows[i]["runs"] for i in ids),
            }
        )
    groups = {
        "dataset_weighted_pooled": strata,
        "mimo": [(d, ids) for d, ids in strata if d == "mimo-defect-map-v1"],
    }
    group_results = {}
    output = {
        "caveat": CAVEAT,
        "seed": SEED,
        "permutations": PERMUTATIONS,
        "bootstraps": BOOTSTRAPS,
        "combined_signals": COMBINED_SIGNALS,
        "strata": metadata,
        "inputs_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (args.labels, args.features, args.context)
        },
        "groups": group_results,
    }
    for group, subset in groups.items():
        group_results[group] = {
            name: group_analysis(rows, subset, ranking[name], tie, PERMUTATIONS, BOOTSTRAPS)
            for name in SIGNALS
        }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "results.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print("Wrote", args.output / "results.json")


if __name__ == "__main__":
    main()
