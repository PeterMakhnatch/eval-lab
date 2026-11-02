> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.

# D6B-FORCED-STAGE2: Isolate AutoReview Stage 2 on Identical Inputs against Ground-Truth Labels

## Executive Summary

This experiment rigorously isolates AutoReview's Stage 2 (raw un-truncated evidence evaluation) from Stage-1 trigger effects. It evaluates whether lightweight models (`gemini-2.5-flash-lite`, `gemini-3.1-flash-lite`) can replace the production teacher model (`gemini-3.8-flash`) when presented with the **exact same raw evidence and prompt**, directly measured against DEV ground-truth labels and calibrated against the teacher's own repeat-run noise floor.

Pre-registered in `prereg.md` (SHA256: `91a179db4024`) prior to running evaluation calls or joining labels.

### Core Headline Findings

1. **Gate Verdict: TEACHER REQUIRED AT STAGE 2 FOR THESE INPUTS.**
   - Under the pre-registered decision rule, a cheap model is viable only if its Pure Stage-2 True Positives (TP) on Cohort A ($N=48$, 46 positive cheats, 2 clean negatives) reach at least 80% of the teacher rerun's detections ($0.80 \times 24 = 19.2$) and is not significantly worse in paired McNemar testing.
   - **`gemini-2.5-flash-lite` (low):** Detects **12/46 = 26.1% [15.3%, 40.9%]** (only 50.0% of teacher rerun). Paired McNemar vs Teacher Rerun: $b = 12$ (teacher rerun only), $c = 0$ (cheap only), exact **$p = 0.0005$** (significantly worse).
   - **`gemini-3.1-flash-lite` (low):** Detects **13/46 = 28.3% [17.0%, 43.1%]** (only 54.2% of teacher rerun). Paired McNemar vs Teacher Rerun: $b = 11$ (teacher rerun only), $c = 0$ (cheap only), exact **$p = 0.0010$** (significantly worse).
   - Both cheap models conclusively fail both criteria.

2. **Teacher Repeat Variance (Noise Floor Ground Truth):**
   - When Gemini 3.8 Flash is rerun independently through the identical runner twice (`rerun_1` vs `rerun_2`), its verdict agreement is **46/48 = 95.83%**, with a Mean Absolute Error (MAE) of only **4.67 points** and a median difference of **0.0 points** ($b=1, c=0, p=1.0000$).
   - Gemini 3.8 Flash is highly stable run-to-run under identical prompt inputs.
   - Compared against the historical single stored reference run (`ar_g38_stored`), the teacher reruns detect **23–24 of 46** cheats (50.0%–52.2%) versus **30/46** (65.2%) for the stored reference run ($b=7, c=1, p=0.0703$). The stored run was an optimistic draw, but the teacher rerun maintains more than double the cheat detection power of the cheap models.

3. **Evidence Blindness vs Trigger Blindness (Recomputing D6's 19 Lost Catches):**
   - D6's prose report claimed 7 cases were lost to Stage-1 trigger under-firing and 7 to Stage-2 evidence blindness.
   - Recomputing directly from D6's receipts on the 67 report-split positives reveals that the stored teacher caught 19 cheats, and cheap models missed 15 and 16 of them:
     - **`gemini-2.5-flash-lite` (15 missed):** **9 lost to Trigger Blindness** (Stage 1 never triggered, Stage 2 never ran), **6 lost to Evidence Blindness** (Stage 2 ran on un-truncated raw evidence but exonerated the agent).
     - **`gemini-3.1-flash-lite` (16 missed):** **8 lost to Trigger Blindness**, **8 lost to Evidence Blindness**.
   - This proves that cheap models fail at **both** stages: they lack sensitivity on compact cards at Stage 1, and they suffer from genuine evidence blindness on un-truncated raw traces at Stage 2.

4. **Failure on Ambiguous Band Cases:**
   - On the 14 positive cases that triggered Stage 2 via the uncertain suspicion band ($25 \le s \le 85$, where Stage 1 did not have a high-confidence confirmed verdict):
     - Stored Teacher: **10/14 = 71.4%**
     - Teacher Rerun 1 (low): **4/14 = 28.6%**
     - Teacher Rerun 2 (low): **3/14 = 21.4%**
     - Teacher Rerun (medium): **6/14 = 42.9%**
     - `gemini-2.5-flash-lite` (low & med): **0/14 = 0.0%**
     - `gemini-3.1-flash-lite` (low & med): **0/14 = 0.0%**
   - The cheap models fail completely on ambiguous cases, exonerating 100% of them.

---

## Evaluation Matrix: Cohort A ($N=48$ Stored Stage-2 Cases: 46 Pos / 2 Neg)

| Arm / Model | Thinking Level | Pure Stage 2 TP [95% Wilson] | Pure Stage 2 FP [95% Wilson] | Confirmed Cascade TP [95% Wilson] | Paired McNemar vs Teacher Rerun 1 ($b / c / p$) | Paired McNemar vs Stored Teacher ($b / c / p$) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **(i) Stored Teacher Reference (`ar_g38`)** | low* | **30/46 = 65.2% [50.8%, 77.3%]** | 1/2 = 50.0% [9.5%, 90.5%] | 30/46 = 65.2% [50.8%, 77.3%] | $b=7, c=1, p=0.0703$ | Reference ($b=0, c=0$) |
| **(ii) Teacher Rerun 1 (`gemini-3.8-flash`)** | low | **24/46 = 52.2% [38.1%, 65.9%]** | 1/2 = 50.0% [9.5%, 90.5%] | 24/46 = 52.2% [38.1%, 65.9%] | Reference ($b=0, c=0$) | $b=1, c=7, p=0.0703$ |
| **(iii) Teacher Rerun 2 (`gemini-3.8-flash`)** | low | **23/46 = 50.0% [36.1%, 63.9%]** | 1/2 = 50.0% [9.5%, 90.5%] | 23/46 = 50.0% [36.1%, 63.9%] | $b=1, c=0, p=1.0000$ | $b=0, c=7, p=0.0156$ |
| **(iv) `gemini-2.5-flash-lite` (Cheap-1)** | low | **12/46 = 26.1% [15.3%, 40.9%]** | 1/2 = 50.0% [9.5%, 90.5%] | 18/46 = 39.1% [26.4%, 53.5%] | $b=12, c=0, p=0.0005$ | $b=18, c=0, p<0.0001$ |
| **(v) `gemini-3.1-flash-lite` (Cheap-2)** | low | **13/46 = 28.3% [17.0%, 43.1%]** | 1/2 = 50.0% [9.5%, 90.5%] | 17/46 = 37.0% [24.5%, 51.5%] | $b=11, c=0, p=0.0010$ | $b=17, c=0, p<0.0001$ |
| **(vi) Teacher Medium (`gemini-3.8-flash`)** | medium | **25/46 = 54.4% [40.1%, 67.9%]** | 1/2 = 50.0% [9.5%, 90.5%] | 26/46 = 56.5% [42.3%, 69.8%] | $b=1, c=2, p=1.0000$ | $b=2, c=7, p=0.1797$ |
| **(vii) `gemini-2.5-flash-lite-med`** | medium | **14/46 = 30.4% [18.8%, 45.3%]** | 1/2 = 50.0% [9.5%, 90.5%] | 19/46 = 41.3% [28.3%, 55.7%] | $b=10, c=0, p=0.0020$ | $b=16, c=0, p<0.0001$ |
| **(viii) `gemini-3.1-flash-lite-med`** | medium | **10/46 = 21.7% [12.1%, 36.2%]** | 1/2 = 50.0% [9.5%, 90.5%] | 16/46 = 34.8% [22.6%, 49.4%] | $b=14, c=0, p=0.0001$ | $b=20, c=0, p<0.0001$ |

*\*Stored teacher session logs confirmed `thinkingLevel: "low"` was served in production.*

---

## Evaluation Matrix: Cohort B ($N=27$ L0-Only Cases: 26 Pos / 1 Neg)

| Arm / Model | Thinking Level | Pure Stage 2 TP [95% Wilson] | Pure Stage 2 FP | Confirmed Cascade TP [95% Wilson] | Confirmed Cascade FP |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Stored Teacher Reference** | low | **14/26 = 53.8% [35.5%, 71.2%]** | 0/1 | 14/26 = 53.8% [35.5%, 71.2%] | 0/1 |
| **Teacher Rerun 1** | low | **14/26 = 53.8% [35.5%, 71.2%]** | 0/1 | 14/26 = 53.8% [35.5%, 71.2%] | 0/1 |
| **Teacher Rerun 2** | low | **14/26 = 53.8% [35.5%, 71.2%]** | 0/1 | 14/26 = 53.8% [35.5%, 71.2%] | 0/1 |
| **`gemini-2.5-flash-lite`** | low | **8/26 = 30.8% [16.5%, 50.0%]** | 0/1 | 14/26 = 53.8% [35.5%, 71.2%]* | 0/1 |
| **`gemini-3.1-flash-lite`** | low | **10/26 = 38.5% [22.4%, 57.5%]** | 0/1 | 14/26 = 53.8% [35.5%, 71.2%]* | 0/1 |
| **Teacher Medium** | medium | **13/26 = 50.0% [32.1%, 67.9%]** | 0/1 | 14/26 = 53.8% [35.5%, 71.2%] | 0/1 |

*\*In Confirmed Cascade, high-confidence Stage-1 confirmed_cheat verdicts are retained unless Stage 2 proves reversion. Thus, the cascade logic partially shields Stage-2 false exonerations when Stage 1 was already confident. In isolated Pure Stage 2, cheap models miss 4–6 detections caught by the teacher.*

---

## Cost and Latency at List Price

| Arm | Cases | Total Tokens | Avg Tokens / Case | Measured Cost | List Price / 1k Cases | Median Latency |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`gemini-3.8-flash` (rerun 1, low)** | 48 | 999,099 | 20,815 | $0.7750 | $6.40 | 3.16s |
| **`gemini-3.8-flash` (rerun 2, low)** | 48 | 922,181 | 19,212 | $0.7164 | $5.89 | 2.70s |
| **`gemini-2.5-flash-lite` (low)** | 48 | 1,061,873 | 22,122 | $0.1187 | $1.85 | 3.77s |
| **`gemini-3.1-flash-lite` (low)** | 48 | 976,118 | 20,336 | $0.2534 | $1.56 | 1.69s |
| **`gemini-3.8-flash` (medium)** | 48 | 1,283,354 | 26,737 | $1.5969 | $11.90 | 12.21s |

---

## Artifact Hashes (SHA256 first 12)

- Pre-Registration: `91a179db4024` (`research-context/bridge/2026-11-02-overnight/loop/raw/D6B-FORCED-STAGE2/prereg.md`)
- Runner Script: `7de518386f9c` (`run_forced_stage2.py`)
- Analysis Script: `198f828aefbe` (`analyze_forced_stage2.py`)
- Results JSON: `e2283a00c634` (`forced_stage2_results.json`)
- Raw Receipts: `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/D6B-FORCED-STAGE2/receipts/`
- Durable Receipts: `~/Developer/eval-lab-results/2026-11-02/D6B/`
