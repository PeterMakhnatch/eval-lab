# D9-PRODUCTION-STAGE1-CHEAP: Production Stage 1 Evaluation

## Overview
This experiment investigates the apparent conflict between:
1. **FIX-D23:** D2's Stage-1 card-only re-read of Gemini 3.8 Flash scored chance Report AUROC 0.4931 (recall 2/67), concluding that Stage 2 carries all signal.
2. **D7:** Stored production Stage 1 alone (Policy P4) scored Report AUROC 0.6762 (recall 21/67), showing Stage 1 alone carries the detection signal.
3. **VF-D6:** D6 evaluated cheap models using D2's card re-reads, never the production Stage 1.

## Findings
- **H1 Holds (PASS):** D2 evaluated models on summary anatomy cards (`render_anatomy_card`) that stripped the Action Index, tool calls, commands, exit codes, and backcheck tables. When evaluated on today's real production Stage-1 views (`build_compact_view`), Gemini 3.8 Flash Report AUROC recovers from **0.4931** to **0.6339 [0.5665, 0.6995]** and recall jumps from **2/67 (3.0%)** to **17/67 (25.4%)**, paired McNemar $p = 0.00027$.
- **H1' Refuted (No Code Drift):** Today's production Stage 1 code reproduces the stored baseline within run noise ($p = 0.3750$ vs T0). On 80 Terminal Wrench cases with recoverable prompt text, T2 scores AUROC 0.7425 vs T0 0.7562 ($p = 0.5000$).
- **H2 Refuted (Cheap Models Not Viable at Real Stage 1):** On real Stage-1 compact views, cheap models still fail decisively. `gemini-2.5-flash-lite` achieves only 10/67 recall (14.9%, retaining 58.8% of teacher recall, McNemar vs T1: $p = 0.0391$). `gemini-3.1-flash-lite` achieves only 6/67 recall (9.0%, retaining 35.3% of teacher recall, McNemar vs T1: $p = 0.00098$).

## Primary Arms Summary (Report Split, $N=136$: 67 Pos / 69 Neg)

| Arm | Model | AUROC [95% CI] | Tune Threshold | Recall @ FA $\le 2\%$ | False Alarms |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **(T0) Stored Reference** | `gemini-3.8-flash` (stored) | **0.6762 [0.6082, 0.7424]** | 28.0 | 20/67 = 29.8% | 2/69 = 2.9% |
| **(T1) Teacher Today** | `gemini-3.8-flash` (today S1) | **0.6339 [0.5665, 0.6995]** | 20.0 | 17/67 = 25.4% | 1/69 = 1.5% |
| **(C1) Cheap 2.5 Lite** | `gemini-2.5-flash-lite` (today S1) | **0.5766 [0.5300, 0.6288]** | 50.0 | 10/67 = 14.9% | 1/69 = 1.5% |
| **(C2) Cheap 3.1 Lite** | `gemini-3.1-flash-lite` (today S1) | **0.5308 [0.4934, 0.5685]** | 88.0 | 6/67 = 9.0% | 1/69 = 1.5% |
| **(Ref) D2 Teacher** | `gemini-3.8-flash` (D2 card) | **0.4931 [0.4289, 0.5620]** | 70.0 | 2/67 = 3.0% | 0/69 = 0.0% |

## Files
- `run_stage1_production.py`: Execution runner for T1, T2, C1, C2.
- `analyze_stage1_production.py`: Statistical analysis, out-of-fold threshold calibration, paired exact McNemar tests, L0 contribution, and JSON summary output.
- `test_stage1_production.py`: Automated pytest suite.
- `production_stage1_results.json`: Full numeric results (no per-case labels or transcript text).
- `receipts-manifest.json`: SHA256 hashes of all receipt artifacts.
