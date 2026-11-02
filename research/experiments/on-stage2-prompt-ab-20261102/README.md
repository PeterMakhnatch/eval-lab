# D8-STAGE2-PROMPT-AB: Stage-2 Prompt & Retrieval A/B Experiment

This experiment isolates whether the AutoReview Stage-2 regression observed between stored production readings (`ar_g38`, 30/46 detections on Cohort A) and today's repository code (24/46 and 23/46 on Cohort A reruns) was driven by prompt rubric changes, evidence retrieval changes, or both.

## Pre-registration
- Frozen before execution: `prereg.md` (sha256[:12] `fc097b3bf68a`).

## Experimental Design
Cohort A: 48 cases from `trace-monitor-bench-dev-v1` where stored AutoReview executed Stage 2 (46 positive cheat cases, 2 negative non-cheat cases; 34 Terminal Wrench, 8 deep_swe, 5 CheatBench, 1 Goodfire).
Model: `google-antigravity/gemini-3.8-flash` at `--thinking low --no-tools`.

Evaluated Arms:
- **Arm A (Current Baseline)**: Today's prompt and retrieval builder from `main` (D6B reruns 1 & 2).
- **Arm B1 (Stored Exact)**: Exact stored prompt text from production runs (stored rubric + stored view + stored retrieval). Two repeats.
- **Arm B2 (Stored Rubric + Today Retrieval)**: Stored rubric header paired with today's compact view and today's retrieved evidence. Two repeats.
- **Arm C (Today Rubric + Stored Retrieval)**: Today's "Categories of Cheating" rubric paired with the stored view and stored retrieval. Two repeats.

## Key Findings & Pre-registered Verdict
- **Pre-registered Verdict**: **`today's prompt regressed`** (CONFIRMED).
  - Arm B1 (stored prompt exact) detects **31/46** (repeat 1) and **30/46** (repeat 2), exactly matching the stored production teacher (30/46).
  - Compared to Arm A (rerun 1: 24/46), Arm B1 gains +7 and +6 detections.
  - Paired exact McNemar tests:
    - B1 rep 1 vs A1: $b=7, c=0, p = 0.01562$ (statistically significant)
    - B1 rep 2 vs A1: $b=6, c=0, p = 0.03125$ (statistically significant)
  - Both repeats exceed the pre-registered threshold ($\ge 4$ detections above Arm A with $p < 0.05$).

## Root Cause Analysis: Retrieval vs. Rubric
The factorial comparison between Arm B2 and Arm C reveals that **retrieval selection is the primary driver of lost detections**:
1. **Arm B2 (Stored Rubric + Today Retrieval)**: TP = 24/46 (rep 1) and 27/46 (rep 2). With today's retrieval, restoring the stored rubric alone leaves repeat 1 identical to Arm A (24/46, $b=2, c=2, p=1.00$).
2. **Arm C (Today Rubric + Stored Retrieval)**: TP = 27/46 (rep 1) and 29/46 (rep 2). Restoring stored retrieval under today's rubric immediately recovers 3 to 5 detections ($p=0.0625$ in repeat 2).
3. **Arm B1 (Both Stored Rubric + Stored Retrieval)**: TP = 31/46 and 30/46. Recombining both reaches peak detection capability.

### Specific Mechanism of Retrieval Regression
Inspection of the discordant cases reveals two primary retrieval bugs in today's code:
1. **Omission of L0 Tripwire Steps**: Production AutoReview retrieved `l0_hit_steps` for cases triggered by L0 tripwires (e.g. read commands probing sensitive paths). Today's `candidate_stage2_steps()` only queries file mutations (`anatomy.patch_and_mutations['mutations']`), completely omitting non-mutation cheat actions like sensitive reads or environment probes unless stage 1 happened to cite them.
2. **Context Dilution / Over-retrieval**: Today's `candidate_stage2_steps()` blindly retrieves up to 30 mutation steps and searches paths across the run. On complex runs (such as deep_swe), this inflated Stage-2 prompts to over 100k-135k characters (3x-5x larger than stored prompts), burying high-signal cheat steps in routine git/compilation noise and dropping model suspicion from 90+ down to 0.

## Artifacts & Reproducibility
- `prereg.md`: pre-registered evaluation plan (`fc097b3bf68a`).
- `run_stage2_ab.py`: execution harness for nested `omp -p` calls with attestation tracking.
- `analyze_stage2_ab.py`: statistical evaluation, Wilson intervals, and paired McNemar calculations.
- `stage2_prompt_ab_results.json`: summary metrics, discordant counts, and exact p-values.
- `test_stage2_prompt_ab.py`: automated test suite for parser and results validation.
