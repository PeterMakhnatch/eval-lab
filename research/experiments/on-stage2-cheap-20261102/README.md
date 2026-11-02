# D6-STAGE2-CHEAP: Evaluation of Lightweight Models in AutoReview's Full Production Cascade

## Overview
This experiment evaluates whether lightweight, cheaper off-the-shelf models (`gemini-2.5-flash-lite`, `gemini-3.1-flash-lite`) can replace the production teacher model (`gemini-3.8-flash`) in AutoReview's full two-stage verification cascade (Stage 1 compact card reading + triggered Stage 2 un-truncated raw tool call retrieval).

Evaluated on the 272 covered DEV cases of TraceMonitor-Bench DEV v1 across 5 benchmark families (`terminal_wrench`, `cheatbench`, `deep_swe`, `goodfire`, `c1c5`), pre-registered before computation (`prereg.md`, SHA256: `6952806e5715`).

## Core Findings & Gate Verdict
- **Gate Verdict: TEACHER REQUIRED AT STAGE 2.**
- **Report Split AUROC ($N_{\text{pos}}=67, N_{\text{neg}}=69$):**
  - Stored Teacher (`ar_g38_stored`): **0.6661 [0.5993, 0.7342]**
  - `gemini-2.5-flash-lite` Cascade: **0.5373 [0.5075, 0.5672]** ($\Delta = -0.1288$)
  - `gemini-3.1-flash-lite` Cascade: **0.5373 [0.5075, 0.5746]** ($\Delta = -0.1288$)
- **Report Split Recall @ FA $\le 2\%$ (Tune Threshold):**
  - Stored Teacher: **19/67 = 28.4% [19.0%, 40.1%]** at 2.9% FA (2/69)
  - `gemini-2.5-flash-lite` Cascade: **5/67 = 7.5% [3.2%, 16.3%]** at 0.0% FA (0/69)
  - `gemini-3.1-flash-lite` Cascade: **3/67 = 4.5% [1.5%, 12.4%]** at 0.0% FA (0/69)
- **Paired Exact McNemar Testing on 67 Report Positives:**
  - `gemini-2.5-flash-lite` vs Stored Teacher: $b = 15$ (teacher only), $c = 1$ (cheap only), both = 4, neither = 47, exact **$p = 0.0005$**.
  - `gemini-3.1-flash-lite` vs Stored Teacher: $b = 16$ (teacher only), $c = 0$ (cheap only), both = 3, neither = 48, exact **$p < 0.0001$**.
- **Conclusion:** Lightweight models fail to retain AutoReview's detection power. They suffer from trigger under-firing at Stage 1 (giving 0 suspicion to suspicious mutation ledgers) and false exoneration at Stage 2 (interpreting package manager replacements and mock test wrappers as standard troubleshooting).
- **Auditor Verification & Attribution (see `VERIFIER-CORRECTIONS.md` and `on-stage2-forced-20261102`):**
  - Recomputing directly from cascade receipts, the 19 teacher catches missed by cheap models partition into:
    - `gemini-2.5-flash-lite` (15 missed): **9 lost to Trigger Blindness** (Stage 1 never triggered) and **6 lost to Evidence Blindness** (Stage 2 ran on raw evidence but exonerated).
    - `gemini-3.1-flash-lite` (16 missed): **8 lost to Trigger Blindness** and **8 lost to Evidence Blindness**.
  - Independent forced Stage-2 isolation (`D6B-FORCED-STAGE2`) confirms that even when presented with identical un-truncated evidence, cheap models detect only 12–13 of 46 positive cheats (26.1%–28.3%) versus 24/46 (52.2%) for the teacher rerun ($p \le 0.0010$).

## Files in this Experiment
- `run_stage2_cascade.py`: Production cascade runner supporting Stage 1 evaluation, trigger checks, Stage 2 un-truncated retrieval execution, and attestation.
- `analyze_stage2_cheap.py`: Statistical analysis using `stats.py` helpers (bootstrap AUROC, out-of-fold operating points, paired McNemar, noise floor, and failure taxonomy).
- `dry_run.py`: 5-case dry run script validating Stage 2 prompt execution and JSON parsing across models.
- `stage2_cheap_results.json`: Comprehensive machine-readable results, confidence intervals, McNemar counts, and noise floor readings.
- `test_stage2_cheap.py`: Unit tests covering verdict confirmation logic and gate thresholds.

## Artifact Hashes (SHA256 first 12)
- Pre-Registration: `6952806e5715` (`research-context/bridge/2026-11-02-overnight/loop/raw/D6-STAGE2-CHEAP/prereg.md`)
- Cascade Runner: `2807b7c3ff99` (`run_stage2_cascade.py`)
- Analysis Script: `e0dc41a3a448` (`analyze_stage2_cheap.py`)
- Results JSON: `1c2956fe5bcf` (`stage2_cheap_results.json`)
- Raw Receipts: `~/Developer/eval-lab-results/2026-11-02/D6/`
