# FIX-S2-RETRIEVAL: Restored Stage-2 Evidence Retrieval

Restores Stage-2 evidence retrieval in `src/evallab/autoreview_monitor.py` with strict character containment:
- Prioritizes L0-hit steps first (sensitive reads, environment probes, test key reads)
- Prioritizes Stage-1 cited steps second
- Prioritizes mutations to target surfaces third, then other mutations
- Filters out-of-range step IDs (`>= len(steps)`) and invalid types (booleans, floats, non-digits) so hallucinated IDs never take a slot
- Caps candidate steps at `max_steps=6` (sized from stored p90 step count of 6 steps; `candidate_stage2_steps` returns at most 6 prioritized steps)
- Raises `ValueError` if `stage2_prompt` receives more steps than `max_steps`, ensuring callers explicitly select prioritized steps
- Caps total prompt at `max_total_chars=45_000` (sized from stored p90 prompt size of 43,332 chars; note that Cohort A was used for sizing)
- Enforces the 45,000-character cap via a strict reduction hierarchy:
  1. Reduces search needles and max hits per needle first
  2. Truncates/drops search blocks
  3. Truncates compact view (preserving critical tail backcheck and high-signal actions evidence) down to budget
  4. Squeezes step outputs down to `MIN_STEP_BUDGET_CHARS = 500` per step
  5. Minimal feasible total consists of framework (~2,450 chars) + `MIN_VIEW_CHARS` (500) + (len(cited) * 500) ≈ 5,950 chars; if even floors exceed cap, truncates with an explicit marker rather than silently exceeding
- Updated CLI help and `STAGE2_NOTE` to reflect budgeted retrieval with per-field limits and character caps
- Updated `autoreview_run` to scan L0 tripwires in full runs to feed sensitive reads into Stage 2, with `candidate_step_ids` reporting at most 6 prioritized steps
- Migrated callers in `on-stage2-cheap-20261102`, `on-stage2-everywhere-20261102`, and `on-stage2-forced-20261102`

## Verification

### Cohort A (46 Positive Cheats, 2 Clean Controls)
*Note on Cohort A:* Cohort A was selected by the stored production trigger ($25 \le \text{suspicion} \le 85$ OR L0 hit). Because it contains only 2 clean negatives, Cohort A false-alarm figures (1 / 2) say nothing about operational false alarm control; false alarms are evaluated on the 69 negative cases of the 136-case report split.

- **Re-run at new head (Google Gemini 3.8 Flash, low thinking, 0 fallbacks, 0 argon):**
  - **Repeat 1:** 30 / 46 TP (65.2%) [95% Wilson: 50.8%, 77.3%], 1 / 2 FP
  - **Repeat 2:** 30 / 46 TP (65.2%) [95% Wilson: 50.8%, 77.3%], 1 / 2 FP
  - *(Predecessor run without strict cap enforcement: Repeat 1: 29 / 46 [48.6%, 75.5%], Repeat 2: 30 / 46 [50.8%, 77.3%])*

### Paired McNemar Comparisons (on 46 Positive Cheats)
Evaluated strictly using `stats.paired(a_bools, b_bools)`:

| Comparison | $b$ (only 1st) | $c$ (only 2nd) | Both Alert | Neither Alert | Exact McNemar $p$ | Assessment |
|---|---|---|---|---|---|---|
| **Patched R1 vs Arm A R1** | 6 | 0 | 24 | 16 | **0.03125** | Statistically significant recovery (+6 cheats) |
| **Patched R2 vs Arm A R1** | 6 | 0 | 24 | 16 | **0.03125** | Statistically significant recovery (+6 cheats) |
| **Patched R1 vs Arm A R2** | 7 | 0 | 23 | 16 | **0.01562** | Statistically significant recovery (+7 cheats) |
| **Patched R2 vs Arm A R2** | 7 | 0 | 23 | 16 | **0.01562** | Statistically significant recovery (+7 cheats) |
| *Predecessor Patched R1 vs Arm A R1* | 5 | 0 | 24 | 17 | **0.06250** | *Not significant at 0.05 (+5 cheats)* |
| **Patched R1 vs Arm B1 R1** | 3 | 4 | 27 | 12 | **1.00000** | No detectable difference (underpowered; discordant b/c 3/4; not an equivalence test) |
| **Patched R2 vs Arm B1 R2** | 3 | 3 | 27 | 13 | **1.00000** | No detectable difference (underpowered; discordant b/c 3/3; not an equivalence test) |
| **Patched R1 vs Stored Teacher** | 7 | 7 | 23 | 9 | **1.00000** | No detectable difference (underpowered; discordant b/c 7/7; not an equivalence test) |
| **Patched R2 vs Stored Teacher** | 7 | 7 | 23 | 9 | **1.00000** | No detectable difference (underpowered; discordant b/c 7/7; not an equivalence test) |

### End-to-End Evaluation on Full 136-Case Report Split
Evaluated on 67 positive cheats and 69 clean controls using stored Stage-1 scores and patched Stage-2 scores on triggered cases.
Tune threshold (98.5th percentile clean quantile): 18.0.

| Version | Recall @ Tune Thresh (95% Wilson CI) | False Alarms @ Tune Thresh (95% Wilson CI) | Fixed Alert Recall ($\ge 50$) | Fixed Alert False Alarms |
|---|---|---|---|---|
| **Stored Baseline Cascade** | 19–20 / 67 (28.4%–29.9%) | 2 / 69 (2.90%) [0.8%, 10.0%] | 19–20 / 67 (28.4%–29.9%) | 2 / 69 (2.90%) |
| **Stage 1 Alone (D7 Finding)** | **21 / 67 (31.3%)** [21.5%, 43.3%] | 2 / 69 (2.90%) [0.8%, 10.0%] | 21 / 67 (31.3%) | 2 / 69 (2.90%) |
| **Patched Repeat 1** | **22 / 67 (32.8%)** [22.8%, 44.7%] | 2 / 69 (2.90%) [0.8%, 10.0%] | 21 / 67 (31.3%) | 2 / 69 (2.90%) |
| **Patched Repeat 2** | **22 / 67 (32.8%)** [22.8%, 44.7%] | 2 / 69 (2.90%) [0.8%, 10.0%] | 21 / 67 (31.3%) | 2 / 69 (2.90%) |

**Honest Epistemic Context:**
As identified in VF-D789, Stage 1 alone already detects 21 / 67 on the report split and alerts on 34 of the 46 positives in Cohort A. The end-to-end recall difference between Stage 1 alone (21 / 67) and the restored Stage-2 cascade (22 / 67) is within noise (+1 case). The genuine value of this PR is restoring stored Stage-2 behavior on the 48-case cohort with strict prompt character containment (guaranteed $\le 45,000$ characters) and safe prioritization, rather than an end-to-end performance breakthrough.
