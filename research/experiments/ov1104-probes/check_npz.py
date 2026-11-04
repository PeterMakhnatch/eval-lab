"""Validate an extraction npz against CONTRACT invariants; print JSON summary (counts only)."""
import json
import sys

import numpy as np

z = np.load(sys.argv[1])
meta = json.loads(str(z["meta"]))
N = len(z["run_keys"])
out = {"file": sys.argv[1], "N": N, "meta": meta, "shapes": {k: list(z[k].shape) for k in z.files if k != "meta"},
       "dtypes": {k: str(z[k].dtype) for k in z.files if k != "meta"}}
fails = []
exp = {"run_mean": (N, 3, 4096), "run_max": (N, 3, 4096), "win_mean": (N, 12, 3, 4096), "win_max": (N, 12, 3, 4096),
       "n_windows": (N,), "win_steps": (N, 12, 2), "n_tokens": (N,)}
for k, s in exp.items():
    if z[k].shape != s:
        fails.append(f"shape {k} {z[k].shape} != {s}")
for k in ("run_mean", "run_max", "win_mean", "win_max"):
    if z[k].dtype != np.float16:
        fails.append(f"dtype {k} {z[k].dtype}")
    if not np.isfinite(z[k].astype(np.float32)).all():
        fails.append(f"non-finite in {k}")
nw, nt, ws, wt = z["n_windows"], z["n_tokens"], z["win_steps"], z["win_tokens"]
max_rel = 0.0
for j in range(N):
    n_total = max(1, -(-int(nt[j]) // 4096))
    if int(nw[j]) != min(12, n_total):
        fails.append(f"run{j}: n_windows {nw[j]} vs tokens {nt[j]}")
    if int(wt[j, :nw[j]].sum()) != min(int(nt[j]), 12 * 4096) and n_total <= 12:
        fails.append(f"run{j}: win_tokens sum {wt[j].sum()} != n_tokens {nt[j]}")
    s = ws[j, :nw[j]]
    if (s < 0).any() or (ws[j, nw[j]:] != -1).any():
        fails.append(f"run{j}: win_steps padding")
    if not (np.diff(s[:, 0]) >= 0).all() or not (np.diff(s[:, 1]) >= 0).all() or (s[:, 1] < s[:, 0]).any():
        fails.append(f"run{j}: win_steps not monotone")
    if (np.abs(z["win_mean"][j, nw[j]:]).sum() != 0) or (np.abs(z["win_max"][j, nw[j]:]).sum() != 0):
        fails.append(f"run{j}: window padding not zero")
    w = wt[j, :nw[j]].astype(np.float64) / wt[j, :nw[j]].sum()
    rm = (z["win_mean"][j, :nw[j]].astype(np.float64) * w[:, None, None]).sum(0)
    got = z["run_mean"][j].astype(np.float64)
    rel = np.abs(rm - got).max() / (np.abs(rm).max() + 1e-6)
    max_rel = max(max_rel, rel)
    if rel > 2e-3:  # fp16 has ~1e-3 relative precision
        fails.append(f"run{j}: run_mean != weighted win_mean (rel {rel:.2e})")
    if not np.array_equal(z["run_max"][j], z["win_max"][j, :nw[j]].max(0)):
        fails.append(f"run{j}: run_max != max of win_max")
out["run_mean_max_rel_err"] = max_rel
out["n_windows_hist"] = {str(k): int((nw == k).sum()) for k in sorted(set(nw.tolist()))}
out["n_tokens"] = nt.tolist()
out["fails"] = fails
out["ok"] = not fails
print(json.dumps(out, indent=1))
sys.exit(0 if not fails else 1)
