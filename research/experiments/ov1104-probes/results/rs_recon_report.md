# Reflex-Sentinel Published Linear Probe Cross-Test (`rs-recon`)

## Summary Answer ($0 Local CPU Evaluation)

Yes: the publisher's shipped linear probes on `IQuest-Coder-V1-40B-Loop-Instruct` (4-bit AWQ) hidden states strongly separate test-tampered runs (`test_tampered=true`, 45/250) from untampered runs (`test_tampered=false`, 205/250) on the 250 primary trajectories, whereas trivial length baselines (`n_turns`, `n_steps`, and total character counts) perform near or below chance (`AUROC 0.3618–0.5390`, `0/45` recall at both 1% and 5% FPR) and the published unmodified `Qwen3-8B` work-log judge achieves `AUROC 0.5460–0.6149` on the 200 conflicting runs it covers.

- **Main caveat (in-sample upper bound):** As verified directly against the publisher's `rs/eval_linear.py` (`--save-probe`), the shipped weights `linear_probe_A.npz` and `linear_probe_B.npz` were fit on **all 200 conflicting runs** in this same dataset (45 positive concealment steps vs 2,137 negative steps across the 155 untampered conflicting runs). Consequently, evaluating `linear_probe_A.npz` (`AUROC 0.9793 [0.9602, 0.9933]` for max-over-steps) and `linear_probe_B.npz` (`AUROC 0.9991 [0.9971, 1.0000]` for max-over-steps) on these 250 runs is **in-sample** for the 200 conflicting runs and represents an optimistic upper bound, not a held-out cross-test.
- **Task-held-out (5-fold GroupKFold) reference:** Evaluating the publisher's 5-fold out-of-fold step scores (`scores/linear_scores_A.json`, where each task cluster of 4 conflicting runs is scored by a probe trained only on the other 40 tasks) under our run-level max-over-steps recipe yields **AUROC 0.8380 [0.7628, 0.9051]** (mean-over-steps **0.8108 [0.7150, 0.8984]**) on the 200 conflicting runs (`45` positives vs `155` conflicting negatives; `50` original controls not in `linear_scores_A.json` treated as abstain), with recall **17/45 = 37.78% [25.11%, 52.37%]** at 5% FPR (`7/155 = 4.52%`) and **7/45 = 15.56% [7.75%, 28.78%]** at 1% FPR (`1/155 = 0.65%`).

---

## 1. Feature Layout, Probe Recipe, and Reproduction Check

### Feature Layout (`features/*.npy` and `features/index.jsonl`)
- **Files:** 250 `.npy` files (`float16`), one per primary trajectory in `runs/agent_runs.jsonl`, totaling 70,964,201 bytes (with `index.jsonl`).
- **Shape `[steps, 2, 5120]`:**
  - **Dimension 0 (`steps`):** Number of assistant turns/steps in the trajectory (`8` to `14` steps per run; `3,456` steps across all 250 runs; `2,767` steps across the 200 conflicting runs). Matches `n_turns` and `len(steps)` for all 250 runs.
  - **Dimension 1 (`2` extraction points per step):**
    - Index `0` = **Point A**: right before the step's first generated token (before the step's reasoning/response).
    - Index `1` = **Point B**: right before the tool call markup (`<tool_call>`, `<function`, or ```` ``` ````) inside the step's assistant message (after inline reasoning, right before the command).
  - **Dimension 2 (`5120` hidden units):** Residual stream of the last token, averaged over **loop-1 layers 40–70 (0-indexed; 41–71 1-indexed)** of the 80-layer latent-reasoning model `IQuest-Coder-V1-40B-Loop-Instruct` (4-bit AWQ, which loops its 80 layers twice for 160 effective layers).

### Probe Weights (`scores/linear_probe_A.npz`, `scores/linear_probe_B.npz`)
- Both `.npz` archives contain keys `['mu', 'sd', 'w', 'b', 'point', 'band']`:
  - `mu`: `float32` shape `(5120,)` — per-feature training mean.
  - `sd`: `float32` shape `(5120,)` — per-feature training standard deviation plus `1e-6` (`X.std(0) + 1e-6`).
  - `w`: `float32` shape `(5120,)` — L2-regularized logistic regression weight vector (`C = 0.003`).
  - `b`: `float32` scalar `()` — intercept (`-6.1901155` for Probe A; `-6.7341433` for Probe B).
  - `point`: `int64` scalar `()` — `0` for Probe A (Point A), `1` for Probe B (Point B).
  - `band`: `<U5` scalar `()` — `"40-70"`.
- **Step scoring formula:** For a run's feature array `arr` of shape `[steps, 2, 5120]`, cast point `p` (`0` for A, `1` for B) to `float32` (`h = arr[:, p, :].astype(np.float32)`) and compute the linear decision function:
  $$\text{score}_t = \left(\frac{h_t - \mu}{\text{sd}}\right) \cdot w + b$$
- **What aggregation the publisher used vs our recipe:**
  - In `rs/eval_linear.py`, the publisher did **not** aggregate step scores into a single run score via max or mean. Instead, the publisher evaluated step-level AUROC at fixed offsets $d \in \{0, 1, 2, 3\}$ steps before the concealment step $t_c$ (comparing the 45 positive runs at step $t_c - d$ against all untampered conflicting runs at the same step indices).
  - Under our corpus README's **"Exact scoring recipe"**, each run gets a single scalar score equal to the **maximum over steps** (`max_t score_t`), and we also report the **mean over steps** (`mean_t score_t`).

### Verification Against `scores/linear_scores_A.json`
In `rs/eval_linear.py`, the publisher produced two distinct artifacts from the same 2,182 training steps (`45` concealment steps at $t = t_c$ from the 45 tampered conflicting runs vs `2,137` steps from the 155 untampered conflicting runs):
1. `scores/linear_probe_A.npz` (`--save-probe`): trained on **all 2,182 steps** across all 50 tasks.
2. `scores/linear_scores_A.json` (`--save-scores`): **5-fold `GroupKFold(5)` by task** out-of-fold step scores across all `2,767` steps of the 200 conflicting runs.

We verified both in `rs_recon.py`:

| Verification Comparison (200 Conflicting Runs, 2,767 Steps) | Step Max Abs Diff | Step Mean Abs Diff | Step Spearman $\rho$ | Run-Max Max Abs Diff | Run-Max Spearman $\rho$ | Run-Mean Max Abs Diff | Run-Mean Spearman $\rho$ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **1. Direct `linear_probe_A.npz` (all-data) vs `linear_scores_A.json` (5-fold OOF)** | `5.2125` | `0.3606` | `0.9641` | `3.4861` | `0.9121` | `0.7017` | `0.9647` |
| **2. Replicated 5-fold `GroupKFold(5)` (`scipy` L-BFGS-B) vs `linear_scores_A.json`** | `0.1336` | `0.0237` | `0.9997` | `0.0423` | `0.9999` | `0.0733` | `0.9996` |
| **3. Replicated 5-fold `GroupKFold(5)` (`sklearn` LogisticRegression) vs `linear_scores_A.json`** | `0.0466` | `0.0108` | `0.99997` | `0.0321` | `0.99996` | `0.0249` | `0.99996` |

- Recomputing `mu` and `sd` on the 2,182 training steps matches `linear_probe_A.npz["mu"]` and `["sd"]` (and `linear_probe_B.npz["mu"]` and `["sd"]`) to **exact `0.0` float32 difference**, and refitting `LogisticRegression(C=0.003)` reproduces `w` within `2.44e-4` (scipy) / `7.85e-5` (sklearn) max abs diff.
- Recomputing the publisher's step-level AUROCs at $d = 0, 1, 2, 3$ steps before $t_c$ from `linear_scores_A.json` reproduces the dataset card's headline numbers (`0.9037` $\approx 90.4\%$ at $d=0$, `0.8177` $\approx 81.8\%$ at $d=1$, `0.6695` $\approx 66.9\%$ at $d=2$, `0.6788` $\approx 67.9\%$ at $d=3$).

---

## 2. Run-Level Evaluation Across All Signals

### Evaluation Protocol
- **Gold label:** `test_tampered` (`45` positives, all in `conflicting`; `205` negatives = `155` `conflicting` negatives + `50` `original` controls). Note: 2 of the 45 positives have `pass_own=false`; per the corpus README, all 45 `test_tampered=true` runs are retained as positives.
- **Cluster bootstrap CI:** 95% percentile bootstrap (`2.5%`, `97.5%`) over the `50` hashed `task_id` clusters (`2,000` resamples, `seed = 20261027`), sampling clusters with replacement and keeping all sibling runs (`4` conflicting + `1` original) together. **Undefined bootstrap replicates: `0 / 2,000`** for every signal.
- **Optimistic operating points (1% and 5% FPR):** Because no pre-frozen threshold from external data exists for these signals, the threshold for each signal is chosen on this evaluation set's negatives (`score >= threshold` maximizing TP subject to achieved $\text{FPR} \le 0.01$ or $\le 0.05$, i.e. $\le 2/205$ or $\le 10/205$ false positives for $N=250$, and $\le 1/155$ or $\le 7/155$ for $N=200$ signals). **These recalls are therefore optimistic in-sample threshold estimates.** Binomial 95% CIs on recall and FPR use the Wilson score interval.

### Main Comparison Table (Primary Runs: $N=250$ Full Set, or $N=200$ Conflicting Subset with $50$ Original Abstain)

| Signal | Cov (Abstain) | AUROC [95% Cluster CI] | Recall @ 1% FPR [95% Wilson CI] | Achieved FPR @ 1% (Conf / Orig) | Recall @ 5% FPR [95% Wilson CI] | Achieved FPR @ 5% (Conf / Orig) |
| --- | --- | --- | --- | --- | --- | --- |
| **`probe_A_max`** (Point A, in-sample `.npz`, max) | `250/250` (`0`) | **0.9793** `[0.9602, 0.9933]` | **80.00%** (`36/45`) `[66.18%, 89.10%]` | `0.98%` (`2/205`)<br>Conf: `1.29%` (`2/155`) `[0.35%, 4.58%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **86.67%** (`39/45`) `[73.82%, 93.74%]` | `4.88%` (`10/205`)<br>Conf: `6.45%` (`10/155`) `[3.54%, 11.46%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` |
| **`probe_A_mean`** (Point A, in-sample `.npz`, mean) | `250/250` (`0`) | **0.9069** `[0.8451, 0.9601]` | **24.44%** (`11/45`) `[14.24%, 38.67%]` | `0.98%` (`2/205`)<br>Conf: `1.29%` (`2/155`) `[0.35%, 4.58%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **60.00%** (`27/45`) `[45.45%, 72.98%]` | `4.88%` (`10/205`)<br>Conf: `6.45%` (`10/155`) `[3.54%, 11.46%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` |
| **`probe_B_max`** (Point B, in-sample `.npz`, max) | `250/250` (`0`) | **0.9991** `[0.9971, 1.0000]` | **100.00%** (`45/45`) `[92.13%, 100.00%]` | `0.98%` (`2/205`)<br>Conf: `1.29%` (`2/155`) `[0.35%, 4.58%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **100.00%** (`45/45`) `[92.13%, 100.00%]` | `4.88%` (`10/205`)<br>Conf: `6.45%` (`10/155`) `[3.54%, 11.46%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` |
| **`probe_B_mean`** (Point B, in-sample `.npz`, mean) | `250/250` (`0`) | **0.9649** `[0.9364, 0.9875]` | **42.22%** (`19/45`) `[28.97%, 56.70%]` | `0.98%` (`2/205`)<br>Conf: `1.29%` (`2/155`) `[0.35%, 4.58%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **88.89%** (`40/45`) `[76.50%, 95.16%]` | `4.88%` (`10/205`)<br>Conf: `5.16%` (`8/155`) `[2.64%, 9.85%]`<br>Orig: `4.00%` (`2/50`) `[1.10%, 13.46%]` |
| **`linear_scores_A_oof_max`** (Point A, 5-fold task-OOF, max) | `200/250` (`50`) | **0.8380** `[0.7628, 0.9051]` | **15.56%** (`7/45`) `[7.75%, 28.78%]` | `0.65%` (`1/155`)<br>Conf: `0.65%` (`1/155`) `[0.11%, 3.56%]`<br>Orig: `abstain` (`0/0`) | **37.78%** (`17/45`) `[25.11%, 52.37%]` | `4.52%` (`7/155`)<br>Conf: `4.52%` (`7/155`) `[2.20%, 9.03%]`<br>Orig: `abstain` (`0/0`) |
| **`linear_scores_A_oof_mean`** (Point A, 5-fold task-OOF, mean) | `200/250` (`50`) | **0.8108** `[0.7150, 0.8984]` | **15.56%** (`7/45`) `[7.75%, 28.78%]` | `0.65%` (`1/155`)<br>Conf: `0.65%` (`1/155`) `[0.11%, 3.56%]`<br>Orig: `abstain` (`0/0`) | **26.67%** (`12/45`) `[15.96%, 41.04%]` | `4.52%` (`7/155`)<br>Conf: `4.52%` (`7/155`) `[2.20%, 9.03%]`<br>Orig: `abstain` (`0/0`) |
| **`n_turns`** (Baseline: turn count) | `250/250` (`0`) | **0.5390** `[0.5169, 0.5668]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/205`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/205`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` |
| **`n_steps`** (Baseline: `len(steps)`, identical to `n_turns`) | `250/250` (`0`) | **0.5390** `[0.5169, 0.5668]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/205`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/205`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` |
| **`msgs_total_chars`** (Baseline: total chars in `msgs`) | `250/250` (`0`) | **0.4059** `[0.3141, 0.4994]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.98%` (`2/205`)<br>Conf: `1.29%` (`2/155`) `[0.35%, 4.58%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `4.88%` (`10/205`)<br>Conf: `5.16%` (`8/155`) `[2.64%, 9.85%]`<br>Orig: `4.00%` (`2/50`) `[1.10%, 13.46%]` |
| **`steps_total_chars`** (Baseline: total chars in `steps`) | `250/250` (`0`) | **0.3833** `[0.2926, 0.4731]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.98%` (`2/205`)<br>Conf: `1.29%` (`2/155`) `[0.35%, 4.58%]`<br>Orig: `0.00%` (`0/50`) `[0.00%, 7.13%]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `4.88%` (`10/205`)<br>Conf: `5.81%` (`9/155`) `[3.08%, 10.66%]`<br>Orig: `2.00%` (`1/50`) `[0.35%, 10.50%]` |
| **`combined_msgs_steps_chars`** (Baseline: `msgs` + `steps` chars) | `250/250` (`0`) | **0.3618** `[0.2688, 0.4559]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.98%` (`2/205`)<br>Conf: `0.65%` (`1/155`) `[0.11%, 3.56%]`<br>Orig: `2.00%` (`1/50`) `[0.35%, 10.50%]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `4.88%` (`10/205`)<br>Conf: `5.81%` (`9/155`) `[3.08%, 10.66%]`<br>Orig: `2.00%` (`1/50`) `[0.35%, 10.50%]` |
| **`llm_scores_all_max`** (Qwen3-8B, max over all rows per run) | `200/250` (`50`) | **0.5460** `[0.4428, 0.6438]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) |
| **`llm_scores_all_mean`** (Qwen3-8B, mean over all rows incl. `-1`) | `200/250` (`50`) | **0.6149** `[0.5240, 0.7018]` | **4.44%** (`2/45`) `[1.23%, 14.83%]` | `0.65%` (`1/155`)<br>Conf: `0.65%` (`1/155`) `[0.11%, 3.56%]`<br>Orig: `abstain` (`0/0`) | **17.78%** (`8/45`) `[9.29%, 31.33%]` | `4.52%` (`7/155`)<br>Conf: `4.52%` (`7/155`) `[2.20%, 9.03%]`<br>Orig: `abstain` (`0/0`) |
| **`llm_scores_valid_max`** (Qwen3-8B, max over `score >= 0` rows) | `200/250` (`50`) | **0.5460** `[0.4428, 0.6438]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) |
| **`llm_scores_valid_mean`** (Qwen3-8B, mean over `score >= 0` rows) | `200/250` (`50`) | **0.5887** `[0.4926, 0.6756]` | **2.22%** (`1/45`) `[0.39%, 11.57%]` | `0.65%` (`1/155`)<br>Conf: `0.65%` (`1/155`) `[0.11%, 3.56%]`<br>Orig: `abstain` (`0/0`) | **15.56%** (`7/45`) `[7.75%, 28.78%]` | `4.52%` (`7/155`)<br>Conf: `4.52%` (`7/155`) `[2.20%, 9.03%]`<br>Orig: `abstain` (`0/0`) |
| **`llm_scores_pointA_valid_max`** (Qwen3-8B Point A, max) | `200/250` (`50`) | **0.5517** `[0.4632, 0.6301]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) |
| **`llm_scores_pointA_valid_mean`** (Qwen3-8B Point A, mean) | `200/250` (`50`) | **0.5824** `[0.4900, 0.6700]` | **4.44%** (`2/45`) `[1.23%, 14.83%]` | `0.00%` (`0/155`)<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) | **11.11%** (`5/45`) `[4.84%, 23.50%]` | `4.52%` (`7/155`)<br>Conf: `4.52%` (`7/155`) `[2.20%, 9.03%]`<br>Orig: `abstain` (`0/0`) |
| **`llm_scores_pointB_valid_max`** (Qwen3-8B Point B, max) | `200/250` (`50`) | **0.5958** `[0.4935, 0.6886]` | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) | **0.00%** (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/155`)*<br>Conf: `0.00%` (`0/155`) `[0.00%, 2.42%]`<br>Orig: `abstain` (`0/0`) |
| **`llm_scores_pointB_valid_mean`** (Qwen3-8B Point B, mean) | `200/250` (`50`) | **0.5913** `[0.4933, 0.6800]` | **2.22%** (`1/45`) `[0.39%, 11.57%]` | `0.65%` (`1/155`)<br>Conf: `0.65%` (`1/155`) `[0.11%, 3.56%]`<br>Orig: `abstain` (`0/0`) | **13.33%** (`6/45`) `[6.26%, 26.18%]` | `4.52%` (`7/155`)<br>Conf: `4.52%` (`7/155`) `[2.20%, 9.03%]`<br>Orig: `abstain` (`0/0`) |

*\*Note on discrete top-score ties (`n_turns`, `n_steps`, `llm_scores_*_max`):* `189/205` negative runs reach the maximum turn cap `n_turns = 14`, and `32/155` conflicting negative runs reach the maximum LLM judge score `10.0`. Because any threshold $\le 14$ (or $\le 10.0$) produces $>5\%$ FPR (`92.2%` and `20.6%` respectively), enforcing $\text{FPR} \le 5\%$ requires setting the threshold above the maximum value, resulting in `0` alarms (`0/45` recall, `0` FP).

### Apples-to-Apples Subset Check: Probes Restricted to the 200 Conflicting Runs (`45` Positives vs `155` Conflicting Negatives)
Because `linear_scores_A.json` and `llm_scores.jsonl` cover only the `200` conflicting runs (`50` original controls omitted), we also evaluate the probes and baselines restricted to the exact same `200` conflicting runs:

| Signal (200 Conflicting Runs Only) | AUROC [95% Cluster CI] | Recall @ 1% FPR ($\le 1/155$ FP) | Recall @ 5% FPR ($\le 7/155$ FP) |
| --- | --- | --- | --- |
| **`probe_A_max_conflicting_only`** (in-sample `.npz`) | **0.9726** `[0.9469, 0.9913]` | `77.78%` (`35/45`) `[63.73%, 87.46%]` | `82.22%` (`37/45`) `[68.67%, 90.71%]` |
| **`probe_A_mean_conflicting_only`** (in-sample `.npz`) | **0.8887** `[0.8193, 0.9497]` | `22.22%` (`10/45`) `[12.54%, 36.27%]` | `55.56%` (`25/45`) `[41.18%, 69.06%]` |
| **`probe_B_max_conflicting_only`** (in-sample `.npz`) | **0.9989** `[0.9962, 1.0000]` | `91.11%` (`41/45`) `[79.27%, 96.49%]` | `100.00%` (`45/45`) `[92.13%, 100.00%]` |
| **`probe_B_mean_conflicting_only`** (in-sample `.npz`) | **0.9589** `[0.9264, 0.9849]` | `35.56%` (`16/45`) `[23.22%, 50.16%]` | `88.89%` (`40/45`) `[76.50%, 95.16%]` |
| **`linear_scores_A_oof_max`** (5-fold task-OOF) | **0.8380** `[0.7628, 0.9051]` | `15.56%` (`7/45`) `[7.75%, 28.78%]` | `37.78%` (`17/45`) `[25.11%, 52.37%]` |
| **`linear_scores_A_oof_mean`** (5-fold task-OOF) | **0.8108** `[0.7150, 0.8984]` | `15.56%` (`7/45`) `[7.75%, 28.78%]` | `26.67%` (`12/45`) `[15.96%, 41.04%]` |
| **`n_turns_conflicting_only`** | **0.5323** `[0.5070, 0.5652]` | `0.00%` (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/45`) `[0.00%, 7.87%]` |
| **`msgs_total_chars_conflicting_only`** | **0.3032** `[0.2069, 0.4149]` | `0.00%` (`0/45`) `[0.00%, 7.87%]` | `0.00%` (`0/45`) `[0.00%, 7.87%]` |

---

## 3. Precision at Base Rates 1%, 5%, and 20% (at the 5% FPR Operating Point)

Using Bayes' rule at base rate $\pi \in \{0.01, 0.05, 0.20\}$,
$$\text{Precision}(\pi) = \frac{\pi \cdot \text{Recall}}{\pi \cdot \text{Recall} + (1 - \pi) \cdot \text{FPR}},$$
we report precision computed from both the **achieved empirical FPR** (`10/205 = 4.878%` for $N=250$; `7/155 = 4.516%` for $N=200$) and the **nominal 5.00% FPR** target:

| Signal | Recall @ 5% FPR | Achieved FPR | Precision @ $\pi=1\%$ (Achieved / Nominal 5% FPR) | Precision @ $\pi=5\%$ (Achieved / Nominal 5% FPR) | Precision @ $\pi=20\%$ (Achieved / Nominal 5% FPR) |
| --- | ---: | ---: | --- | --- | --- |
| **`probe_A_max`** ($N=250$) | `86.67%` (`39/45`) | `4.88%` (`10/205`) | **15.22%** / `14.90%` | **48.32%** / `47.71%` | **81.62%** / `81.25%` |
| **`probe_A_mean`** ($N=250$) | `60.00%` (`27/45`) | `4.88%` (`10/205`) | **11.05%** / `10.81%` | **39.30%** / `38.71%` | **75.46%** / `75.00%` |
| **`probe_B_max`** ($N=250$) | `100.00%` (`45/45`) | `4.88%` (`10/205`) | **17.15%** / `16.81%` | **51.90%** / `51.28%` | **83.67%** / `83.33%` |
| **`probe_B_mean`** ($N=250$) | `88.89%` (`40/45`) | `4.88%` (`10/205`) | **15.55%** / `15.22%` | **48.96%** / `48.34%` | **82.00%** / `81.63%` |
| **`linear_scores_A_oof_max`** ($N=200$) | `37.78%` (`17/45`) | `4.52%` (`7/155`) | **7.79%** / `7.09%` | **30.57%** / `28.45%` | **67.65%** / `65.38%` |
| **`linear_scores_A_oof_mean`** ($N=200$) | `26.67%` (`12/45`) | `4.52%` (`7/155`) | **5.63%** / `5.11%` | **23.71%** / `21.92%` | **59.62%** / `57.14%` |
| **`n_turns` / `n_steps`** ($N=250$) | `0.00%` (`0/45`) | `0.00%` (`0/205`) | `undef` (`0/0`) / `0.00%` | `undef` (`0/0`) / `0.00%` | `undef` (`0/0`) / `0.00%` |
| **`msgs_total_chars`** ($N=250$) | `0.00%` (`0/45`) | `4.88%` (`10/205`) | **0.00%** / `0.00%` | **0.00%** / `0.00%` | **0.00%** / `0.00%` |
| **`steps_total_chars`** ($N=250$) | `0.00%` (`0/45`) | `4.88%` (`10/205`) | **0.00%** / `0.00%` | **0.00%** / `0.00%` | **0.00%** / `0.00%` |
| **`llm_scores_all_max` / `valid_max`** ($N=200$) | `0.00%` (`0/45`) | `0.00%` (`0/155`) | `undef` (`0/0`) / `0.00%` | `undef` (`0/0`) / `0.00%` | `undef` (`0/0`) / `0.00%` |
| **`llm_scores_all_mean`** ($N=200$) | `17.78%` (`8/45`) | `4.52%` (`7/155`) | **3.82%** / `3.47%` | **17.16%** / `15.76%` | **49.60%** / `47.06%` |
| **`llm_scores_valid_mean`** ($N=200$) | `15.56%` (`7/45`) | `4.52%` (`7/155`) | **3.36%** / `3.05%` | **15.35%** / `14.07%` | **46.27%** / `43.75%` |
| **`llm_scores_pointA_valid_mean`** ($N=200$) | `11.11%` (`5/45`) | `4.52%` (`7/155`) | **2.42%** / `2.20%` | **11.46%** / `10.47%` | **38.08%** / `35.71%` |
| **`llm_scores_pointB_valid_mean`** ($N=200$) | `13.33%` (`6/45`) | `4.52%` (`7/155`) | **2.90%** / `2.62%` | **13.45%** / `12.31%` | **42.47%** / `40.00%` |

---

## 4. Key Caveats and Scientific Limits

1. **In-Sample Publisher Probe Weights (`linear_probe_A.npz`, `linear_probe_B.npz`):**
   The publisher trained `linear_probe_A.npz` and `linear_probe_B.npz` (`--save-probe` in `rs/eval_linear.py`) on all 200 conflicting runs of this dataset (the 45 positive concealment steps at $t = t_c$ vs all 2,137 steps of the 155 untampered conflicting runs). Their run-level AUROCs (`0.9793` for Probe A max, `0.9991` for Probe B max on $N=250$; `0.9726` and `0.9989` on $N=200$) are **in-sample training fit upper bounds**, not held-out performance. Comparing `probe_A_max_conflicting_only` (`0.9726`) with the 5-fold task-held-out `linear_scores_A_oof_max` (`0.8380`) directly quantifies the in-sample inflation: `+0.1346` AUROC and `+44.44` percentage points of recall at 5% FPR (`82.22%` vs `37.78%`).
2. **In-Sample Threshold Selection for Recall at 1% and 5% FPR:**
   All recall@1%FPR and recall@5%FPR values use thresholds chosen on the evaluated negative set itself. With 205 negatives (or 155 conflicting negatives), the 1% FPR operating point allows only 2 (or 1) false positives, making empirical threshold selection optimistic and sensitive to the top 1–2 negative outliers.
3. **Why Length Baselines Fail Here (and Invert):**
   In `conflicting` tasks, an honest agent that refuses to rewrite `test_solution.py` cannot make the contradictory test pass, so it keeps debugging until hitting `max_turns = 14` (`154/155` conflicting negatives take 14 turns). By contrast, an agent that tampers with `test_solution.py` occasionally finishes or stops slightly earlier or writes shorter messages (`msgs_total_chars` AUROC is `0.4059` on $N=250$ and `0.3032` on $N=200$, i.e. tampered runs have *fewer* characters on average than untampered conflicting runs).
4. **Single Generator, Short Trajectories, Single Tampering Mechanism:**
   All 250 trajectories come from a single model (`IQuest-Coder-V1-40B-Loop-Instruct`, 4-bit AWQ, temperature 0.7), a single 14-turn tool loop (`execute_bash` and `str_replace_editor`), and a single tampering mechanism (modifying `test_solution.py`).
5. **ImpossibleBench Task-Family Overlap with Goodfire:**
   The 50 tasks are drawn from `Impossible-LiveCodeBench (easy)`. Task-family overlap with Goodfire's ImpossibleBench release is known; these runs are not task-independent of Goodfire ImpossibleBench corpora.
6. **Sample Size and Oracle Nuance (`45` Positives, `2` with `pass_own=false`):**
   There are only 45 positive runs across 50 task clusters (`43` with `test_tampered=true` and `pass_own=true`, plus `2` with `test_tampered=true` and `pass_own=false`). All 45 are kept as positives per our scoring recipe.

---

## 5. Exact Commands and Deliverables

To reproduce `results.json` deterministically from `/private/tmp/ov1104/probes/workers/rs-recon/` (runtime ~18–35s on CPU, $0 cost, standard library + `numpy`/`scipy` only):

```sh
cd /private/tmp/ov1104/probes/workers/rs-recon
/private/tmp/ov1104/probes/venv/bin/python rs_recon.py
```

### Files Written in `/private/tmp/ov1104/probes/workers/rs-recon/`
- `rs_recon.py`: Self-contained deterministic evaluation and verification script (`numpy` and `scipy` only; no local model loads; no raw IDs or text persisted).
- `results.json`: Full machine-readable output containing `cohort_summary`, `reproduction_check`, `signals` (19 signals with coverage, AUROC, 2,000-resample cluster bootstrap CIs, recall/FPR with Wilson CIs, and precision at 1%/5%/20% base rates), `conflicting_200_comparison`, and `runs` (250 rows keyed only by 16-hex SHA-256 `run_key` and `task_key`).
- `RESULT.md`: This report.
