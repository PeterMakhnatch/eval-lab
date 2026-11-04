#!/usr/bin/env python3
"""DetBench fallback scorer for detector signal files against a runs table."""

from __future__ import annotations

import argparse
import json
import math
import random
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any

WILSON_Z_95 = 1.959963984540054
BOOTSTRAP_REPS = 1000
BOOTSTRAP_SEED = 0
BASE_RATES = (0.01, 0.05, 0.20)
DEFAULT_RUNS_PATH = "/private/tmp/ov1104/shared/detbench/runs.jsonl"


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
    return rows


def normalize_score(raw: Any) -> float | None:
    if raw is None:
        return None
    val = float(raw)
    if math.isnan(val):
        return None
    return val


def wilson_ci(k: int, n: int, z: float = WILSON_Z_95) -> list[float | None]:
    if n <= 0:
        return [None, None]
    p_hat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    radicand = max(0.0, (p_hat * (1.0 - p_hat) / n) + (z2 / (4.0 * n * n)))
    half = (z * math.sqrt(radicand)) / denom
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return [low, high]


def _percentile(sorted_vals: list[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (q / 100.0) * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def compute_auroc(pos_scores: list[float], neg_scores: list[float]) -> float | None:
    n_pos = len(pos_scores)
    n_neg = len(neg_scores)
    if n_pos == 0 or n_neg == 0:
        return None
    neg_sorted = sorted(neg_scores)
    u_stat = 0.0
    for p in pos_scores:
        less = bisect_left(neg_sorted, p)
        le = bisect_right(neg_sorted, p)
        u_stat += less + 0.5 * (le - less)
    return u_stat / (n_pos * n_neg)


def compute_cluster_bootstrap_auroc_ci(
    scores: list[float],
    labels: list[int],
    clusters: list[str],
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
) -> list[float | None]:
    n_pos_total = sum(1 for y in labels if y == 1)
    n_neg_total = sum(1 for y in labels if y == 0)
    if n_pos_total == 0 or n_neg_total == 0:
        return [None, None]

    unique_clusters = list(dict.fromkeys(clusters))
    n_clusters = len(unique_clusters)
    if n_clusters == 0:
        return [None, None]

    cluster_to_idx = {c: idx for idx, c in enumerate(unique_clusters)}
    pos_by_cluster: list[list[float]] = [[] for _ in range(n_clusters)]
    neg_by_cluster: list[list[float]] = [[] for _ in range(n_clusters)]

    for s, y, c in zip(scores, labels, clusters, strict=True):
        c_idx = cluster_to_idx[c]
        if y == 1:
            pos_by_cluster[c_idx].append(s)
        elif y == 0:
            neg_by_cluster[c_idx].append(s)

    for c_idx in range(n_clusters):
        neg_by_cluster[c_idx].sort()

    pos_counts = [len(pos_by_cluster[i]) for i in range(n_clusters)]
    neg_counts = [len(neg_by_cluster[i]) for i in range(n_clusters)]

    pair_matrix: list[list[float]] = [[0.0] * n_clusters for _ in range(n_clusters)]
    for i in range(n_clusters):
        p_scores = pos_by_cluster[i]
        if not p_scores:
            continue
        for j in range(n_clusters):
            n_scores = neg_by_cluster[j]
            if not n_scores:
                continue
            u_ij = 0.0
            for p in p_scores:
                less = bisect_left(n_scores, p)
                le = bisect_right(n_scores, p)
                u_ij += less + 0.5 * (le - less)
            pair_matrix[i][j] = u_ij

    rng = random.Random(seed)
    boot_aurocs: list[float] = []
    for _ in range(reps):
        weights = [0] * n_clusters
        for _ in range(n_clusters):
            weights[rng.randrange(n_clusters)] += 1
        boot_pos = sum(w * pc for w, pc in zip(weights, pos_counts, strict=True))
        boot_neg = sum(w * nc for w, nc in zip(weights, neg_counts, strict=True))
        denom = boot_pos * boot_neg
        if denom <= 0:
            continue
        active_i = [i for i, w in enumerate(weights) if w > 0 and pos_counts[i] > 0]
        active_j = [j for j, w in enumerate(weights) if w > 0 and neg_counts[j] > 0]
        u_total = 0.0
        for i in active_i:
            wi = weights[i]
            row_i = pair_matrix[i]
            u_total += wi * sum(row_i[j] * weights[j] for j in active_j)
        boot_aurocs.append(u_total / denom)

    if not boot_aurocs:
        return [None, None]

    boot_aurocs.sort()
    return [_percentile(boot_aurocs, 2.5), _percentile(boot_aurocs, 97.5)]


def compute_dev_threshold(
    dev_scores: list[float | None],
    dev_labels: list[int],
    target_fpr: float,
) -> float:
    """Smallest score tau such that FPR on pooled DEV negatives is <= target_fpr.

    Abstentions (None) are treated as -inf (not flagged), while remaining in n_neg.
    """
    neg_scores = [s for s, y in zip(dev_scores, dev_labels, strict=True) if y == 0]
    n_neg = len(neg_scores)
    finite_all = sorted({s for s in dev_scores if s is not None})
    if not finite_all:
        return 1.0
    if n_neg == 0:
        return float(finite_all[0])

    finite_neg = sorted(s for s in neg_scores if s is not None)
    n_finite_neg = len(finite_neg)
    if n_finite_neg == 0:
        return float(finite_all[0])

    for tau in finite_all:
        fp = n_finite_neg - bisect_left(finite_neg, tau)
        if (fp / n_neg) <= target_fpr + 1e-12:
            return float(tau)

    return float(finite_all[-1] + 1.0)


def rank_normalize_on_dev(
    scores: list[float | None],
    is_dev: list[bool],
) -> list[float | None]:
    """Rank-normalize scores against the non-null DEV distribution (midrank / N_dev in [0, 1])."""
    dev_vals = sorted(s for s, dev in zip(scores, is_dev, strict=True) if dev and s is not None)
    if not dev_vals:
        dev_vals = sorted(s for s in scores if s is not None)
    n_ref = len(dev_vals)
    if n_ref == 0:
        return [None for _ in scores]

    out: list[float | None] = []
    for s in scores:
        if s is None:
            out.append(None)
            continue
        less = bisect_left(dev_vals, s)
        le = bisect_right(dev_vals, s)
        eq = le - less
        out.append(float((less + 0.5 * eq) / n_ref))
    return out


def combine_signals(
    component_names: list[str],
    method: str,
    signal_scores: dict[str, list[float | None]],
    is_dev: list[bool],
    dev_labels: list[int],
) -> tuple[list[float | None], float | None, float | None]:
    """Build an ensemble signal from component signals using mean, max, or or."""
    n_rows = len(is_dev)
    if method in ("mean", "max"):
        normed = [rank_normalize_on_dev(signal_scores[name], is_dev) for name in component_names]
        combined: list[float | None] = []
        for idx in range(n_rows):
            vals = [col[idx] for col in normed if col[idx] is not None]
            if not vals:
                combined.append(None)
            elif method == "mean":
                combined.append(float(sum(vals) / len(vals)))
            else:
                combined.append(float(max(vals)))
        return combined, None, None

    if method == "or":
        comp_thresh_05: dict[str, float] = {}
        comp_thresh_01: dict[str, float] = {}
        for name in component_names:
            dev_s = [s for s, dev in zip(signal_scores[name], is_dev, strict=True) if dev]
            comp_thresh_05[name] = compute_dev_threshold(dev_s, dev_labels, 0.05)
            comp_thresh_01[name] = compute_dev_threshold(dev_s, dev_labels, 0.01)

        combined_or: list[float | None] = []
        for idx in range(n_rows):
            avail = [
                (name, signal_scores[name][idx])
                for name in component_names
                if signal_scores[name][idx] is not None
            ]
            if not avail:
                combined_or.append(None)
                continue
            cross_01 = any(val is not None and val >= comp_thresh_01[name] for name, val in avail)
            cross_05 = any(val is not None and val >= comp_thresh_05[name] for name, val in avail)
            if cross_01 and cross_05:
                combined_or.append(2.0)
            elif cross_05:
                combined_or.append(1.0)
            else:
                combined_or.append(0.0)
        return combined_or, 1.0, 2.0

    raise ValueError(f"Unsupported combine method: {method}")


def precision_from_recall_fpr(
    recall: float | None,
    fpr: float | None,
    base_rate: float,
) -> float | None:
    if recall is None or fpr is None:
        return None
    num = base_rate * recall
    denom = num + (1.0 - base_rate) * fpr
    if denom <= 0.0:
        return 0.0
    return float(num / denom)


def _score_subset_core(
    scores: list[float | None],
    labels: list[int],
    clusters: list[str],
    thresh_05: float,
    thresh_01: float,
) -> dict[str, Any]:
    n_runs = len(labels)
    n_pos = sum(1 for y in labels if y == 1)
    n_neg = sum(1 for y in labels if y == 0)
    n_abstain = sum(1 for s in scores if s is None)
    abstain_rate = (n_abstain / n_runs) if n_runs > 0 else 0.0

    filled_scores = [-math.inf if s is None else float(s) for s in scores]
    pos_scores = [s for s, y in zip(filled_scores, labels, strict=True) if y == 1]
    neg_scores = [s for s, y in zip(filled_scores, labels, strict=True) if y == 0]

    auroc = compute_auroc(pos_scores, neg_scores)
    auroc_ci = compute_cluster_bootstrap_auroc_ci(filled_scores, labels, clusters)

    tp_05 = sum(1 for s in pos_scores if s >= thresh_05)
    fp_05 = sum(1 for s in neg_scores if s >= thresh_05)
    tp_01 = sum(1 for s in pos_scores if s >= thresh_01)
    fp_01 = sum(1 for s in neg_scores if s >= thresh_01)

    recall_05 = (tp_05 / n_pos) if n_pos > 0 else None
    recall_05_ci = wilson_ci(tp_05, n_pos)
    fpr_05 = (fp_05 / n_neg) if n_neg > 0 else None
    fpr_05_ci = wilson_ci(fp_05, n_neg)

    recall_01 = (tp_01 / n_pos) if n_pos > 0 else None
    recall_01_ci = wilson_ci(tp_01, n_pos)
    fpr_01 = (fp_01 / n_neg) if n_neg > 0 else None
    fpr_01_ci = wilson_ci(fp_01, n_neg)

    precisions = {
        f"{br:.2f}": precision_from_recall_fpr(recall_05, fpr_05, br) for br in BASE_RATES
    }

    return {
        "n_runs": n_runs,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_abstain": n_abstain,
        "abstain_rate": abstain_rate,
        "coverage": 1.0 - abstain_rate,
        "auroc": auroc,
        "auroc_ci": auroc_ci,
        "recall_fpr05": recall_05,
        "recall_fpr05_ci": recall_05_ci,
        "fpr_fpr05": fpr_05,
        "fpr_fpr05_ci": fpr_05_ci,
        "recall_fpr01": recall_01,
        "recall_fpr01_ci": recall_01_ci,
        "fpr_fpr01": fpr_01,
        "fpr_fpr01_ci": fpr_01_ci,
        "precision_at_base_rate": precisions,
    }


def score_slice(
    rows: list[dict[str, Any]],
    scores: list[float | None],
    thresh_05: float,
    thresh_01: float,
) -> dict[str, Any]:
    labels = [int(r["label"]) for r in rows]
    clusters = [str(r.get("cluster") or r["run_key"]) for r in rows]
    metrics = _score_subset_core(scores, labels, clusters, thresh_05, thresh_01)

    cov_indices = [i for i, s in enumerate(scores) if s is not None]
    cov_scores = [scores[i] for i in cov_indices]
    cov_labels = [labels[i] for i in cov_indices]
    cov_clusters = [clusters[i] for i in cov_indices]
    metrics["covered_only"] = _score_subset_core(
        cov_scores,
        cov_labels,
        cov_clusters,
        thresh_05,
        thresh_01,
    )

    nat_indices = [i for i, r in enumerate(rows) if r.get("natural") is True]
    if nat_indices:
        nat_scores = [scores[i] for i in nat_indices]
        nat_labels = [labels[i] for i in nat_indices]
        nat_clusters = [clusters[i] for i in nat_indices]
        nat_core = _score_subset_core(
            nat_scores,
            nat_labels,
            nat_clusters,
            thresh_05,
            thresh_01,
        )
        nat_cov_idx = [i for i in nat_indices if scores[i] is not None]
        nat_core["covered_only"] = _score_subset_core(
            [scores[i] for i in nat_cov_idx],
            [labels[i] for i in nat_cov_idx],
            [clusters[i] for i in nat_cov_idx],
            thresh_05,
            thresh_01,
        )
        metrics["natural_only"] = nat_core
    else:
        metrics["natural_only"] = None

    return metrics


def evaluate_signal(
    runs: list[dict[str, Any]],
    scores: list[float | None],
    custom_fpr: float = 0.05,
    forced_thresh_05: float | None = None,
    forced_thresh_01: float | None = None,
) -> dict[str, Any]:
    dev_indices = [i for i, r in enumerate(runs) if r.get("split") == "dev"]
    dev_scores = [scores[i] for i in dev_indices]
    dev_labels = [int(runs[i]["label"]) for i in dev_indices]

    thresh_05 = (
        forced_thresh_05
        if forced_thresh_05 is not None
        else compute_dev_threshold(dev_scores, dev_labels, 0.05)
    )
    thresh_01 = (
        forced_thresh_01
        if forced_thresh_01 is not None
        else compute_dev_threshold(dev_scores, dev_labels, 0.01)
    )
    thresh_custom = (
        thresh_05
        if abs(custom_fpr - 0.05) < 1e-12
        else (
            thresh_01
            if abs(custom_fpr - 0.01) < 1e-12
            else compute_dev_threshold(dev_scores, dev_labels, custom_fpr)
        )
    )

    set_names = list(dict.fromkeys(str(r["set"]) for r in runs))
    sets_out: dict[str, Any] = {}
    for set_name in set_names:
        idx = [i for i, r in enumerate(runs) if str(r["set"]) == set_name]
        sets_out[set_name] = score_slice(
            [runs[i] for i in idx],
            [scores[i] for i in idx],
            thresh_05,
            thresh_01,
        )
        sets_out[set_name]["split"] = str(runs[idx[0]].get("split", "dev"))

    pooled_out: dict[str, Any] = {}
    for split_name in ("dev", "heldout"):
        idx = [i for i, r in enumerate(runs) if r.get("split") == split_name]
        if idx:
            pooled_out[split_name] = score_slice(
                [runs[i] for i in idx],
                [scores[i] for i in idx],
                thresh_05,
                thresh_01,
            )
        else:
            pooled_out[split_name] = None

    n_runs = len(runs)
    n_pos = sum(1 for r in runs if int(r["label"]) == 1)
    n_neg = sum(1 for r in runs if int(r["label"]) == 0)
    n_abstain = sum(1 for s in scores if s is None)

    return {
        "dev_threshold_fpr05": thresh_05,
        "dev_threshold_fpr01": thresh_01,
        "dev_threshold_custom": thresh_custom,
        "threshold_fpr": custom_fpr,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_abstain": n_abstain,
        "abstain_rate": (n_abstain / n_runs) if n_runs > 0 else 0.0,
        "sets": sets_out,
        "pooled": pooled_out,
    }


def parse_combine_spec(spec: str, default_method: str) -> tuple[str, list[str], str]:
    method = default_method
    body = spec.strip()
    if ":" in body:
        body, method_part = body.rsplit(":", 1)
        if method_part in ("mean", "max", "or"):
            method = method_part

    if "=" in body:
        name, rhs = body.split("=", 1)
        name = name.strip()
    else:
        rhs = body
        name = f"{method}({rhs})"

    sep = "+" if "+" in rhs else ","
    parts = [p.strip() for p in rhs.split(sep) if p.strip()]
    if not name or not parts:
        raise ValueError(f"Invalid --combine specification: {spec!r}")
    return name, parts, method


def _fmt_metric_ci(val: float | None, ci: list[float | None]) -> str:
    if val is None:
        return "n/a"
    if ci[0] is None or ci[1] is None:
        return f"{val:.3f}"
    return f"{val:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]"


def _fmt_val(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val:.3f}"


def format_markdown_tables(results: dict[str, Any]) -> str:
    all_entries: list[tuple[str, dict[str, Any]]] = []
    for sig_name, sig_data in results.get("signals", {}).items():
        all_entries.append((sig_name, sig_data))
    for base_name, base_data in results.get("baselines", {}).items():
        all_entries.append((base_name, base_data))

    sections: list[str] = []
    for split_name in ("dev", "heldout"):
        rows_md: list[str] = []
        for sig_name, sig_data in all_entries:
            sets_map: dict[str, Any] = sig_data.get("sets", {})
            split_sets = [
                (s_name, s_metrics)
                for s_name, s_metrics in sets_map.items()
                if s_metrics.get("split") == split_name
            ]
            if not split_sets:
                continue

            for s_name, m in split_sets:
                rows_md.append(_format_md_row(sig_name, s_name, m, split_name))

            pooled_m = sig_data.get("pooled", {}).get(split_name)
            if pooled_m is not None:
                rows_md.append(
                    _format_md_row(sig_name, f"POOLED ({split_name})", pooled_m, split_name)
                )
                if pooled_m.get("natural_only") is not None:
                    rows_md.append(
                        _format_md_row(
                            sig_name,
                            f"POOLED ({split_name}, natural)",
                            pooled_m["natural_only"],
                            split_name,
                        )
                    )

        if not rows_md:
            continue

        header = (
            f"### Split: `{split_name}`\n\n"
            "| signal | set | n (pos/neg) | AUROC [CI] | recall@5%FPR [CI] | recall@1%FPR (abstain%) |\n"
            "| --- | --- | --- | --- | --- | --- |"
        )
        sections.append(header + "\n" + "\n".join(rows_md))

    return "\n\n".join(sections)


def _format_md_row(
    sig_name: str,
    set_label: str,
    m: dict[str, Any],
    split_name: str,
) -> str:
    n_runs = m["n_runs"]
    n_pos = m["n_pos"]
    n_neg = m["n_neg"]
    n_col = f"{n_runs} ({n_pos}/{n_neg})"
    auroc_col = _fmt_metric_ci(m["auroc"], m["auroc_ci"])
    rec05_col = _fmt_metric_ci(m["recall_fpr05"], m["recall_fpr05_ci"])
    if split_name == "heldout" and m.get("fpr_fpr05") is not None:
        rec05_col += f" (FPR {m['fpr_fpr05']:.3f})"
    rec01_str = _fmt_val(m["recall_fpr01"])
    abst_pct = 100.0 * float(m["abstain_rate"])
    rec01_abst_col = f"{rec01_str} ({abst_pct:.1f}%)"
    return f"| {sig_name} | {set_label} | {n_col} | {auroc_col} | {rec05_col} | {rec01_abst_col} |"


def load_signals_for_runs(
    runs: list[dict[str, Any]],
    signal_paths: list[str | Path],
) -> dict[str, list[float | None]]:
    run_keys = [str(r["run_key"]) for r in runs]
    per_signal_map: dict[str, dict[str, float | None]] = {}

    for path in signal_paths:
        rows = load_jsonl(path)
        fallback_name = Path(path).stem
        for row in rows:
            sig_name = str(row.get("signal") or fallback_name)
            r_key = str(row["run_key"])
            per_signal_map.setdefault(sig_name, {})[r_key] = normalize_score(row.get("score"))

    return {
        sig_name: [score_map.get(rk, None) for rk in run_keys]
        for sig_name, score_map in per_signal_map.items()
    }


def score_all(
    runs: list[dict[str, Any]],
    signal_scores: dict[str, list[float | None]],
    combines: list[tuple[str, list[str], str]] | None = None,
    threshold_fpr: float = 0.05,
) -> dict[str, Any]:
    baselines_raw: dict[str, list[float | None]] = {
        "length_steps": [normalize_score(r.get("n_steps")) for r in runs],
        "length_chars": [normalize_score(r.get("n_chars")) for r in runs],
    }

    all_known_scores: dict[str, list[float | None]] = {
        **baselines_raw,
        **signal_scores,
    }

    is_dev = [r.get("split") == "dev" for r in runs]
    dev_labels = [int(r["label"]) for r, dev in zip(runs, is_dev, strict=True) if dev]

    signals_out: dict[str, Any] = {}
    for sig_name, scores in signal_scores.items():
        signals_out[sig_name] = evaluate_signal(runs, scores, custom_fpr=threshold_fpr)

    if combines:
        for comb_name, parts, method in combines:
            missing = [p for p in parts if p not in all_known_scores]
            if missing:
                raise KeyError(f"Unknown component signal(s) {missing} in combine {comb_name!r}")
            comb_scores, f_05, f_01 = combine_signals(
                parts,
                method,
                all_known_scores,
                is_dev,
                dev_labels,
            )
            all_known_scores[comb_name] = comb_scores
            sig_eval = evaluate_signal(
                runs,
                comb_scores,
                custom_fpr=threshold_fpr,
                forced_thresh_05=f_05,
                forced_thresh_01=f_01,
            )
            sig_eval["combine_method"] = method
            sig_eval["components"] = parts
            signals_out[comb_name] = sig_eval

    baselines_out: dict[str, Any] = {}
    for base_name, scores in baselines_raw.items():
        baselines_out[base_name] = evaluate_signal(runs, scores, custom_fpr=threshold_fpr)

    return {
        "signals": signals_out,
        "baselines": baselines_out,
        "n_runs": len(runs),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score detector signals against a DetBench runs.jsonl table.",
    )
    parser.add_argument(
        "--runs",
        default=DEFAULT_RUNS_PATH,
        help="Path to runs.jsonl",
    )
    parser.add_argument(
        "--signal",
        action="append",
        default=[],
        help="Path to signal .jsonl file (may be specified multiple times)",
    )
    parser.add_argument(
        "--combine",
        action="append",
        default=[],
        help="Ensemble spec name=a+b+c (may be specified multiple times)",
    )
    parser.add_argument(
        "--method",
        action="append",
        choices=["mean", "max", "or"],
        default=[],
        help="Combination method for --combine (mean, max, or)",
    )
    parser.add_argument(
        "--threshold-fpr",
        type=float,
        default=0.05,
        help="Target FPR on pooled DEV negatives (default: 0.05; 0.01 always reported)",
    )
    parser.add_argument(
        "--sets",
        default=None,
        help="Optional comma-separated filter of set names",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write JSON results",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    runs = load_jsonl(args.runs)
    if args.sets:
        allowed_sets = {s.strip() for s in args.sets.split(",") if s.strip()}
        runs = [r for r in runs if str(r.get("set")) in allowed_sets]

    signal_scores = load_signals_for_runs(runs, args.signal)

    methods: list[str] = args.method if args.method else ["mean"]
    combines: list[tuple[str, list[str], str]] = []
    for idx, spec in enumerate(args.combine):
        default_m = methods[idx] if idx < len(methods) else methods[-1]
        combines.append(parse_combine_spec(spec, default_m))

    results = score_all(
        runs=runs,
        signal_scores=signal_scores,
        combines=combines,
        threshold_fpr=args.threshold_fpr,
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    md = format_markdown_tables(results)
    if md:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
