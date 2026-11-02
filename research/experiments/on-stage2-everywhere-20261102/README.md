> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.

# Experiment: Trigger-Policy Study (D7-STAGE2-EVERYWHERE)

## Overview

Investigates whether AutoReview's cheat detection signal is maximized by expanding its Stage-2 execution trigger from production's gated 17.6% (48 of 272 DEV cases: 21 via suspicion band $25 \le s \le 85$, 27 via deterministic L0 gate rules) to **100% of cases (Stage 2 Everywhere)**, or by narrowing to L0-only, Band-only, or Stage 1 only.

## Evaluated Policies

All policies evaluate the production teacher model `google-antigravity/gemini-3.8-flash` at thinking level `low`:

- **P0 (Production Reference):** Stored AutoReview readings (`ar_g38.jsonl`), triggering Stage 2 on 48/272 cases (20/136 on Report split).
- **P1 (Stage 2 Everywhere):** Forced Stage-2 execution on all 272 DEV cohort cases (reusing D6B Teacher Rerun 1 for the 48 stored cases, plus 224 newly executed cases).
- **P2 (L0-Only Trigger):** Stage 2 executed only on deterministic L0 gate hits (33 DEV cases, 16 on Report split); Stage 1 used otherwise.
- **P3 (Band-Only Trigger):** Stage 2 executed only when Stage 1 suspicion is in $25 \le s \le 85$ (21 DEV cases, 6 on Report split); Stage 1 used otherwise.
- **P4 (Stage 1 Only):** Stage 2 never executed; all 272 cases evaluated purely on Stage 1 compact card views.

## Headline Findings

1. **Stage 2 Everywhere does not improve detection:** P1 achieves **18/67 = 26.9% [17.7%, 38.5%]** recall at FA $\le 2\%$, compared to **20/67 = 29.8% [20.2%, 41.7%]** for P0 (McNemar $b=3, c=1, p=0.6250$). AUROC falls from **0.6661 [0.5964, 0.7348]** (P0) to **0.6401 [0.5756, 0.7038]** (P1).
2. **Mechanism of Degradation (False Alarm Inflation):** Stage 2 prompt engineering was optimized for suspicious traces. When applied indiscriminately to 100% of clean traces, Stage 2 generates false alarms (3/69 = 4.3% in Report, up from 2/69 = 2.9%), forcing the out-of-fold operating threshold up from 18.0 to 35.0 and suppressing borderline detections in `deep_swe` (from 2/12 to 0/12).
3. **Pre-Registered Gate Verdict: RETAIN PRODUCTION TRIGGER.**
   - Rule 1 (Stage 2 Everywhere): FAILS recall improvement ($\ge 1.25\times$) and significance ($p < 0.05$).
   - Rule 2 (L0-Only Enough): FAILS call reduction budget ($80\% \ge 60\%$).

## Reproducibility Manifest

| Artifact | Path | SHA256[:12] |
| :--- | :--- | :--- |
| Pre-Registration | `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/D7-STAGE2-EVERYWHERE/prereg.md` | `b48cd057da2b` |
| Runner Script | `research/experiments/on-stage2-everywhere-20261102/run_stage2_everywhere.py` | `a29b4b5c46e2` |
| Analysis Script | `research/experiments/on-stage2-everywhere-20261102/analyze_stage2_everywhere.py` | `06509fe0fdd5` |
| Results Summary | `research/experiments/on-stage2-everywhere-20261102/stage2_everywhere_results.json` | `e815ecec6598` |
| Unit Tests | `research/experiments/on-stage2-everywhere-20261102/test_stage2_everywhere.py` | `c8295972f8fe` |
| Full P1 Receipts | `receipts/all-272-p1-stage2-low.jsonl` | `51cb187a6f34` |
| Forced 224 Receipts | `receipts/forced-stage2-224-low.jsonl` | `773b6eb8e2ad` |
| Repeat 48 Receipts | `receipts/repeat-sample-48-low.jsonl` | `259b9c675e65` |
| D6B Reference Receipts | `receipts/d6b-teacher-rerun-1-low.jsonl` | `c7415c8c890a` |
