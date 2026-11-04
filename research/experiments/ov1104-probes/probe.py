#!/usr/bin/env python3
"""
probe.py — Probe training, freezing, scoring, and reporting tool for lane `probes`.

Subcommands:
  loso   Leave-one-source-out evaluation over DEV sets for the pre-registered grid.
  fit    Fit a selected grid config on all DEV sets and save frozen probe.npz.
  score  Score runs across specified sets using a frozen probe.npz and emit DetBench signals.jsonl.
  report Evaluate signals (AUROC with cluster bootstrap CI, recall/FPR at DEV 1%/5% FPR thresholds
         with Wilson CIs, precision at 1%/5%/20% base rates, natural-only breakdown, coverage,
         length baselines, and OR / max-rank combinations).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import StandardScaler

SEED = 20261104
N_BOOTSTRAP = 1000
LAYER_MAP = {9: 0, 18: 1, 27: 2}

# Pre-registered grid: 5 feature specs x 3 regularization values = 15 configs.
# Do NOT add to or alter this grid (binding per PROMPT.md).
FEATURE_SPECS: list[dict[str, Any]] = [
    {
        "feature_id": "run_mean_L18",
        "description": "run_mean L18",
        "level": "run",
        "array_key": "run_mean",
        "layers": [18],
        "win_agg": None,
    },
    {
        "feature_id": "run_max_L18",
        "description": "run_max L18",
        "level": "run",
        "array_key": "run_max",
        "layers": [18],
        "win_agg": None,
    },
    {
        "feature_id": "run_mean_L9_L18_L27",
        "description": "run_mean L9+L18+L27 (concat)",
        "level": "run",
        "array_key": "run_mean",
        "layers": [9, 18, 27],
        "win_agg": None,
    },
    {
        "feature_id": "win_mean_L18_max",
        "description": "win-level L18 mean (score = max over windows)",
        "level": "window",
        "array_key": "win_mean",
        "layers": [18],
        "win_agg": "max",
    },
    {
        "feature_id": "win_mean_L18_mean",
        "description": "win-level L18 mean (score = mean over windows)",
        "level": "window",
        "array_key": "win_mean",
        "layers": [18],
        "win_agg": "mean",
    },
]

C_VALUES: list[float] = [0.01, 0.1, 1.0]


def build_default_grid() -> list[dict[str, Any]]:
    grid: list[dict[str, Any]] = []
    for fspec in FEATURE_SPECS:
        for c in C_VALUES:
            name = f"{fspec['feature_id']}__c{c}"
            cfg = {
                "name": name,
                "feature_id": fspec["feature_id"],
                "description": f"{fspec['description']}, C={c}",
                "level": fspec["level"],
                "array_key": fspec["array_key"],
                "layers": list(fspec["layers"]),
                "win_agg": fspec["win_agg"],
                "C": float(c),
                "class_weight": "balanced",
                "max_iter": 2000,
                "seed": SEED,
            }
            grid.append(cfg)
    return grid


DEFAULT_GRID = build_default_grid()
GRID_BY_NAME: dict[str, dict[str, Any]] = {cfg["name"]: cfg for cfg in DEFAULT_GRID}


def resolve_config(name: str) -> dict[str, Any]:
    if name in GRID_BY_NAME:
        return dict(GRID_BY_NAME[name])
    norm = (
        name.replace("⊕", "_")
        .replace("+", "_")
        .replace(" ", "_")
        .replace(":", "__c")
    )
    if norm in GRID_BY_NAME:
        return dict(GRID_BY_NAME[norm])
    raise ValueError(
        f"Unknown grid config '{name}'. Valid configs: {sorted(GRID_BY_NAME.keys())}"
    )


def load_runs(runs_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(runs_path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "run_key" not in row or "set" not in row or "split" not in row:
                raise ValueError(f"Malformed runs row at {runs_path}:{line_no}")
            rows.append(row)
    return rows


def load_signals_file(sig_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(sig_path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "run_key" not in row or "signal" not in row:
                raise ValueError(f"Malformed signal row at {sig_path}:{line_no}")
            rows.append(row)
    return rows


class LazySetVectors:
    """Lazy reader around <vectors>/<set>.npz using mmap_mode='r'."""

    def __init__(self, npz_path: Path):
        self.npz_path = npz_path
        self._npz = np.load(npz_path, mmap_mode="r", allow_pickle=False)
        self._cache: dict[str, np.ndarray] = {}
        run_keys_arr = self._get_array("run_keys")
        self.run_keys: list[str] = [str(k) for k in run_keys_arr.tolist()]
        self.key_to_idx: dict[str, int] = {k: i for i, k in enumerate(self.run_keys)}
        meta_raw = self._get_array("meta")
        if isinstance(meta_raw, np.ndarray):
            meta_str = str(meta_raw.item()) if meta_raw.ndim == 0 else str(meta_raw[0])
        else:
            meta_str = str(meta_raw)
        self.meta: dict[str, Any] = json.loads(meta_str)

    def _get_array(self, key: str) -> np.ndarray:
        if key not in self._cache:
            self._cache[key] = self._npz[key]
        return self._cache[key]

    def get_n_tokens(self, indices: Sequence[int]) -> np.ndarray:
        arr = self._get_array("n_tokens")
        return np.asarray(arr[indices], dtype=np.int32)

    def get_run_features(
        self, array_key: str, layers: Sequence[int], indices: Sequence[int]
    ) -> np.ndarray:
        arr = self._get_array(array_key)  # [N, 3, 4096] float16
        layer_idxs = [LAYER_MAP[lyr] for lyr in layers]
        sub = np.asarray(arr[indices][:, layer_idxs, :], dtype=np.float32)
        n = sub.shape[0]
        return sub.reshape(n, -1)

    def get_window_features_for_runs(
        self, layers: Sequence[int], indices: Sequence[int]
    ) -> tuple[list[np.ndarray], list[np.ndarray]]:
        """
        Return per-run list of valid window feature matrices [n_w, D] in float32
        and per-run list of win_steps [n_w, 2] in int32.
        """
        win_arr = self._get_array("win_mean")  # [N, 12, 3, 4096]
        n_win_arr = self._get_array("n_windows")  # [N]
        win_steps_arr = self._get_array("win_steps")  # [N, 12, 2]
        layer_idxs = [LAYER_MAP[lyr] for lyr in layers]

        feats_list: list[np.ndarray] = []
        steps_list: list[np.ndarray] = []
        for idx in indices:
            n_w = int(n_win_arr[idx])
            if n_w <= 0:
                feats_list.append(np.zeros((0, len(layer_idxs) * win_arr.shape[-1]), dtype=np.float32))
                steps_list.append(np.zeros((0, 2), dtype=np.int32))
                continue
            w_sub = np.asarray(win_arr[idx, :n_w, layer_idxs, :], dtype=np.float32)
            w_sub = w_sub.reshape(n_w, -1)
            s_sub = np.asarray(win_steps_arr[idx, :n_w, :], dtype=np.int32)
            feats_list.append(w_sub)
            steps_list.append(s_sub)
        return feats_list, steps_list


def wilson_ci(k: int, n: int, z: float = 1.959963984540054) -> tuple[float | None, float | None]:
    """Wilson score 95% confidence interval for a binomial proportion k / n."""
    if n <= 0:
        return None, None
    p = k / n
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt((p * (1.0 - p)) / n + (z * z) / (4.0 * n * n))
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return float(lo), float(hi)


def cluster_bootstrap_auroc(
    y_true: np.ndarray,
    y_score: np.ndarray,
    clusters: Sequence[str],
    n_boot: int = N_BOOTSTRAP,
    seed: int = SEED,
) -> dict[str, Any]:
    """
    Compute exact AUROC and task-cluster bootstrap 95% CI (1000 resamples, seed=20261104).
    Uses vectorized cluster-multiplicity Mann-Whitney U for speed and exactness.
    """
    y_true = np.asarray(y_true, dtype=np.int32)
    y_score = np.asarray(y_score, dtype=np.float64)
    n = int(len(y_true))
    pos_mask = y_true == 1
    neg_mask = y_true == 0
    n_pos = int(np.sum(pos_mask))
    n_neg = int(np.sum(neg_mask))

    unique_clusters, cluster_inv = np.unique(np.asarray(clusters, dtype=str), return_inverse=True)
    n_clusters = int(len(unique_clusters))

    if n_pos == 0 or n_neg == 0:
        return {
            "value": None,
            "ci95": [None, None],
            "n_runs": n,
            "n_pos": n_pos,
            "n_neg": n_neg,
            "n_clusters": n_clusters,
            "n_boot_valid": 0,
        }

    auroc_val = float(roc_auc_score(y_true, y_score))

    # Pairwise comparison matrix M[i, j] for i in pos, j in neg:
    # 1.0 if s_pos[i] > s_neg[j], 0.5 if equal, 0.0 if <
    s_pos = y_score[pos_mask]
    s_neg = y_score[neg_mask]
    diff = s_pos[:, None] - s_neg[None, :]
    M = np.where(diff > 0.0, 1.0, np.where(diff == 0.0, 0.5, 0.0))

    # Cluster membership matrices: C_pos [K, N_pos], C_neg [K, N_neg]
    c_pos = cluster_inv[pos_mask]
    c_neg = cluster_inv[neg_mask]
    C_pos = np.zeros((n_clusters, n_pos), dtype=np.float64)
    C_neg = np.zeros((n_clusters, n_neg), dtype=np.float64)
    C_pos[c_pos, np.arange(n_pos)] = 1.0
    C_neg[c_neg, np.arange(n_neg)] = 1.0

    # Cluster-level pairwise matrix K_mat = C_pos @ M @ C_neg.T of shape [K, K]
    K_mat = C_pos @ M @ C_neg.T
    pos_counts_per_cluster = C_pos.sum(axis=1)  # [K]
    neg_counts_per_cluster = C_neg.sum(axis=1)  # [K]

    rng = np.random.default_rng(seed)
    # Sample K clusters with replacement for n_boot replicates
    sampled = rng.integers(0, n_clusters, size=(n_boot, n_clusters))
    # Multiplicity of each cluster in each bootstrap replicate: [n_boot, K]
    mult = np.zeros((n_boot, n_clusters), dtype=np.float64)
    rows_idx = np.repeat(np.arange(n_boot), n_clusters)
    np.add.at(mult, (rows_idx, sampled.ravel()), 1.0)

    tot_pos = mult @ pos_counts_per_cluster  # [n_boot]
    tot_neg = mult @ neg_counts_per_cluster  # [n_boot]
    denom = tot_pos * tot_neg
    valid = denom > 0.0

    if not np.any(valid):
        return {
            "value": auroc_val,
            "ci95": [None, None],
            "n_runs": n,
            "n_pos": n_pos,
            "n_neg": n_neg,
            "n_clusters": n_clusters,
            "n_boot_valid": 0,
        }

    mult_valid = mult[valid]
    num = np.sum((mult_valid @ K_mat) * mult_valid, axis=1)
    boot_aurocs = num / denom[valid]
    ci_lo, ci_hi = np.percentile(boot_aurocs, [2.5, 97.5])

    return {
        "value": auroc_val,
        "ci95": [float(ci_lo), float(ci_hi)],
        "n_runs": n,
        "n_pos": n_pos,
        "n_neg": n_neg,
        "n_clusters": n_clusters,
        "n_boot_valid": int(np.sum(valid)),
    }


def fit_scaler_and_logreg(
    X_train: np.ndarray, y_train: np.ndarray, c_val: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Fit StandardScaler and LogisticRegression(C=c_val, class_weight="balanced", max_iter=2000)
    on float32 features. Returns (scaler_mean, scaler_std, weights, bias) all in float32.
    """
    X_train_f32 = np.asarray(X_train, dtype=np.float32)
    y_train_i32 = np.asarray(y_train, dtype=np.int32)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_train_f32).astype(np.float32, copy=False)

    clf = LogisticRegression(
        C=float(c_val),
        class_weight="balanced",
        max_iter=2000,
        random_state=SEED,
        solver="lbfgs",
    )
    clf.fit(X_scaled, y_train_i32)

    scaler_mean = np.asarray(scaler.mean_, dtype=np.float32)
    scaler_std = np.asarray(scaler.scale_, dtype=np.float32)
    # Guard against zero std in degenerate coordinates
    scaler_std = np.where(scaler_std == 0.0, 1.0, scaler_std).astype(np.float32)
    weights = np.asarray(clf.coef_[0], dtype=np.float32)
    bias = np.asarray(clf.intercept_, dtype=np.float32).reshape(1)
    return scaler_mean, scaler_std, weights, bias


def score_matrix(
    X: np.ndarray, scaler_mean: np.ndarray, scaler_std: np.ndarray, weights: np.ndarray, bias: np.ndarray
) -> np.ndarray:
    """Compute linear logits in float32: ((X - mean) / std) @ weights + bias[0]."""
    X_f32 = np.asarray(X, dtype=np.float32)
    Z = (X_f32 - scaler_mean) / scaler_std
    return (Z @ weights + bias[0]).astype(np.float32)


def prepare_dev_data(
    runs: list[dict[str, Any]], vectors_dir: Path, split: str
) -> tuple[list[str], dict[str, list[dict[str, Any]]], dict[str, LazySetVectors], dict[str, np.ndarray]]:
    """
    Validate and group DEV runs by set, enforcing the strict constraint:
    never train or select thresholds on split == 'heldout' rows.
    """
    assert split == "dev", f"loso/fit only allows --split dev (got '{split}'); heldout rows are strictly forbidden."
    dev_runs = [r for r in runs if r.get("split") == "dev"]
    assert len(dev_runs) > 0, "No rows with split == 'dev' found in runs file; refusing to run on heldout rows."

    # Verify that no dev set shares a name with any heldout row
    dev_set_names = sorted({r["set"] for r in dev_runs})
    for r in runs:
        if r["set"] in dev_set_names:
            assert r.get("split") == "dev", (
                f"Set '{r['set']}' contains non-dev split '{r.get('split')}' row '{r.get('run_key')}'!"
            )

    by_set: dict[str, list[dict[str, Any]]] = {s: [] for s in dev_set_names}
    for r in dev_runs:
        assert r.get("split") != "heldout", f"Refusing heldout row {r.get('run_key')}"
        if r.get("label") not in (0, 1):
            raise ValueError(f"DEV run {r.get('run_key')} missing binary label (got {r.get('label')})")
        by_set[r["set"]].append(r)

    vec_by_set: dict[str, LazySetVectors] = {}
    idx_by_set: dict[str, np.ndarray] = {}
    for s in dev_set_names:
        npz_path = vectors_dir / f"{s}.npz"
        if not npz_path.exists():
            raise FileNotFoundError(f"Missing vector file for DEV set '{s}': {npz_path}")
        v = LazySetVectors(npz_path)
        vec_by_set[s] = v
        kept_rows = []
        idxs = []
        for r in by_set[s]:
            rk = r["run_key"]
            if rk not in v.key_to_idx:
                if int(r.get("n_steps") or 0) == 0 and int(r.get("n_chars") or 0) == 0:
                    continue
                raise KeyError(f"DEV run_key '{rk}' not found in {npz_path}")
            kept_rows.append(r)
            idxs.append(v.key_to_idx[rk])
        by_set[s] = kept_rows
        idx_by_set[s] = np.asarray(idxs, dtype=np.int32)

    return dev_set_names, by_set, vec_by_set, idx_by_set


def eval_config_loso_and_oof(
    cfg: dict[str, Any],
    dev_sets: list[str],
    by_set: dict[str, list[dict[str, Any]]],
    vec_by_set: dict[str, LazySetVectors],
    idx_by_set: dict[str, np.ndarray],
    win_model_cache: dict[tuple[str, float], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] | None = None,
) -> dict[str, Any]:
    """
    Evaluate a single grid config via Leave-One-Source-Out (LOSO) across dev_sets.
    Returns per-set AUROC (with cluster bootstrap CI), mean AUROC over sets,
    and pooled OOF AUROC + OOF negative scores.
    """
    level = cfg["level"]
    array_key = cfg["array_key"]
    layers = cfg["layers"]
    win_agg = cfg["win_agg"]
    c_val = float(cfg["C"])

    per_set_results: dict[str, Any] = {}
    set_aurocs: list[float] = []
    oof_y_all: list[np.ndarray] = []
    oof_s_all: list[np.ndarray] = []
    oof_c_all: list[str] = []

    for held_set in dev_sets:
        train_sets = [s for s in dev_sets if s != held_set]
        if not train_sets:
            raise ValueError("LOSO requires at least 2 DEV sets.")

        if level == "run":
            X_train_parts = [
                vec_by_set[s].get_run_features(array_key, layers, idx_by_set[s])
                for s in train_sets
            ]
            y_train_parts = [
                np.asarray([r["label"] for r in by_set[s]], dtype=np.int32)
                for s in train_sets
            ]
            X_train = np.concatenate(X_train_parts, axis=0)
            y_train = np.concatenate(y_train_parts, axis=0)

            scaler_mean, scaler_std, weights, bias = fit_scaler_and_logreg(X_train, y_train, c_val)

            X_test = vec_by_set[held_set].get_run_features(array_key, layers, idx_by_set[held_set])
            y_test = np.asarray([r["label"] for r in by_set[held_set]], dtype=np.int32)
            c_test = [str(r.get("cluster", r["run_key"])) for r in by_set[held_set]]
            s_test = score_matrix(X_test, scaler_mean, scaler_std, weights, bias)

        elif level == "window":
            cache_key = (held_set, c_val)
            if win_model_cache is not None and cache_key in win_model_cache:
                scaler_mean, scaler_std, weights, bias = win_model_cache[cache_key]
            else:
                X_win_parts: list[np.ndarray] = []
                y_win_parts: list[np.ndarray] = []
                for s in train_sets:
                    w_feats, _ = vec_by_set[s].get_window_features_for_runs(layers, idx_by_set[s])
                    for r, wf in zip(by_set[s], w_feats, strict=False):
                        if wf.shape[0] > 0:
                            X_win_parts.append(wf)
                            y_win_parts.append(np.full(wf.shape[0], int(r["label"]), dtype=np.int32))
                X_train = np.concatenate(X_win_parts, axis=0)
                y_train = np.concatenate(y_win_parts, axis=0)
                scaler_mean, scaler_std, weights, bias = fit_scaler_and_logreg(X_train, y_train, c_val)
                if win_model_cache is not None:
                    win_model_cache[cache_key] = (scaler_mean, scaler_std, weights, bias)

            w_feats_test, _ = vec_by_set[held_set].get_window_features_for_runs(layers, idx_by_set[held_set])
            y_test = np.asarray([r["label"] for r in by_set[held_set]], dtype=np.int32)
            c_test = [str(r.get("cluster", r["run_key"])) for r in by_set[held_set]]
            s_test = np.zeros(len(by_set[held_set]), dtype=np.float32)
            for i, wf in enumerate(w_feats_test):
                if wf.shape[0] == 0:
                    s_test[i] = 0.0
                else:
                    w_scores = score_matrix(wf, scaler_mean, scaler_std, weights, bias)
                    if win_agg == "max":
                        s_test[i] = float(np.max(w_scores))
                    elif win_agg == "mean":
                        s_test[i] = float(np.mean(w_scores))
                    else:
                        raise ValueError(f"Unknown win_agg: {win_agg}")
        else:
            raise ValueError(f"Unknown level: {level}")

        res = cluster_bootstrap_auroc(y_test, s_test, c_test, n_boot=N_BOOTSTRAP, seed=SEED)
        per_set_results[held_set] = res
        if res["value"] is not None:
            set_aurocs.append(float(res["value"]))
        oof_y_all.append(y_test)
        oof_s_all.append(s_test)
        oof_c_all.extend(c_test)

    oof_y = np.concatenate(oof_y_all, axis=0)
    oof_s = np.concatenate(oof_s_all, axis=0)
    pooled_oof = cluster_bootstrap_auroc(oof_y, oof_s, oof_c_all, n_boot=N_BOOTSTRAP, seed=SEED)
    mean_auroc = float(np.mean(set_aurocs)) if set_aurocs else None
    std_auroc = float(np.std(set_aurocs)) if set_aurocs else None

    return {
        "config": cfg,
        "mean_auroc": mean_auroc,
        "std_auroc": std_auroc,
        "pooled_oof_auroc": pooled_oof,
        "per_set": per_set_results,
    }


def cmd_loso(args: argparse.Namespace) -> int:
    assert args.split == "dev", "loso strictly requires --split dev; refusing heldout split."
    assert args.grid == "default", f"Only pre-registered --grid default is permitted (got '{args.grid}')."

    runs = load_runs(Path(args.runs))
    vectors_dir = Path(args.vectors)
    dev_sets, by_set, vec_by_set, idx_by_set = prepare_dev_data(runs, vectors_dir, args.split)

    win_model_cache: dict[tuple[str, float], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    config_results: list[dict[str, Any]] = []

    for cfg in DEFAULT_GRID:
        res = eval_config_loso_and_oof(
            cfg, dev_sets, by_set, vec_by_set, idx_by_set, win_model_cache=win_model_cache
        )
        config_results.append(res)

    # Length baselines on the exact same DEV rows: n_chars, n_steps (from runs.jsonl), n_tokens (from vector file)
    baselines: dict[str, Any] = {}
    for b_name in ("n_chars", "n_steps", "n_tokens"):
        per_set_b: dict[str, Any] = {}
        b_aurocs: list[float] = []
        all_y: list[np.ndarray] = []
        all_s: list[np.ndarray] = []
        all_c: list[str] = []

        for s in dev_sets:
            y_s = np.asarray([r["label"] for r in by_set[s]], dtype=np.int32)
            c_s = [str(r.get("cluster", r["run_key"])) for r in by_set[s]]
            if b_name in ("n_chars", "n_steps"):
                scores_s = np.asarray([float(r.get(b_name, 0)) for r in by_set[s]], dtype=np.float64)
            else:
                scores_s = vec_by_set[s].get_n_tokens(idx_by_set[s]).astype(np.float64)

            res_s = cluster_bootstrap_auroc(y_s, scores_s, c_s, n_boot=N_BOOTSTRAP, seed=SEED)
            per_set_b[s] = res_s
            if res_s["value"] is not None:
                b_aurocs.append(float(res_s["value"]))
            all_y.append(y_s)
            all_s.append(scores_s)
            all_c.extend(c_s)

        pooled_b = cluster_bootstrap_auroc(
            np.concatenate(all_y), np.concatenate(all_s), all_c, n_boot=N_BOOTSTRAP, seed=SEED
        )
        baselines[b_name] = {
            "mean_auroc": float(np.mean(b_aurocs)) if b_aurocs else None,
            "std_auroc": float(np.std(b_aurocs)) if b_aurocs else None,
            "pooled_auroc": pooled_b,
            "per_set": per_set_b,
        }

    ranked = sorted(
        config_results,
        key=lambda x: (x["mean_auroc"] if x["mean_auroc"] is not None else -1.0),
        reverse=True,
    )
    best_cfg_name = ranked[0]["config"]["name"] if ranked else None

    out_payload = {
        "split": "dev",
        "grid": "default",
        "seed": SEED,
        "n_bootstrap": N_BOOTSTRAP,
        "dev_sets": dev_sets,
        "n_dev_runs": int(sum(len(by_set[s]) for s in dev_sets)),
        "best_config": best_cfg_name,
        "best_mean_auroc": ranked[0]["mean_auroc"] if ranked else None,
        "configs": config_results,
        "length_baselines": baselines,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_payload, f, indent=2, sort_keys=False)

    return 0


def compute_probe_version(
    cfg: dict[str, Any],
    dev_run_keys: Sequence[str],
    weights: np.ndarray,
    bias: np.ndarray,
    vec_metas: dict[str, dict[str, Any]],
) -> str:
    """
    Compute deterministic version = sha256 of:
    (config JSON + sorted DEV run_keys + weights bytes + meta.render_sha/extract_sha/model/revision of vector files)
    """
    h = hashlib.sha256()
    cfg_canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    h.update(cfg_canonical.encode("utf-8"))
    h.update(b"\n")
    h.update(json.dumps(sorted(dev_run_keys), separators=(",", ":")).encode("utf-8"))
    h.update(b"\n")
    h.update(np.asarray(weights, dtype=np.float32).tobytes())
    h.update(np.asarray(bias, dtype=np.float32).tobytes())
    h.update(b"\n")
    meta_summary = []
    for s in sorted(vec_metas.keys()):
        m = vec_metas[s]
        meta_summary.append(
            {
                "set": s,
                "render_sha": m.get("render_sha", ""),
                "extract_sha": m.get("extract_sha", ""),
                "model": m.get("model", ""),
                "revision": m.get("revision", ""),
            }
        )
    h.update(json.dumps(meta_summary, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return h.hexdigest()


def compute_fpr_threshold(neg_scores: np.ndarray, target_fpr: float) -> float:
    """
    Compute score threshold tau on DEV negative scores such that strictly exceeding tau
    (score > tau) yields empirical FPR <= target_fpr on DEV negatives.
    Uses quantile(neg_scores, 1.0 - target_fpr).
    """
    neg_scores = np.asarray(neg_scores, dtype=np.float64)
    if neg_scores.size == 0:
        return float("inf")
    return float(np.quantile(neg_scores, 1.0 - target_fpr))


def cmd_fit(args: argparse.Namespace) -> int:
    assert args.split == "dev", "fit strictly requires --split dev; refusing heldout split."
    runs = load_runs(Path(args.runs))
    vectors_dir = Path(args.vectors)
    dev_sets, by_set, vec_by_set, idx_by_set = prepare_dev_data(runs, vectors_dir, args.split)

    cfg = resolve_config(args.config)
    level = cfg["level"]
    array_key = cfg["array_key"]
    layers = cfg["layers"]
    win_agg = cfg["win_agg"]
    c_val = float(cfg["C"])

    # Fit on ALL DEV sets
    if level == "run":
        X_parts = [vec_by_set[s].get_run_features(array_key, layers, idx_by_set[s]) for s in dev_sets]
        y_parts = [np.asarray([r["label"] for r in by_set[s]], dtype=np.int32) for s in dev_sets]
        X_all = np.concatenate(X_parts, axis=0)
        y_all = np.concatenate(y_parts, axis=0)
        scaler_mean, scaler_std, weights, bias = fit_scaler_and_logreg(X_all, y_all, c_val)
        in_sample_dev_scores = score_matrix(X_all, scaler_mean, scaler_std, weights, bias)

        # Compute LOSO out-of-fold scores on DEV sets (aligned to full-DEV weight norm)
        # so DEV negative thresholds are genuinely out-of-source and free of D>>N memorization shift.
        full_norm = float(np.linalg.norm(weights))
        oof_scores_list: list[np.ndarray] = []
        oof_ev_steps_list: list[list[int]] = []
        for s_idx, held_set in enumerate(dev_sets):
            tr_X = np.concatenate([X_parts[i] for i in range(len(dev_sets)) if i != s_idx], axis=0)
            tr_y = np.concatenate([y_parts[i] for i in range(len(dev_sets)) if i != s_idx], axis=0)
            sm_f, ss_f, w_f, b_f = fit_scaler_and_logreg(tr_X, tr_y, c_val)
            f_norm = float(np.linalg.norm(w_f))
            scale = (full_norm / f_norm) if f_norm > 0.0 else 1.0
            sc_held = score_matrix(X_parts[s_idx], sm_f, ss_f, w_f * scale, b_f * scale)
            oof_scores_list.append(sc_held)
            oof_ev_steps_list.extend([[-1, -1]] * len(by_set[held_set]))
        dev_scores = np.concatenate(oof_scores_list, axis=0).astype(np.float32)
        dev_oof_ev_steps = np.asarray(oof_ev_steps_list, dtype=np.int32)

    elif level == "window":
        X_win_parts: list[np.ndarray] = []
        y_win_parts: list[np.ndarray] = []
        per_set_w_feats: dict[str, list[np.ndarray]] = {}
        per_set_w_steps: dict[str, list[np.ndarray]] = {}
        per_run_w_feats: list[np.ndarray] = []
        y_run_list: list[int] = []
        for s in dev_sets:
            w_feats, w_steps = vec_by_set[s].get_window_features_for_runs(layers, idx_by_set[s])
            per_set_w_feats[s] = w_feats
            per_set_w_steps[s] = w_steps
            for r, wf in zip(by_set[s], w_feats, strict=False):
                per_run_w_feats.append(wf)
                y_run_list.append(int(r["label"]))
                if wf.shape[0] > 0:
                    X_win_parts.append(wf)
                    y_win_parts.append(np.full(wf.shape[0], int(r["label"]), dtype=np.int32))
        X_all_win = np.concatenate(X_win_parts, axis=0)
        y_all_win = np.concatenate(y_win_parts, axis=0)
        scaler_mean, scaler_std, weights, bias = fit_scaler_and_logreg(X_all_win, y_all_win, c_val)

        y_all = np.asarray(y_run_list, dtype=np.int32)
        in_sample_dev_scores = np.zeros(len(per_run_w_feats), dtype=np.float32)
        for i, wf in enumerate(per_run_w_feats):
            if wf.shape[0] == 0:
                in_sample_dev_scores[i] = 0.0
            else:
                w_sc = score_matrix(wf, scaler_mean, scaler_std, weights, bias)
                in_sample_dev_scores[i] = float(np.max(w_sc)) if win_agg == "max" else float(np.mean(w_sc))

        full_norm = float(np.linalg.norm(weights))
        oof_scores_vals: list[float] = []
        oof_ev_steps_list = []
        for held_set in dev_sets:
            tr_win_X: list[np.ndarray] = []
            tr_win_y: list[np.ndarray] = []
            for s in dev_sets:
                if s == held_set:
                    continue
                for r, wf in zip(by_set[s], per_set_w_feats[s], strict=False):
                    if wf.shape[0] > 0:
                        tr_win_X.append(wf)
                        tr_win_y.append(np.full(wf.shape[0], int(r["label"]), dtype=np.int32))
            sm_f, ss_f, w_f, b_f = fit_scaler_and_logreg(
                np.concatenate(tr_win_X, axis=0), np.concatenate(tr_win_y, axis=0), c_val
            )
            f_norm = float(np.linalg.norm(w_f))
            scale = (full_norm / f_norm) if f_norm > 0.0 else 1.0
            for wf, ws in zip(per_set_w_feats[held_set], per_set_w_steps[held_set], strict=False):
                if wf.shape[0] == 0:
                    oof_scores_vals.append(0.0)
                    oof_ev_steps_list.append([-1, -1])
                else:
                    w_sc = score_matrix(wf, sm_f, ss_f, w_f * scale, b_f * scale)
                    best_w = int(np.argmax(w_sc))
                    sc_val = float(w_sc[best_w]) if win_agg == "max" else float(np.mean(w_sc))
                    oof_scores_vals.append(sc_val)
                    oof_ev_steps_list.append([int(ws[best_w, 0]), int(ws[best_w, 1])])
        dev_scores = np.asarray(oof_scores_vals, dtype=np.float32)
        dev_oof_ev_steps = np.asarray(oof_ev_steps_list, dtype=np.int32)
    else:
        raise ValueError(f"Unknown level: {level}")

    dev_run_keys_ordered = [r["run_key"] for s in dev_sets for r in by_set[s]]
    vec_metas = {s: vec_by_set[s].meta for s in dev_sets}
    version = compute_probe_version(cfg, dev_run_keys_ordered, weights, bias, vec_metas)

    dev_neg_mask = y_all == 0
    dev_neg_scores = np.sort(dev_scores[dev_neg_mask].astype(np.float32))
    thr_1pct = compute_fpr_threshold(dev_neg_scores, 0.01)
    thr_5pct = compute_fpr_threshold(dev_neg_scores, 0.05)

    model_names = sorted({str(vec_metas[s].get("model", "")) for s in dev_sets if vec_metas[s].get("model")})
    model_str = model_names[0] if model_names else "Qwen/Qwen3-8B"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        out_path,
        scaler_mean=scaler_mean.astype(np.float32),
        scaler_std=scaler_std.astype(np.float32),
        weights=weights.astype(np.float32),
        bias=bias.astype(np.float32),
        config=np.array(json.dumps(cfg, sort_keys=True)),
        version=np.array(version),
        model=np.array(model_str),
        dev_run_keys=np.asarray(sorted(dev_run_keys_ordered)),
        dev_run_keys_ordered=np.asarray(dev_run_keys_ordered),
        dev_scores=dev_scores.astype(np.float32),
        dev_in_sample_scores=in_sample_dev_scores.astype(np.float32),
        dev_oof_ev_steps=dev_oof_ev_steps.astype(np.int32),
        dev_labels=y_all.astype(np.int32),
        dev_neg_scores=dev_neg_scores.astype(np.float32),
        threshold_1pct_fpr=np.array(thr_1pct, dtype=np.float64),
        threshold_5pct_fpr=np.array(thr_5pct, dtype=np.float64),
    )
    return 0


def parse_sets_arg(sets_arg: Sequence[str], available_sets: Sequence[str]) -> list[str]:
    if len(sets_arg) == 1 and sets_arg[0].strip().lower() == "all":
        return list(available_sets)
    out: list[str] = []
    for token in sets_arg:
        for part in token.split(","):
            p = part.strip()
            if p:
                out.append(p)
    if len(out) == 1 and out[0].lower() == "all":
        return list(available_sets)
    return out


def cmd_score(args: argparse.Namespace) -> int:
    probe_npz = np.load(Path(args.probe), allow_pickle=False)
    scaler_mean = np.asarray(probe_npz["scaler_mean"], dtype=np.float32)
    scaler_std = np.asarray(probe_npz["scaler_std"], dtype=np.float32)
    weights = np.asarray(probe_npz["weights"], dtype=np.float32)
    bias = np.asarray(probe_npz["bias"], dtype=np.float32).reshape(1)
    cfg = json.loads(str(probe_npz["config"].item()))
    version = str(probe_npz["version"].item())
    fallback_model = str(probe_npz["model"].item()) if "model" in probe_npz.files else "Qwen/Qwen3-8B"
    level = cfg["level"]
    array_key = cfg["array_key"]
    layers = cfg["layers"]
    win_agg = cfg["win_agg"]

    dev_oof_map: dict[str, tuple[float, list[int]]] = {}
    if "dev_run_keys_ordered" in probe_npz.files and "dev_scores" in probe_npz.files:
        rk_ord = [str(k) for k in probe_npz["dev_run_keys_ordered"].tolist()]
        sc_ord = probe_npz["dev_scores"].tolist()
        ev_ord = (
            probe_npz["dev_oof_ev_steps"].tolist()
            if "dev_oof_ev_steps" in probe_npz.files
            else [[-1, -1]] * len(rk_ord)
        )
        for k, sc_v, ev_v in zip(rk_ord, sc_ord, ev_ord, strict=False):
            ev_clean = [int(ev_v[0]), int(ev_v[1])] if (level == "window" and int(ev_v[0]) >= 0) else []
            dev_oof_map[k] = (float(sc_v), ev_clean)

    runs = load_runs(Path(args.runs))
    seen_sets: list[str] = []
    for r in runs:
        if r["set"] not in seen_sets:
            seen_sets.append(r["set"])
    target_sets = set(parse_sets_arg(args.sets, seen_sets))

    vectors_dir = Path(args.vectors)
    vec_cache: dict[str, LazySetVectors | None] = {}
    for s in target_sets:
        npz_path = vectors_dir / f"{s}.npz"
        vec_cache[s] = LazySetVectors(npz_path) if npz_path.exists() else None

    signal_rows: list[dict[str, Any]] = []
    for r in runs:
        s = r["set"]
        if s not in target_sets:
            continue
        rk = r["run_key"]
        v = vec_cache.get(s)
        if v is None:
            signal_rows.append(
                {
                    "run_key": rk,
                    "signal": args.signal_name,
                    "version": version,
                    "score": None,
                    "abstain": f"missing_vector_file:{s}.npz",
                    "evidence_steps": [],
                    "model": fallback_model,
                    "cost_usd": 0.0,
                }
            )
            continue

        model_str = str(v.meta.get("model", fallback_model))
        if rk not in v.key_to_idx:
            signal_rows.append(
                {
                    "run_key": rk,
                    "signal": args.signal_name,
                    "version": version,
                    "score": None,
                    "abstain": "missing_run_vector",
                    "evidence_steps": [],
                    "model": model_str,
                    "cost_usd": 0.0,
                }
            )
            continue

        idx = v.key_to_idx[rk]
        if level == "run":
            x = v.get_run_features(array_key, layers, [idx])
            if not np.all(np.isfinite(x)):
                signal_rows.append(
                    {
                        "run_key": rk,
                        "signal": args.signal_name,
                        "version": version,
                        "score": None,
                        "abstain": "non_finite_vector",
                        "evidence_steps": [],
                        "model": model_str,
                        "cost_usd": 0.0,
                    }
                )
                continue
            if rk in dev_oof_map:
                sc = dev_oof_map[rk][0]
            else:
                sc = float(score_matrix(x, scaler_mean, scaler_std, weights, bias)[0])
            signal_rows.append(
                {
                    "run_key": rk,
                    "signal": args.signal_name,
                    "version": version,
                    "score": sc,
                    "abstain": None,
                    "evidence_steps": [],
                    "model": model_str,
                    "cost_usd": 0.0,
                }
            )
        elif level == "window":
            w_feats, w_steps = v.get_window_features_for_runs(layers, [idx])
            wf = w_feats[0]
            ws = w_steps[0]
            if wf.shape[0] == 0:
                signal_rows.append(
                    {
                        "run_key": rk,
                        "signal": args.signal_name,
                        "version": version,
                        "score": None,
                        "abstain": "zero_windows",
                        "evidence_steps": [],
                        "model": model_str,
                        "cost_usd": 0.0,
                    }
                )
                continue
            if not np.all(np.isfinite(wf)):
                signal_rows.append(
                    {
                        "run_key": rk,
                        "signal": args.signal_name,
                        "version": version,
                        "score": None,
                        "abstain": "non_finite_window_vector",
                        "evidence_steps": [],
                        "model": model_str,
                        "cost_usd": 0.0,
                    }
                )
                continue
            if rk in dev_oof_map:
                sc, ev_steps = dev_oof_map[rk]
            else:
                w_scores = score_matrix(wf, scaler_mean, scaler_std, weights, bias)
                best_w = int(np.argmax(w_scores))
                sc = float(w_scores[best_w]) if win_agg == "max" else float(np.mean(w_scores))
                step_span = ws[best_w]
                ev_steps = [int(step_span[0]), int(step_span[1])] if int(step_span[0]) >= 0 else []
            signal_rows.append(
                {
                    "run_key": rk,
                    "signal": args.signal_name,
                    "version": version,
                    "score": sc,
                    "abstain": None,
                    "evidence_steps": ev_steps,
                    "model": model_str,
                    "cost_usd": 0.0,
                }
            )
        else:
            raise ValueError(f"Unknown level: {level}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for row in signal_rows:
            f.write(json.dumps(row) + "\n")
    return 0


def bayes_precision(recall: float | None, fpr: float | None, base_rate: float) -> float | None:
    if recall is None or fpr is None:
        return None
    if recall == 0.0 and fpr == 0.0:
        return 0.0
    num = base_rate * recall
    den = num + (1.0 - base_rate) * fpr
    if den <= 0.0:
        return 0.0
    return float(num / den)


def evaluate_subset_metrics(
    rows: list[dict[str, Any]],
    sig_map: dict[str, dict[str, Any]],
    thr_1pct: float,
    thr_5pct: float,
) -> dict[str, Any]:
    """
    Compute full metrics on a subset of runs:
    - coverage (total, scored, abstained, abstain_rate + Wilson 95% CI, abstain reasons)
    - AUROC with cluster bootstrap 95% CI (on scored rows, and with abstentions treated as -inf)
    - recall and FPR at DEV 1% and 5% FPR thresholds (with Wilson 95% CIs, both on all rows
      where abstentions are not flagged, and on scored-only rows)
    - precision at base rates 1%, 5%, 20%
    """
    n_total = len(rows)
    n_pos_total = sum(1 for r in rows if r.get("label") == 1)
    n_neg_total = sum(1 for r in rows if r.get("label") == 0)

    scored_y: list[int] = []
    scored_s: list[float] = []
    scored_c: list[str] = []
    abstain_reasons: dict[str, int] = {}

    tp_1 = 0
    fp_1 = 0
    tp_5 = 0
    fp_5 = 0
    n_abstain = 0

    for r in rows:
        rk = r["run_key"]
        lbl = int(r["label"])
        cl = str(r.get("cluster", rk))
        srow = sig_map.get(rk)
        if srow is None or srow.get("score") is None:
            n_abstain += 1
            reason = "missing_signal_row" if srow is None else str(srow.get("abstain") or "unspecified")
            abstain_reasons[reason] = abstain_reasons.get(reason, 0) + 1
            continue

        sc = float(srow["score"])
        scored_y.append(lbl)
        scored_s.append(sc)
        scored_c.append(cl)

        if sc > thr_1pct:
            if lbl == 1:
                tp_1 += 1
            else:
                fp_1 += 1
        if sc > thr_5pct:
            if lbl == 1:
                tp_5 += 1
            else:
                fp_5 += 1

    n_scored = n_total - n_abstain
    abstain_rate = (n_abstain / n_total) if n_total > 0 else 0.0
    abstain_ci_lo, abstain_ci_hi = wilson_ci(n_abstain, n_total)

    auroc_scored = cluster_bootstrap_auroc(
        np.asarray(scored_y, dtype=np.int32),
        np.asarray(scored_s, dtype=np.float64),
        scored_c,
        n_boot=N_BOOTSTRAP,
        seed=SEED,
    )

    def build_OperatingPoint(tp: int, fp: int, nom_fpr: float, thr_val: float) -> dict[str, Any]:
        rec = (tp / n_pos_total) if n_pos_total > 0 else None
        rec_lo, rec_hi = wilson_ci(tp, n_pos_total)
        fpr = (fp / n_neg_total) if n_neg_total > 0 else None
        fpr_lo, fpr_hi = wilson_ci(fp, n_neg_total)
        # For Bayes precision, use empirical FPR (and also provide nominal-FPR precision when empirical FPR == 0)
        eff_fpr = fpr if (fpr is not None and fpr > 0.0) else nom_fpr
        return {
            "threshold": thr_val,
            "tp": tp,
            "n_pos": n_pos_total,
            "recall": rec,
            "recall_ci95": [rec_lo, rec_hi],
            "fp": fp,
            "n_neg": n_neg_total,
            "fpr": fpr,
            "fpr_ci95": [fpr_lo, fpr_hi],
            "precision_at_base_rates": {
                "1pct": bayes_precision(rec, fpr, 0.01),
                "5pct": bayes_precision(rec, fpr, 0.05),
                "20pct": bayes_precision(rec, fpr, 0.20),
            },
            "precision_at_base_rates_floor_nominal_fpr": {
                "1pct": bayes_precision(rec, eff_fpr, 0.01),
                "5pct": bayes_precision(rec, eff_fpr, 0.05),
                "20pct": bayes_precision(rec, eff_fpr, 0.20),
            },
        }

    return {
        "coverage": {
            "n_total": n_total,
            "n_scored": n_scored,
            "n_abstain": n_abstain,
            "abstain_rate": abstain_rate,
            "abstain_rate_ci95": [abstain_ci_lo, abstain_ci_hi],
            "abstain_reasons": abstain_reasons,
        },
        "auroc": auroc_scored,
        "at_dev_1pct_fpr": build_OperatingPoint(tp_1, fp_1, 0.01, thr_1pct),
        "at_dev_5pct_fpr": build_OperatingPoint(tp_5, fp_5, 0.05, thr_5pct),
    }


def evaluate_or_combination_subset(
    rows: list[dict[str, Any]],
    sig_map_a: dict[str, dict[str, Any]],
    sig_map_b: dict[str, dict[str, Any]],
    thr_a_1: float,
    thr_b_1: float,
    thr_a_5: float,
    thr_b_5: float,
) -> dict[str, Any]:
    """
    Evaluate the OR-cascade rule on a subset of runs:
    Flagged at 5% (resp. 1%) if signal A > thr_A OR signal B > thr_B.
    """
    n_total = len(rows)
    n_pos = sum(1 for r in rows if r.get("label") == 1)
    n_neg = sum(1 for r in rows if r.get("label") == 0)

    tp_1 = 0
    fp_1 = 0
    tp_5 = 0
    fp_5 = 0
    n_abstain_both = 0

    for r in rows:
        rk = r["run_key"]
        lbl = int(r["label"])
        ra = sig_map_a.get(rk)
        rb = sig_map_b.get(rk)
        sa = None if (ra is None or ra.get("score") is None) else float(ra["score"])
        sb = None if (rb is None or rb.get("score") is None) else float(rb["score"])

        if sa is None and sb is None:
            n_abstain_both += 1
            continue

        flag_1 = (sa is not None and sa > thr_a_1) or (sb is not None and sb > thr_b_1)
        flag_5 = (sa is not None and sa > thr_a_5) or (sb is not None and sb > thr_b_5)

        if flag_1:
            if lbl == 1:
                tp_1 += 1
            else:
                fp_1 += 1
        if flag_5:
            if lbl == 1:
                tp_5 += 1
            else:
                fp_5 += 1

    def op_dict(tp: int, fp: int, nom_fpr: float) -> dict[str, Any]:
        rec = (tp / n_pos) if n_pos > 0 else None
        rec_lo, rec_hi = wilson_ci(tp, n_pos)
        fpr = (fp / n_neg) if n_neg > 0 else None
        fpr_lo, fpr_hi = wilson_ci(fp, n_neg)
        eff_fpr = fpr if (fpr is not None and fpr > 0.0) else nom_fpr
        return {
            "tp": tp,
            "n_pos": n_pos,
            "recall": rec,
            "recall_ci95": [rec_lo, rec_hi],
            "fp": fp,
            "n_neg": n_neg,
            "fpr": fpr,
            "fpr_ci95": [fpr_lo, fpr_hi],
            "precision_at_base_rates": {
                "1pct": bayes_precision(rec, fpr, 0.01),
                "5pct": bayes_precision(rec, fpr, 0.05),
                "20pct": bayes_precision(rec, fpr, 0.20),
            },
            "precision_at_base_rates_floor_nominal_fpr": {
                "1pct": bayes_precision(rec, eff_fpr, 0.01),
                "5pct": bayes_precision(rec, eff_fpr, 0.05),
                "20pct": bayes_precision(rec, eff_fpr, 0.20),
            },
        }

    abstain_ci_lo, abstain_ci_hi = wilson_ci(n_abstain_both, n_total)
    return {
        "coverage": {
            "n_total": n_total,
            "n_scored": n_total - n_abstain_both,
            "n_abstain": n_abstain_both,
            "abstain_rate": (n_abstain_both / n_total) if n_total > 0 else 0.0,
            "abstain_rate_ci95": [abstain_ci_lo, abstain_ci_hi],
        },
        "at_dev_1pct_fpr": op_dict(tp_1, fp_1, 0.01),
        "at_dev_5pct_fpr": op_dict(tp_5, fp_5, 0.05),
    }


def evaluate_full_signal(
    sig_name: str,
    runs: list[dict[str, Any]],
    sig_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    Evaluate a single signal across:
    - DEV thresholds (strictly from split == 'dev' negatives)
    - Per set (all rows and natural-only rows)
    - Pooled by split ('dev', 'heldout') (all rows and natural-only rows)
    """
    dev_neg_scores: list[float] = []
    for r in runs:
        if r.get("split") == "dev" and int(r.get("label", -1)) == 0:
            assert r.get("split") != "heldout"
            srow = sig_map.get(r["run_key"])
            if srow is not None and srow.get("score") is not None:
                dev_neg_scores.append(float(srow["score"]))

    dev_neg_arr = np.asarray(dev_neg_scores, dtype=np.float64)
    thr_1pct = compute_fpr_threshold(dev_neg_arr, 0.01)
    thr_5pct = compute_fpr_threshold(dev_neg_arr, 0.05)

    sets_in_order: list[str] = []
    for r in runs:
        if r["set"] not in sets_in_order:
            sets_in_order.append(r["set"])

    per_set: dict[str, Any] = {}
    per_set_natural: dict[str, Any] = {}
    for s in sets_in_order:
        s_rows = [r for r in runs if r["set"] == s]
        s_nat_rows = [r for r in s_rows if r.get("natural") is True]
        split_name = s_rows[0]["split"] if s_rows else "unknown"
        m_all = evaluate_subset_metrics(s_rows, sig_map, thr_1pct, thr_5pct)
        m_all["split"] = split_name
        per_set[s] = m_all

        m_nat = evaluate_subset_metrics(s_nat_rows, sig_map, thr_1pct, thr_5pct)
        m_nat["split"] = split_name
        per_set_natural[s] = m_nat

    by_split: dict[str, Any] = {}
    by_split_natural: dict[str, Any] = {}
    for sp in ("dev", "heldout"):
        sp_rows = [r for r in runs if r.get("split") == sp]
        if not sp_rows:
            continue
        sp_nat_rows = [r for r in sp_rows if r.get("natural") is True]
        by_split[sp] = evaluate_subset_metrics(sp_rows, sig_map, thr_1pct, thr_5pct)
        by_split_natural[sp] = evaluate_subset_metrics(sp_nat_rows, sig_map, thr_1pct, thr_5pct)

    return {
        "signal": sig_name,
        "thresholds": {
            "source": "dev_negatives",
            "n_dev_negatives": int(dev_neg_arr.size),
            "fpr_1pct": thr_1pct,
            "fpr_5pct": thr_5pct,
        },
        "by_split": by_split,
        "by_split_natural_only": by_split_natural,
        "per_set": per_set,
        "per_set_natural_only": per_set_natural,
    }


def build_max_rank_signal_map(
    runs: list[dict[str, Any]],
    sig_map_a: dict[str, dict[str, Any]],
    sig_map_b: dict[str, dict[str, Any]],
    combo_name: str,
) -> dict[str, dict[str, Any]]:
    """
    Construct a continuous combined signal 'max_rank' where each signal is rank-normalized
    against its DEV empirical CDF (strictly using split == 'dev' scores so heldout is never leaked),
    and combined via max(rank_a, rank_b).
    """
    dev_a = np.sort(
        np.asarray(
            [
                float(sig_map_a[r["run_key"]]["score"])
                for r in runs
                if r.get("split") == "dev"
                and r["run_key"] in sig_map_a
                and sig_map_a[r["run_key"]].get("score") is not None
            ],
            dtype=np.float64,
        )
    )
    dev_b = np.sort(
        np.asarray(
            [
                float(sig_map_b[r["run_key"]]["score"])
                for r in runs
                if r.get("split") == "dev"
                and r["run_key"] in sig_map_b
                and sig_map_b[r["run_key"]].get("score") is not None
            ],
            dtype=np.float64,
        )
    )

    def ecdf_rank(val: float | None, ref: np.ndarray) -> float | None:
        if val is None or ref.size == 0:
            return None
        pos = int(np.searchsorted(ref, val, side="right"))
        return float(pos / ref.size)

    out_map: dict[str, dict[str, Any]] = {}
    for r in runs:
        rk = r["run_key"]
        ra = sig_map_a.get(rk)
        rb = sig_map_b.get(rk)
        sa = None if (ra is None or ra.get("score") is None) else float(ra["score"])
        sb = None if (rb is None or rb.get("score") is None) else float(rb["score"])
        qa = ecdf_rank(sa, dev_a)
        qb = ecdf_rank(sb, dev_b)
        if qa is None and qb is None:
            out_map[rk] = {"run_key": rk, "signal": combo_name, "score": None, "abstain": "both_abstained"}
        elif qa is None:
            out_map[rk] = {"run_key": rk, "signal": combo_name, "score": qb, "abstain": None}
        elif qb is None:
            out_map[rk] = {"run_key": rk, "signal": combo_name, "score": qa, "abstain": None}
        else:
            out_map[rk] = {"run_key": rk, "signal": combo_name, "score": max(qa, qb), "abstain": None}
    return out_map


def fmt_val_ci(val: float | None, ci: Sequence[float | None] | None, denom: str | None = None) -> str:
    if val is None:
        return "N/A"
    s = f"{val:.3f}"
    if denom is not None:
        s += f" ({denom})"
    if ci and len(ci) == 2 and ci[0] is not None and ci[1] is not None:
        s += f" [{ci[0]:.3f}, {ci[1]:.3f}]"
    return s


def render_report_markdown(
    report_data: dict[str, Any],
    loso_summary: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# Probe & Baseline Scoring Report")
    lines.append("")
    lines.append(
        f"- **Runs total**: {report_data['n_runs']} "
        f"(DEV: {report_data['n_dev_runs']}, Held-out: {report_data['n_heldout_runs']})"
    )
    lines.append(
        f"- **Bootstrap**: {N_BOOTSTRAP} cluster resamples (`seed={SEED}`, cluster = `cluster` field)"
    )
    lines.append(
        f"- **Threshold source**: `{report_data['thresholds_from']}` (DEV negative scores only; held-out rows never touched for training or thresholding)"
    )
    lines.append("")

    if loso_summary is not None:
        lines.append("## 1. DEV Leave-One-Source-Out (LOSO) Pre-registered Grid Summary")
        lines.append("")
        lines.append(
            f"- **Best grid config on DEV LOSO**: `{loso_summary.get('best_config')}` "
            f"(mean LOSO AUROC = `{loso_summary.get('best_mean_auroc', 0.0):.4f}`)"
        )
        lines.append("")
        dev_sets = loso_summary.get("dev_sets", [])
        set_Cols = " | ".join(dev_sets)
        lines.append(f"| Config / Baseline | Mean LOSO AUROC | Pooled OOF AUROC [95% CI] | {set_Cols} |")
        sep = " | ".join(["---"] * (3 + len(dev_sets)))
        lines.append(f"| {sep} |")
        for c_entry in loso_summary.get("configs", []):
            cname = c_entry["config"]["name"]
            m_auroc = c_entry["mean_auroc"]
            p_oof = c_entry["pooled_oof_auroc"]
            per_s = [
                fmt_val_ci(
                    c_entry["per_set"][s]["value"],
                    c_entry["per_set"][s]["ci95"],
                    f"{c_entry['per_set'][s]['n_pos']}+/{c_entry['per_set'][s]['n_neg']}-",
                )
                for s in dev_sets
            ]
            oof_denom = f"{p_oof['n_pos']}+/{p_oof['n_neg']}-"
            lines.append(
                f"| `{cname}` | {m_auroc:.3f} | {fmt_val_ci(p_oof['value'], p_oof['ci95'], oof_denom)} | "
                + " | ".join(per_s)
                + " |"
            )
        for bname, b_entry in loso_summary.get("length_baselines", {}).items():
            m_auroc = b_entry["mean_auroc"]
            p_b = b_entry["pooled_auroc"]
            per_s = [
                fmt_val_ci(
                    b_entry["per_set"][s]["value"],
                    b_entry["per_set"][s]["ci95"],
                    f"{b_entry['per_set'][s]['n_pos']}+/{b_entry['per_set'][s]['n_neg']}-",
                )
                for s in dev_sets
            ]
            b_denom = f"{p_b['n_pos']}+/{p_b['n_neg']}-"
            lines.append(
                f"| `baseline:{bname}` | {m_auroc:.3f} | {fmt_val_ci(p_b['value'], p_b['ci95'], b_denom)} | "
                + " | ".join(per_s)
                + " |"
            )
        lines.append("")

    lines.append("## 2. Pooled Split Performance (All Rows)")
    lines.append("")
    lines.append(
        "| Signal | Split | AUROC (pos+/neg-) [95% CI] | Recall @ 1% DEV FPR [95% CI] | FPR @ 1% DEV Thr [95% CI] | Recall @ 5% DEV FPR [95% CI] | FPR @ 5% DEV Thr [95% CI] | Prec @ 1% / 5% / 20% Base (5% Thr) | Abstain Rate [95% CI] |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")

    signals_dict: dict[str, Any] = report_data["signals"]
    for sname, sdata in signals_dict.items():
        for sp in ("dev", "heldout"):
            if sp not in sdata["by_split"]:
                continue
            m = sdata["by_split"][sp]
            au = m["auroc"]
            op1 = m["at_dev_1pct_fpr"]
            op5 = m["at_dev_5pct_fpr"]
            cov = m["coverage"]
            p5 = op5["precision_at_base_rates_floor_nominal_fpr"]
            prec_str = (
                f"{p5['1pct']:.3f} / {p5['5pct']:.3f} / {p5['20pct']:.3f}"
                if p5["1pct"] is not None
                else "N/A"
            )
            au_d = f"{au['n_pos']}+/{au['n_neg']}-"
            r1_d = f"{op1['tp']}/{op1['n_pos']}"
            f1_d = f"{op1['fp']}/{op1['n_neg']}"
            r5_d = f"{op5['tp']}/{op5['n_pos']}"
            f5_d = f"{op5['fp']}/{op5['n_neg']}"
            ab_d = f"{cov['n_abstain']}/{cov['n_total']}"
            lines.append(
                f"| `{sname}` | `{sp}` | "
                f"{fmt_val_ci(au['value'], au['ci95'], au_d)} | "
                f"{fmt_val_ci(op1['recall'], op1['recall_ci95'], r1_d)} | "
                f"{fmt_val_ci(op1['fpr'], op1['fpr_ci95'], f1_d)} | "
                f"{fmt_val_ci(op5['recall'], op5['recall_ci95'], r5_d)} | "
                f"{fmt_val_ci(op5['fpr'], op5['fpr_ci95'], f5_d)} | "
                f"{prec_str} | "
                f"{fmt_val_ci(cov['abstain_rate'], cov['abstain_rate_ci95'], ab_d)} |"
            )

    # Also add OR combinations to the pooled split table
    for cname, cdata in report_data.get("or_combinations", {}).items():
        for sp in ("dev", "heldout"):
            if sp not in cdata["by_split"]:
                continue
            m = cdata["by_split"][sp]
            op1 = m["at_dev_1pct_fpr"]
            op5 = m["at_dev_5pct_fpr"]
            cov = m["coverage"]
            p5 = op5["precision_at_base_rates_floor_nominal_fpr"]
            prec_str = (
                f"{p5['1pct']:.3f} / {p5['5pct']:.3f} / {p5['20pct']:.3f}"
                if p5["1pct"] is not None
                else "N/A"
            )
            r1_d = f"{op1['tp']}/{op1['n_pos']}"
            f1_d = f"{op1['fp']}/{op1['n_neg']}"
            r5_d = f"{op5['tp']}/{op5['n_pos']}"
            f5_d = f"{op5['fp']}/{op5['n_neg']}"
            ab_d = f"{cov['n_abstain']}/{cov['n_total']}"
            lines.append(
                f"| `{cname}` | `{sp}` | "
                f"N/A (binary rule) | "
                f"{fmt_val_ci(op1['recall'], op1['recall_ci95'], r1_d)} | "
                f"{fmt_val_ci(op1['fpr'], op1['fpr_ci95'], f1_d)} | "
                f"{fmt_val_ci(op5['recall'], op5['recall_ci95'], r5_d)} | "
                f"{fmt_val_ci(op5['fpr'], op5['fpr_ci95'], f5_d)} | "
                f"{prec_str} | "
                f"{fmt_val_ci(cov['abstain_rate'], cov['abstain_rate_ci95'], ab_d)} |"
            )
    lines.append("")

    lines.append("## 3. Per-Set Breakdown (All Sets)")
    lines.append("")
    lines.append(
        "| Signal | Set | Split | AUROC [95% CI] | Recall @ 1% DEV FPR [95% CI] | FPR @ 1% DEV Thr [95% CI] | Recall @ 5% DEV FPR [95% CI] | FPR @ 5% DEV Thr [95% CI] | Abstain |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for sname, sdata in signals_dict.items():
        for set_id, m in sdata["per_set"].items():
            au = m["auroc"]
            op1 = m["at_dev_1pct_fpr"]
            op5 = m["at_dev_5pct_fpr"]
            cov = m["coverage"]
            au_d = f"{au['n_pos']}+/{au['n_neg']}-"
            r1_d = f"{op1['tp']}/{op1['n_pos']}"
            f1_d = f"{op1['fp']}/{op1['n_neg']}"
            r5_d = f"{op5['tp']}/{op5['n_pos']}"
            f5_d = f"{op5['fp']}/{op5['n_neg']}"
            lines.append(
                f"| `{sname}` | `{set_id}` | `{m['split']}` | "
                f"{fmt_val_ci(au['value'], au['ci95'], au_d)} | "
                f"{fmt_val_ci(op1['recall'], op1['recall_ci95'], r1_d)} | "
                f"{fmt_val_ci(op1['fpr'], op1['fpr_ci95'], f1_d)} | "
                f"{fmt_val_ci(op5['recall'], op5['recall_ci95'], r5_d)} | "
                f"{fmt_val_ci(op5['fpr'], op5['fpr_ci95'], f5_d)} | "
                f"{cov['n_abstain']}/{cov['n_total']} |"
            )
    for cname, cdata in report_data.get("or_combinations", {}).items():
        for set_id, m in cdata["per_set"].items():
            op1 = m["at_dev_1pct_fpr"]
            op5 = m["at_dev_5pct_fpr"]
            cov = m["coverage"]
            r1_d = f"{op1['tp']}/{op1['n_pos']}"
            f1_d = f"{op1['fp']}/{op1['n_neg']}"
            r5_d = f"{op5['tp']}/{op5['n_pos']}"
            f5_d = f"{op5['fp']}/{op5['n_neg']}"
            lines.append(
                f"| `{cname}` | `{set_id}` | `{m['split']}` | "
                f"N/A (OR rule) | "
                f"{fmt_val_ci(op1['recall'], op1['recall_ci95'], r1_d)} | "
                f"{fmt_val_ci(op1['fpr'], op1['fpr_ci95'], f1_d)} | "
                f"{fmt_val_ci(op5['recall'], op5['recall_ci95'], r5_d)} | "
                f"{fmt_val_ci(op5['fpr'], op5['fpr_ci95'], f5_d)} | "
                f"{cov['n_abstain']}/{cov['n_total']} |"
            )
    lines.append("")

    lines.append("## 4. Natural-Only Breakdown (`natural == true`)")
    lines.append("")
    lines.append(
        "| Signal | Split | AUROC (pos+/neg-) [95% CI] | Recall @ 1% DEV FPR [95% CI] | FPR @ 1% DEV Thr [95% CI] | Recall @ 5% DEV FPR [95% CI] | FPR @ 5% DEV Thr [95% CI] |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for sname, sdata in signals_dict.items():
        for sp in ("dev", "heldout"):
            if sp not in sdata["by_split_natural_only"]:
                continue
            m = sdata["by_split_natural_only"][sp]
            au = m["auroc"]
            op1 = m["at_dev_1pct_fpr"]
            op5 = m["at_dev_5pct_fpr"]
            au_d = f"{au['n_pos']}+/{au['n_neg']}-"
            r1_d = f"{op1['tp']}/{op1['n_pos']}"
            f1_d = f"{op1['fp']}/{op1['n_neg']}"
            r5_d = f"{op5['tp']}/{op5['n_pos']}"
            f5_d = f"{op5['fp']}/{op5['n_neg']}"
            lines.append(
                f"| `{sname}` | `{sp}` | "
                f"{fmt_val_ci(au['value'], au['ci95'], au_d)} | "
                f"{fmt_val_ci(op1['recall'], op1['recall_ci95'], r1_d)} | "
                f"{fmt_val_ci(op1['fpr'], op1['fpr_ci95'], f1_d)} | "
                f"{fmt_val_ci(op5['recall'], op5['recall_ci95'], r5_d)} | "
                f"{fmt_val_ci(op5['fpr'], op5['fpr_ci95'], f5_d)} |"
            )
    for cname, cdata in report_data.get("or_combinations", {}).items():
        for sp in ("dev", "heldout"):
            if sp not in cdata["by_split_natural_only"]:
                continue
            m = cdata["by_split_natural_only"][sp]
            op1 = m["at_dev_1pct_fpr"]
            op5 = m["at_dev_5pct_fpr"]
            r1_d = f"{op1['tp']}/{op1['n_pos']}"
            f1_d = f"{op1['fp']}/{op1['n_neg']}"
            r5_d = f"{op5['tp']}/{op5['n_pos']}"
            f5_d = f"{op5['fp']}/{op5['n_neg']}"
            lines.append(
                f"| `{cname}` | `{sp}` | "
                f"N/A (OR rule) | "
                f"{fmt_val_ci(op1['recall'], op1['recall_ci95'], r1_d)} | "
                f"{fmt_val_ci(op1['fpr'], op1['fpr_ci95'], f1_d)} | "
                f"{fmt_val_ci(op5['recall'], op5['recall_ci95'], r5_d)} | "
                f"{fmt_val_ci(op5['fpr'], op5['fpr_ci95'], f5_d)} |"
            )
    lines.append("")

    if report_data.get("external_detbench_scorer"):
        lines.append("## 5. External DetBench Scorer Output (Verbatim)")
        lines.append("")
        lines.append("```")
        lines.append(str(report_data["external_detbench_scorer"]["output"]).rstrip())
        lines.append("```")
        lines.append("")

    return "\n".join(lines) + "\n"


def maybe_run_external_detbench_scorer(signal_paths: Sequence[Path]) -> dict[str, Any] | None:
    candidates = [
        Path(os.path.expanduser("~/Developer/eval-lab/.worktrees/ov1104-probes/research/experiments/ov1104-detbench/score.py")),
        Path("/private/tmp/ov1104/shared/detbench/score.py"),
    ]
    for scorer_path in candidates:
        if scorer_path.exists():
            cmd = [sys.executable, str(scorer_path)]
            for sp in signal_paths:
                cmd.extend(["--signal", str(sp)])
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                return {
                    "scorer_path": str(scorer_path),
                    "returncode": proc.returncode,
                    "output": (proc.stdout or "") + (proc.stderr or ""),
                }
            except Exception as exc:
                return {
                    "scorer_path": str(scorer_path),
                    "returncode": -1,
                    "output": f"Error invoking external scorer: {exc}",
                }
    return None


def cmd_report(args: argparse.Namespace) -> int:
    assert args.thresholds_from == "dev", (
        f"Thresholds must be derived strictly from 'dev' negatives (got '{args.thresholds_from}')."
    )
    runs = load_runs(Path(args.runs))
    signal_Maps: dict[str, dict[str, dict[str, Any]]] = {}
    signal_order: list[str] = []
    signal_paths: list[Path] = []

    for sp_str in args.signals:
        for part in sp_str.split(","):
            part = part.strip()
            if not part:
                continue
            sp = Path(part)
            signal_paths.append(sp)
            srows = load_signals_file(sp)
            if not srows:
                continue
            sname = str(srows[0]["signal"])
            if sname not in signal_Maps:
                signal_Maps[sname] = {}
                signal_order.append(sname)
            for r in srows:
                signal_Maps[sname][r["run_key"]] = r

    # Length baselines from runs.jsonl (`n_chars`, `n_steps`)
    if args.length_from == "runs":
        for bname in ("n_chars", "n_steps"):
            bmap: dict[str, dict[str, Any]] = {}
            for r in runs:
                rk = r["run_key"]
                val = r.get(bname)
                bmap[rk] = {
                    "run_key": rk,
                    "signal": bname,
                    "version": "runs_baseline_v1",
                    "score": float(val) if val is not None else None,
                    "abstain": "missing_length" if val is None else None,
                    "evidence_steps": [],
                    "model": None,
                    "cost_usd": 0.0,
                }
            signal_Maps[bname] = bmap
            if bname not in signal_order:
                signal_order.append(bname)

    # Determine pairs to combine
    combine_pairs: list[tuple[str, str]] = []
    if args.combine:
        parts = [p.strip() for p in args.combine.split(",") if p.strip()]
        if len(parts) >= 2:
            combine_pairs.append((parts[0], parts[1]))
    elif len([s for s in signal_order if s not in ("n_chars", "n_steps")]) >= 2:
        non_base = [s for s in signal_order if s not in ("n_chars", "n_steps")]
        combine_pairs.append((non_base[0], non_base[1]))

    # Build max_rank combination signals first so they get full signal evaluation (AUROC, CIs, thresholds)
    for sig_a, sig_b in combine_pairs:
        if sig_a in signal_Maps and sig_b in signal_Maps:
            max_name = f"combine_max_rank({sig_a},{sig_b})"
            signal_Maps[max_name] = build_max_rank_signal_map(
                runs, signal_Maps[sig_a], signal_Maps[sig_b], max_name
            )
            if max_name not in signal_order:
                signal_order.append(max_name)

    evaluated_signals: dict[str, Any] = {}
    for sname in signal_order:
        evaluated_signals[sname] = evaluate_full_signal(sname, runs, signal_Maps[sname])

    # Evaluate OR-cascade combinations using each signal's own DEV 1% and 5% FPR threshold
    or_combinations: dict[str, Any] = {}
    sets_in_order: list[str] = []
    for r in runs:
        if r["set"] not in sets_in_order:
            sets_in_order.append(r["set"])

    for sig_a, sig_b in combine_pairs:
        if sig_a not in evaluated_signals or sig_b not in evaluated_signals:
            continue
        thr_a_1 = evaluated_signals[sig_a]["thresholds"]["fpr_1pct"]
        thr_b_1 = evaluated_signals[sig_b]["thresholds"]["fpr_1pct"]
        thr_a_5 = evaluated_signals[sig_a]["thresholds"]["fpr_5pct"]
        thr_b_5 = evaluated_signals[sig_b]["thresholds"]["fpr_5pct"]

        or_name = f"combine_or({sig_a},{sig_b})"
        per_set_or: dict[str, Any] = {}
        per_set_or_nat: dict[str, Any] = {}
        for s in sets_in_order:
            s_rows = [r for r in runs if r["set"] == s]
            s_nat = [r for r in s_rows if r.get("natural") is True]
            sp_name = s_rows[0]["split"] if s_rows else "unknown"
            m_all = evaluate_or_combination_subset(
                s_rows, signal_Maps[sig_a], signal_Maps[sig_b], thr_a_1, thr_b_1, thr_a_5, thr_b_5
            )
            m_all["split"] = sp_name
            per_set_or[s] = m_all

            m_nat = evaluate_or_combination_subset(
                s_nat, signal_Maps[sig_a], signal_Maps[sig_b], thr_a_1, thr_b_1, thr_a_5, thr_b_5
            )
            m_nat["split"] = sp_name
            per_set_or_nat[s] = m_nat

        by_split_or: dict[str, Any] = {}
        by_split_or_nat: dict[str, Any] = {}
        for sp in ("dev", "heldout"):
            sp_rows = [r for r in runs if r.get("split") == sp]
            if not sp_rows:
                continue
            sp_nat = [r for r in sp_rows if r.get("natural") is True]
            by_split_or[sp] = evaluate_or_combination_subset(
                sp_rows, signal_Maps[sig_a], signal_Maps[sig_b], thr_a_1, thr_b_1, thr_a_5, thr_b_5
            )
            by_split_or_nat[sp] = evaluate_or_combination_subset(
                sp_nat, signal_Maps[sig_a], signal_Maps[sig_b], thr_a_1, thr_b_1, thr_a_5, thr_b_5
            )

        or_combinations[or_name] = {
            "signals": [sig_a, sig_b],
            "method": "or",
            "thresholds": {
                sig_a: {"fpr_1pct": thr_a_1, "fpr_5pct": thr_a_5},
                sig_b: {"fpr_1pct": thr_b_1, "fpr_5pct": thr_b_5},
            },
            "by_split": by_split_or,
            "by_split_natural_only": by_split_or_nat,
            "per_set": per_set_or,
            "per_set_natural_only": per_set_or_nat,
        }

    ext_scorer = maybe_run_external_detbench_scorer(signal_paths)

    # Check if sibling loso.json exists in same output folder to include LOSO summary in report.md
    out_json_path = Path(args.out)
    loso_summary = None
    loso_candidate = Path(args.loso) if getattr(args, "loso", None) else (out_json_path.parent / "loso.json")
    if loso_candidate and loso_candidate.exists():
        try:
            with open(loso_candidate, encoding="utf-8") as f:
                loso_summary = json.load(f)
        except Exception:
            loso_summary = None

    report_payload: dict[str, Any] = {
        "seed": SEED,
        "n_bootstrap": N_BOOTSTRAP,
        "thresholds_from": args.thresholds_from,
        "length_from": args.length_from,
        "n_runs": len(runs),
        "n_dev_runs": sum(1 for r in runs if r.get("split") == "dev"),
        "n_heldout_runs": sum(1 for r in runs if r.get("split") == "heldout"),
        "signals": evaluated_signals,
        "or_combinations": or_combinations,
        "external_detbench_scorer": ext_scorer,
    }
    if loso_summary is not None:
        report_payload["loso_summary"] = {
            "best_config": loso_summary.get("best_config"),
            "best_mean_auroc": loso_summary.get("best_mean_auroc"),
            "length_baselines": {
                k: v.get("mean_auroc") for k, v in loso_summary.get("length_baselines", {}).items()
            },
        }

    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(report_payload, f, indent=2)

    if args.md:
        md_path = Path(args.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_text = render_report_markdown(report_payload, loso_summary=loso_summary)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe training, freezing, scoring, and reporting CLI.")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_loso = sub.add_parser("loso", help="Leave-one-source-out evaluation over DEV sets.")
    p_loso.add_argument("--runs", required=True, help="Path to DetBench runs.jsonl")
    p_loso.add_argument("--vectors", required=True, help="Directory containing <set>.npz vector files")
    p_loso.add_argument("--split", default="dev", help="Split to evaluate on (must be 'dev')")
    p_loso.add_argument("--grid", default="default", help="Pre-registered grid name ('default')")
    p_loso.add_argument("--out", required=True, help="Output JSON path (loso.json)")
    p_loso.set_defaults(func=cmd_loso)

    p_fit = sub.add_parser("fit", help="Fit a config on all DEV sets and save frozen probe.npz.")
    p_fit.add_argument("--runs", required=True, help="Path to DetBench runs.jsonl")
    p_fit.add_argument("--vectors", required=True, help="Directory containing <set>.npz vector files")
    p_fit.add_argument("--split", default="dev", help="Split to train on (must be 'dev')")
    p_fit.add_argument("--config", required=True, help="Grid config name (e.g. run_mean_L18__c0.1)")
    p_fit.add_argument("--out", required=True, help="Output .npz path (probe.npz)")
    p_fit.set_defaults(func=cmd_fit)

    p_score = sub.add_parser("score", help="Score runs with frozen probe.npz and emit DetBench signals.jsonl.")
    p_score.add_argument("--probe", required=True, help="Path to frozen probe.npz")
    p_score.add_argument("--runs", required=True, help="Path to DetBench runs.jsonl")
    p_score.add_argument("--vectors", required=True, help="Directory containing <set>.npz vector files")
    p_score.add_argument("--sets", nargs="+", required=True, help="List of set names or 'all'")
    p_score.add_argument("--signal-name", required=True, help="Signal name for DetBench output rows")
    p_score.add_argument("--out", required=True, help="Output .jsonl path")
    p_score.set_defaults(func=cmd_score)

    p_rep = sub.add_parser("report", help="Generate scoring report with CIs, baselines, and combinations.")
    p_rep.add_argument("--runs", required=True, help="Path to DetBench runs.jsonl")
    p_rep.add_argument("--signals", nargs="+", required=True, help="One or more signal .jsonl files")
    p_rep.add_argument("--length-from", default="runs", choices=["runs", "none"], help="Include length baselines")
    p_rep.add_argument("--thresholds-from", default="dev", help="Split to derive FPR thresholds from (must be 'dev')")
    p_rep.add_argument("--combine", default=None, help="Comma-separated signal names to combine (e.g. probe,judge)")
    p_rep.add_argument("--method", default="or", choices=["or", "max", "both"], help="Combination method")
    p_rep.add_argument("--loso", default=None, help="Optional path to loso.json to embed in report")
    p_rep.add_argument("--out", required=True, help="Output report.json path")
    p_rep.add_argument("--md", required=True, help="Output report.md path")
    p_rep.set_defaults(func=cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
