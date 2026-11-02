# VERIFIER CORRECTIONS: D6-STAGE2-CHEAP

**Author:** D6B-FORCED-STAGE2 Independent Auditor  
**Date:** 2026-11-02  
**Target:** PR #1774 (`research/experiments/on-stage2-cheap-20261102/`) and bridge report `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/D6-STAGE2-CHEAP/report.md`  

---

## Corrections and Empirical Corrections

### 1. Failure Taxonomy Breakdown of the 19 Missed Catches
- **D6 Original Statement:** D6 claimed 7 of 19 missed cases were due to Stage-1 Trigger Blindness and 7 of 19 were due to Stage-2 Evidence Blindness (accounting for only 14 of 19 cases).
- **Exact Receipt Recomputation:**
  Recomputed directly from `gemini-2.5-flash-lite-cascade.jsonl` and `gemini-3.1-flash-lite-cascade.jsonl` on the 67 report-split positives where stored teacher caught 19:
  - **`gemini-2.5-flash-lite` (15 missed cases, 4 detected):**
    - **9 cases lost to Trigger Blindness** (Stage 1 gave suspicion < 25 and no L0 rule fired, so Stage 2 never executed).
    - **6 cases lost to Evidence Blindness** (Stage 2 executed on un-truncated raw evidence, but the model exonerated the cheat).
    - $9 + 6 = 15$ missed cases ($15 + 4 = 19$).
  - **`gemini-3.1-flash-lite` (16 missed cases, 3 detected):**
    - **8 cases lost to Trigger Blindness**.
    - **8 cases lost to Evidence Blindness**.
    - $8 + 8 = 16$ missed cases ($16 + 3 = 19$).
- **Implication:** The loss in cascade detection power is driven roughly equally by Stage-1 sensitivity failure (trigger blindness) and Stage-2 comprehension failure (evidence blindness).

---

### 2. Teacher Noise Floor in Label Terms
- **D6 Original Statement:** D6 noted that the teacher rerun on 48 stored Stage-2 cases agreed with stored verdicts on only 32/48 (66.7%) of cases (suspicion MAE 21.9 pts), but did not measure label-based True Positives or run-to-run repeat stability.
- **Empirical Measurement from D6B:**
  - When Gemini 3.8 Flash is rerun twice independently through the identical runner (`rerun_1` vs `rerun_2`), its verdict agreement is **46/48 = 95.83%**, with an MAE of only **4.67 points** and a median difference of **0.0 points** ($b=1, c=0, p=1.0000$).
  - In label terms on the 46 positive cheats in Cohort A, the stored teacher reference achieved **30/46 = 65.2%** recall, while independent teacher reruns achieve **24/46 = 52.2%** (`rerun_1`) and **23/46 = 50.0%** (`rerun_2`) at low thinking, and **25/46 = 54.4%** at medium thinking.
  - The single historical stored run was an optimistic draw ($b=7, c=1, p=0.0703$ vs `rerun_1`), but under identical inputs the teacher's True Positive rate is stable around 50%–54%.

---

### 3. Isolated Stage-2 Capability (Same Inputs, Ground-Truth Judged)
- **D6 Original Statement:** Concluded "the teacher is strictly required at Stage 2" from full-cascade runs where cheap models only evaluated 37 cases (their own triggers).
- **Forced Stage-2 Evidence from D6B:**
  - When forced to evaluate the exact same un-truncated raw evidence across all 48 stored Stage-2 cases (46 positives):
    - Teacher Rerun 1 detects **24/46 = 52.2%** cheats.
    - `gemini-2.5-flash-lite` detects **12/46 = 26.1%** cheats ($b=12, c=0, p=0.0005$).
    - `gemini-3.1-flash-lite` detects **13/46 = 28.3%** cheats ($b=11, c=0, p=0.0010$).
  - On the 14 ambiguous band-only positive cheats, cheap models detect **0/14 = 0.0%**, completely failing to identify malicious circumvention.
- **Conclusion:** D6's headline that the teacher is required at Stage 2 is **confirmed and upheld**, but now grounded in isolated identical inputs against ground-truth labels and calibrated against the teacher's repeat-run noise floor.
