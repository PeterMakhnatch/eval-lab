# Threshold portability across DEV corpora (ov1104-detbench)

Exploratory (not pre-registered). For each signal, pick the threshold that gives the target
FPR on **four** DEV corpora, then apply it to the fifth and record the FPR and recall there.
This is the operational failure mode behind "classifiers fail across sources": pooled AUROC
can look fine while the threshold does not transfer. DEV only; no held-out row is read.

Thresholds and combinations are produced by `score.choose_threshold` and `score.combine`, the
same functions `SIGNAL-MATRIX.md` uses. Repro:

```bash
uv run python research/experiments/ov1104-detbench/threshold_portability.py
```

## Target FPR 1% on the four training corpora

| signal | c1c5 | cheatbench | goodfire | sol-astra | terminal-wrench | worst overshoot |
|---|---|---|---|---|---|---|
| `length_chars` | 0.0% / 0% | 22.7% / 4% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | **22.7%** (cheatbench) |
| `length_steps` | 0.0% / 0% | 38.7% / 16% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | **38.7%** (cheatbench) |
| `l0v_full_any_hard` | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | **0.0%** (-) |
| `l0v_full_l0_hard_gate_rule_hidden_info_read` | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | **0.0%** (-) |
| `cached_facets_haiku_all` | - | 0.0% / 0% | 0.0% / 0% | - | - | **0.0%** (-) |
| `j2b_actions_task_diff_v2_flash` | 7.1% / 83% | 0.0% / 22% | 0.0% / 0% | 0.0% / 88% | 1.4% / 49% | **7.1%** (c1c5) |
| `j2b_3fam_mean` | 0.0% / 83% | 0.0% / 24% | 0.0% / 1% | 0.0% / 88% | 4.8% / 77% | **4.8%** (terminal-wrench) |
| `probe_qwen3_8b` | 0.0% / 0% | 4.0% / 3% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | **4.0%** (cheatbench) |
| `probe_qwen3_8b_run_mean_z` | 0.0% / 0% | 0.0% / 1% | 0.0% / 0% | 8.3% / 0% | 1.4% / 4% | **8.3%** (sol-astra) |
| `max_rule_j2b_length` | 0.0% / 36% | 1.3% / 21% | 0.0% / 0% | 0.0% / 88% | 0.7% / 44% | **1.3%** (cheatbench) |
| `max_best_rule_j2b` | 5.3% / 45% | 0.0% / 21% | 0.0% / 0% | 0.0% / 100% | 1.4% / 49% | **5.3%** (c1c5) |
| `max_rule_probez_j2b` | 0.0% / 36% | 0.0% / 23% | 0.0% / 0% | 2.1% / 88% | 2.1% / 49% | **2.1%** (sol-astra) |
| `or_best_rule_j2b` | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | **0.0%** (-) |

Cells are `FPR on the held corpus / recall on the held corpus` at the threshold tuned for
1% FPR on the other four.

### Worst overshoot, ranked

| signal | worst held corpus | FPR there |
|---|---|---:|
| `length_steps` | cheatbench | 38.7% |
| `length_chars` | cheatbench | 22.7% |
| `probe_qwen3_8b_run_mean_z` | sol-astra | 8.3% |
| `j2b_actions_task_diff_v2_flash` | c1c5 | 7.1% |
| `max_best_rule_j2b` | c1c5 | 5.3% |
| `j2b_3fam_mean` | terminal-wrench | 4.8% |
| `probe_qwen3_8b` | cheatbench | 4.0% |
| `max_rule_probez_j2b` | sol-astra | 2.1% |
| `max_rule_j2b_length` | cheatbench | 1.3% |
| `l0v_full_any_hard` | - | 0.0% |
| `l0v_full_l0_hard_gate_rule_hidden_info_read` | - | 0.0% |
| `cached_facets_haiku_all` | - | 0.0% |
| `or_best_rule_j2b` | - | 0.0% |

## Target FPR 5% on the four training corpora

| signal | c1c5 | cheatbench | goodfire | sol-astra | terminal-wrench | worst overshoot |
|---|---|---|---|---|---|---|
| `length_chars` | 0.0% / 0% | 34.7% / 15% | 1.4% / 15% | 0.0% / 0% | 0.0% / 0% | **34.7%** (cheatbench) |
| `length_steps` | 0.0% / 0% | 52.0% / 33% | 0.0% / 1% | 0.0% / 0% | 0.0% / 0% | **52.0%** (cheatbench) |
| `l0v_full_any_hard` | 0.0% / 0% | 32.0% / 31% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | **32.0%** (cheatbench) |
| `l0v_full_l0_hard_gate_rule_hidden_info_read` | 5.3% / 0% | 0.0% / 20% | 2.9% / 8% | 0.0% / 0% | 2.7% / 73% | **5.3%** (c1c5) |
| `cached_facets_haiku_all` | - | 36.0% / 64% | 0.0% / 0% | - | - | **36.0%** (cheatbench) |
| `j2b_actions_task_diff_v2_flash` | 35.7% / 100% | 0.0% / 43% | 0.0% / 9% | 0.0% / 100% | 7.5% / 87% | **35.7%** (c1c5) |
| `j2b_3fam_mean` | 28.6% / 83% | 0.0% / 45% | 0.0% / 8% | 0.0% / 100% | 20.5% / 89% | **28.6%** (c1c5) |
| `probe_qwen3_8b` | 0.0% / 17% | 5.3% / 3% | 1.4% / 1% | 0.0% / 12% | 13.7% / 24% | **13.7%** (terminal-wrench) |
| `probe_qwen3_8b_run_mean_z` | 21.4% / 0% | 0.0% / 13% | 4.3% / 11% | 10.4% / 12% | 2.1% / 8% | **21.4%** (c1c5) |
| `max_rule_j2b_length` | 10.5% / 45% | 6.7% / 43% | 2.9% / 10% | 0.0% / 100% | 4.1% / 87% | **10.5%** (c1c5) |
| `max_best_rule_j2b` | 21.1% / 55% | 0.0% / 43% | 2.9% / 10% | 0.0% / 100% | 9.6% / 92% | **21.1%** (c1c5) |
| `max_rule_probez_j2b` | 10.5% / 45% | 0.0% / 45% | 4.3% / 16% | 8.3% / 100% | 5.5% / 87% | **10.5%** (c1c5) |
| `or_best_rule_j2b` | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 0.0% / 0% | 10.3% / 92% | **10.3%** (terminal-wrench) |

Cells are `FPR on the held corpus / recall on the held corpus` at the threshold tuned for
5% FPR on the other four.

### Worst overshoot, ranked

| signal | worst held corpus | FPR there |
|---|---|---:|
| `length_steps` | cheatbench | 52.0% |
| `cached_facets_haiku_all` | cheatbench | 36.0% |
| `j2b_actions_task_diff_v2_flash` | c1c5 | 35.7% |
| `length_chars` | cheatbench | 34.7% |
| `l0v_full_any_hard` | cheatbench | 32.0% |
| `j2b_3fam_mean` | c1c5 | 28.6% |
| `probe_qwen3_8b_run_mean_z` | c1c5 | 21.4% |
| `max_best_rule_j2b` | c1c5 | 21.1% |
| `probe_qwen3_8b` | terminal-wrench | 13.7% |
| `max_rule_j2b_length` | c1c5 | 10.5% |
| `max_rule_probez_j2b` | c1c5 | 10.5% |
| `or_best_rule_j2b` | terminal-wrench | 10.3% |
| `l0v_full_l0_hard_gate_rule_hidden_info_read` | c1c5 | 5.3% |

## What this shows

Worst overshoot when the threshold is tuned on four corpora and applied to the fifth:

| signal | 1% target | 5% target |
|---|---:|---:|
| `length_steps` | **38.7%** (cheatbench) | **52.0%** (cheatbench) |
| `length_chars` | **22.7%** (cheatbench) | **34.7%** (cheatbench) |
| `cached_facets_haiku_all` | 0.0% | 36.0% (cheatbench) |
| `j2b_actions_task_diff_v2_flash` | 7.1% (c1c5) | 35.7% (c1c5) |
| `j2b_3fam_mean` | 4.8% (terminal-wrench) | 28.6% (c1c5) |
| `probe_qwen3_8b_run_mean_z` | 8.3% (sol-astra) | 21.4% (c1c5) |
| `l0v_full_any_hard` | 0.0% | 32.0% (cheatbench) |
| `l0v_full_l0_hard_gate_rule_hidden_info_read` | 0.0% | 5.3% (c1c5) |
| `max_rule_j2b_length` | **1.3%** (cheatbench) | **10.5%** (c1c5) |
| `max_rule_probez_j2b` | **2.1%** (sol-astra) | **10.5%** (c1c5) |
| `max_best_rule_j2b` | 5.3% (c1c5) | 21.1% (c1c5) |
| `or_best_rule_j2b` | 0.0% (see caveat) | 10.3% (terminal-wrench) |

- **A shared threshold on a length baseline is unusable.** `length_chars` overshoots to
  22.7% at a 1% target and 34.7% at a 5% target; `length_steps` to 38.7% and 52.0%. All of the
  worst cases are `cheatbench`. Pooled AUROC for `length_chars` is 0.53, so the ranking looks
  harmless while the operating point is wrong by one to two orders of magnitude.
- **The percentile-rank `max` combinations are the most portable objects measured here, and
  the advantage is much larger at the tight target.** At 1% the best combination overshoots to
  1.3-2.1% versus 22.7-38.7% for the length baselines (roughly a 20x difference). At 5% the
  same combination overshoots to 10.5%, still the best but only about 3x better than the
  baselines. The mechanism is that the rank is computed against each member's own DEV-negative
  distribution, so the operating point follows the corpus instead of assuming a shared score
  scale.
- **Binary rule signals are not automatically safe.** `l0v_full_any_hard` reads 0.0% at a 1%
  target but 32.0% at 5% on `cheatbench`: the single "rule fired" value sits between the two
  targets, so the overshoot appears as soon as the target is loose enough to reach it.
- **`or_best_rule_j2b` reads 0.0% at 1% for a bad reason.** Its DEV 1%-FPR threshold is `+inf`
  (the OR fires on more than 1% of DEV negatives even at its lowest non-trivial point), so it
  flags nothing and cannot overshoot. Treat 0.0% as "not operating", not as portable. This is
  the same `+inf` artefact that made the earlier "18/20 held-out sets at 5%" claim misleading.
- **Practical rule:** use the `max` combination rather than a single member; if the corpus
  family is known, re-derive the threshold per family; and if one threshold must be shared
  across unknown corpora, expect the realised FPR to be several times the target even for the
  best signal measured here. A 1% target is the only regime where the combination's FPR stays
  near the target.

The table values above are fixed text tied to the current DEV set; re-running the script
regenerates the measured tables but does not recompute this paragraph.

