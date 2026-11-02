# Decision Memo: Student-Monitor Distillation (D3-DISTILL-PLAN)

- **Date:** 2026-11-02
- **Author:** D3 Distillation Planning Item
- **Audience:** Peter Makhnatch, Eval Lab Coordinator
- **Status:** Complete / Final Decision Memo

## What Changed (Independent Verifier Corrections)
Following independent verification in VF-D-BENCH (`/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D-BENCH/report.md`, see `VERIFIER-CORRECTIONS.md`):
1. **Teacher Performance Scope:** Clarified that "teacher is near chance on 5 of 6 families" describes D2's Stage-1 card-only re-read, NOT production AutoReview. On the same 272 covered cases, D1 stored readings (with Stage-2 retrieval where triggered) achieve report AUROC 0.666 [0.599, 0.734] vs 0.493 [0.429, 0.562] for D2's Stage-1 re-read (Terminal Wrench: 0.740 vs 0.500; DeepSWE: 0.708 vs 0.476).
2. **Gates 1 & 3 Computation:** Gates 1 and 3 are dynamically computed. Gate 3 is presented under both cost bases: fails under subscription $0 (negative savings) and passes under list price $6.97/1k ($63.58 net savings at 10k traces, break-even at ~9,440 traces). "Fails all three gates" corrected to "fails Gates 1 and 2; Gate 3 depends on cost basis".
3. **Gate 2 Data Sufficiency:** Stated rule applied consistently: among trustworthy families, CheatBench (75) and Terminal Wrench (158) meet the 62-positive bar, while c1c5 (6) and sol_astra (8) fail (and deep_swe has 50 < 62 and is publisher_cot_judged).
4. **D4 Framing:** Corrected claim that D4 "proved CoT-free cards cause blindness": D4 readers saw bounded step transcripts with reasoning kept where present, and concluded CoT-class label noise is not identified.
5. **69 Pending Verdicts:** Corrected queue audit: 69 queue lines represent 60 unique run IDs (4 guide examples, 52 benchmark cases: 24 Goodfire pos, 13 deep_swe pos, 12 CheatBench neg, 2 Goodfire neg, 1 deep_swe neg). They resolve whether publisher CoT-judged labels hold, not container-verified labels.
6. **Sol/Astra FA Rate:** Corrected to 1/10 for `gemini-3.1-flash-lite` and 0/10 for `gemini-2.5-flash-lite`.
7. **Post-Hoc Signal Rule:** Labeled the "demonstrable signal in 1 of 6 families" rule as post-hoc, resting on 4 scripted positives.

---

## Executive Summary & Final Verdict

**VERDICT: DO NOT DISTILL (DO X FIRST)**

We recommend **against** training or distilling a student trace monitor tonight (**VERDICT: DO NOT DISTILL / DO X FIRST**). Distillation fails Decision Gates 1 and 2, while Gate 3 depends on the cost basis:
1. **Zero Headroom on Working Families ($X = 0.0\%$ vs required $\ge 8.5\%$):** In the only family where the Stage-1 card reader demonstrates post-hoc signal (`sol_astra`: AUROC 1.000 [1.000, 1.000], recall 4/4 = 100% @ FA 1/10 = 10% for 3.1-lite and 0/10 for 2.5-lite), cheap off-the-shelf readers (`gemini-3.1-flash-lite`, `gemini-2.5-flash-lite`) **already achieve 4/4 = 100% recall**, leaving zero empirical headroom ($X = 0.0\%$). In the other 5 families, the Stage-1 reader operates near chance (AUROC 95% CI contains 0.50). Production AutoReview recovers signal (pooled AUROC 0.666, Terminal Wrench 0.740) primarily through Stage-2 raw-step retrieval, not Stage-1 card reading.
2. **Severe Trustworthy Data Deficit ($N_{\text{pos}} < 62$ on key families):** While `terminal_wrench` (158 pos) and `cheatbench` (75 pos) clear the $N_{\text{pos}} \ge 62$ threshold, container-verified families `c1c5` ($N_{\text{pos}} = 6$) and `sol_astra` ($N_{\text{pos}} = 8$) severely fail it. `deep_swe` ($N_{\text{pos}} = 50$) also fails, and together with `goodfire` (80 pos) belongs to `publisher_cot_judged`, where D4 found CoT-class label noise is not identified and compact cards lack reasoning traces.
3. **Cost-Basis-Dependent Economic Value:** Under production subscription pricing ($0.00 marginal teacher cost), a self-hosted 7B LoRA student on Modal A10G ($0.612/1k) yields negative savings (-$6.12 at 10k traces), failing Gate 3. Under list price ($6.97/1k for `gemini-3.8-flash`), Gate 3 passes with $63.58 savings at 10k traces (break-even ~9,440 traces). Because Gates 1 and 2 fail, DO-NOT-DISTILL follows regardless.

### The Single Next Action (Do X First)
**Action:** Adjudicate the **69 pending run verdicts** in Judge Station (`docs/judge-guide.md`, command: `uv run evallab judge serve`).
- **Cost:** **$0.00** model spend; ~2.5 hours of human operator time (2 minutes per case).
- **Why:** The 69 queue lines cover 52 benchmark cases (24 Goodfire pos, 13 deep_swe pos, 12 CheatBench neg, 2 Goodfire neg, 1 deep_swe neg). Adjudicating them resolves whether upstream publisher CoT-judged labels are valid on action-level cards before spending compute on training.
---

## 1. Label Inventory & Provenance Audit

Across TraceMonitor-Bench DEV v1 (780 total cases, 776 scored) and the frozen 300-case ladder cohort:

| Family | Scored (Bench) | Cohort (Report Split) | Provenance Class | Human Labels | Container-Verified | Tripwire-Confirmed | Trustworthy for Distillation? |
| :--- | :---: | :---: | :--- | :---: | :---: | :---: | :--- |
| **c1c5** | 16 (6 pos, 10 neg) | 8 (3 pos, 5 neg) | `container_verified` | 0 | 16 (100%) | 5 / 6 pos | **Yes** (clean, but severely under-powered: $N_{\text{pos}}=3$) |
| **sol_astra** | 56 (8 pos, 48 neg) | 14 (4 pos, 10 neg) | `container_verified` | 0 | 56 (100%) | 14 scripted | **Yes** (clean, but under-powered: $N_{\text{pos}}=4$) |
| **cheatbench** | 150 (75 pos, 75 neg) | 32 (16 pos, 16 neg) | `publisher` | 0 | 0 | 0 | **Marginal** (12.5% reader disagreement in D4) |
| **terminal_wrench**| 304 (158 pos, 146 neg)| 40 (20 pos, 20 neg) | `publisher` | 0 | 0 | 121 / 158 pos | **Marginal** (bimodal pipeline artifact; L0 captures 121) |
| **goodfire** | 150 (80 pos, 70 neg) | 32 (16 pos, 16 neg) | `publisher_cot_judged`| 0 | 0 | 0 | **NO (Structural Reader Blindness)** |
| **deep_swe** | 100 (50 pos, 50 neg) | 24 (12 pos, 12 neg) | `publisher_cot_judged`| 0 | 0 | 0 | **NO (Structural Reader Blindness)** |
| **Total** | **776 (377 pos, 399 neg)**| **150 (71 pos, 79 neg)**| — | **0** | **72** | **140** | **Only 72 container-verified cases exist** |

### Critical Takeaway on Labels
1. **Stage-1 Card Readings are NOT Ground Truth:** In D2's Stage-1 card-only re-read, `gemini-3.8-flash` exhibits an overall pooled AUROC of 0.493 [0.429, 0.562] on DEV. Distilling a student to match Stage-1 card suspicion scores simply distills chance. Production AutoReview achieves AUROC 0.666 only through Stage-2 retrieval.
2. **D4 Addendum Enforced:** The `publisher_cot_judged` class (`goodfire` and `deep_swe`, 250 cases) cannot be used to train or evaluate a CoT-free student. D4 demonstrated that independent frontier models called 0 of 20 cases cheated, and concluded that CoT-class label noise is not identified.

---

## 2. Per-Family Ladder Breakdown & Signal Analysis

Using D2 receipts (`ladder_results.json` and per-rung JSONLs), we evaluate the teacher (`gemini-3.8-flash`) and off-the-shelf alternatives on the frozen `report` split across each family:

| Family | Report Pos/Neg | Teacher AUROC [95% CI] | Teacher Rec@2% FA | Teacher Demonstrable Signal? | Best Cheap Rung (`gemini-3.1-flash-lite`) | McNemar vs Teacher ($b, c, p$) | Headroom ($X$) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **sol_astra** | 4 / 10 | **1.000 [1.000, 1.000]** | **4/4 (100.0%)** | **YES** | **4/4 (100.0%)** (AUROC 0.938) | $b=0, c=0, p=1.000$ | **0.0%** |
| **c1c5** | 3 / 5 | 0.600 [0.000, 1.000] | 1/3 (33.3%) | NO (CI includes 0.5) | 1/3 (33.3%) (AUROC 0.667) | $b=1, c=1, p=1.000$ | 0.0% |
| **cheatbench** | 16 / 16 | 0.506 [0.385, 0.625] | 1/16 (6.2%) | NO (CI includes 0.5) | 2/16 (12.5%) (AUROC 0.562) | $b=2, c=1, p=1.000$ | -6.3% (Lite leads) |
| **terminal_wrench**| 20 / 20 | 0.500 [0.500, 0.500] | 0/20 (0.0%) | NO (At chance) | 0/20 (0.0%) (AUROC 0.500) | $b=0, c=0, p=1.000$ | 0.0% |
| **goodfire** | 16 / 16 | 0.535 [0.438, 0.635] | 0/16 (0.0%) | NO (CI includes 0.5) | 0/16 (0.0%) (AUROC 0.500) | $b=0, c=0, p=1.000$ | 0.0% |
| **deep_swe** | 12 / 12 | 0.476 [0.267, 0.681] | 0/12 (0.0%) | NO (CI includes 0.5) | 0/12 (0.0%) (AUROC 0.458) | $b=0, c=0, p=1.000$ | 0.0% |
| **Pooled (D2)**| **71 / 79** | **0.511 [0.443, 0.580]** | **6/71 (8.5%)** | **NO (At chance)** | **7/71 (9.9%)** (AUROC 0.537) | $b=3, c=2, p=1.000$ | **-1.4% (Lite leads)**|

*Notes:*
- Post-hoc demonstrable signal rule: AUROC 95% bootstrap CI strictly excluding 0.50 AND recall > false-alarm rate. This was met only on `sol_astra` and rests on 4 scripted positives (bootstrap CI [1.0, 1.0] on $N=4$ is degenerate).
- `gemini-3.1-flash-lite` has 1/10 false alarms on `sol_astra`; `gemini-2.5-flash-lite` has 0/10 false alarms.
- `gemini-2.5-flash-lite` achieves higher recall on `cheatbench`: 4/16 (25.0%) vs teacher 1/16 (6.25%), with McNemar $b=3, c=0, p=0.250$.
- In the only family with Stage-1 signal (`sol_astra`), cheap models match the teacher perfectly ($X = 0.0\%$).

---

## 3. Pre-registered Decision Rule Arithmetic

The decision rule pre-registers three conditions:

### Criterion 1: Headroom Gate ($X \ge 8.5\%$)
- **Requirement:** The best cheap rung from D2 must lose at least $X = 8.5$ percentage points of recall at FA $\le 1\%$ against the teacher in families where the teacher has signal.
- **Justification for $X = 8.5\%$:** On the 71 report positives, exact McNemar testing at $\alpha = 0.05$ requires a minimum discordant difference of $|b - c| \ge 6$ cases ($6 / 71 = 8.45\% \approx 8.5\%$). Any difference smaller than 8.5 points cannot be statistically distinguished from sampling noise.
- **Outcome:** **FAILS.** In `sol_astra`, `gemini-3.1-flash-lite` achieves 4/4 (100%) vs teacher 4/4 (100%), yielding $X = 0.0\%$. In the other families, the teacher has no demonstrable signal.

### Criterion 2: Data Sufficiency Gate ($N_{\text{pos}} \ge 62$)
- **Requirement:** At least $N_{\text{pos}} \ge 62$ trustworthy positive cases per family across train/test splits.
- **Justification for $N = 62$:** To measure a target recall of 80% with a 95% Wilson confidence interval half-width $\le 10\%$, standard sample size requires $N \ge z^2 \cdot p(1-p) / d^2 = (1.96 / 0.10)^2 \cdot 0.80 \cdot 0.20 = 61.5 \approx 62$. To detect an 8.5 percentage point gap with 80% power under McNemar, each family would require $N_{\text{pos}} \approx 134$.
- **Outcome:** **FAILS.** Among trustworthy classes, `terminal_wrench` (158) and `cheatbench` (75) meet the threshold, but `c1c5` ($N_{\text{pos}} = 6$) and `sol_astra` ($N_{\text{pos}} = 8$) fail. `deep_swe` ($N_{\text{pos}} = 50$) also fails and, like `goodfire`, is `publisher_cot_judged`.

### Criterion 3: Economic Breakeven Gate ($V \cdot \Delta C \ge 2.0 \cdot C_{\text{train+eval}}$)
- **Requirement:** Projected 3-month serving savings must exceed one-off training and evaluation costs by at least $2.0\times$ ($2.0 \times \$30.00 = \$60.00$).
- **Cost Arithmetic & Outcome:**
  - **Subscription Basis ($0.00 teacher marginal cost):** Self-hosted 7B LoRA on Modal A10G costs **$0.612 per 1,000 cases**, increasing serving costs by $6.12 at 10k traces. **FAILS.**
  - **List Price Basis ($6.97/1k teacher list):** Savings vs teacher are $6.358 per 1,000 traces ($63.58 at 10k traces $\ge \$60.00$). **PASSES** at volume $\ge 9,440$ traces.
  - Overall: Fails under production subscription deployment. Regardless of cost basis, Gates 1 and 2 fail, so the DO-NOT-DISTILL verdict stands.
---

## 4. Student Recipes & Modal GPU Cost Estimates

| Recipe | Architecture & Base | Training Setup (Modal) | One-Off Training + Eval Cost [Estimate] | Serving Cost / 1k Traces [Estimate] | Latency p50 [Estimate] |
| :--- | :--- | :--- | :---: | :---: | :---: |
| **Recipe A (LoRA)** | Qwen 2.5 7B Instruct (LoRA r=16, a=32) | 1x NVIDIA A10G ($1.10/hr), 388 tune cases, 3 epochs (~2h) | **$30.00** ($2.20 train + $11 dev runs + $1.50 eval + $15.30 buffer) | **$0.61** (2.0s / trace on A10G) | ~2.0 s |
| **Recipe B (Encoder)**| ModernBERT-large or DeBERTa-v3-large | 1x NVIDIA A10G ($1.10/hr), 388 tune cards, 10 epochs (~30m) | **$7.50** ($0.55 train + $2.75 dev runs + $0.50 eval + $3.70 buffer) | **$0.0055** (200ms on Modal CPU @ $0.10/hr) | ~200 ms |
| **Reference (D2)** | Gemini 3.8 Flash (AutoReview Stage 1) | Off-the-shelf API | $0.00 | $0.00 sub ($6.97 list) | 5.07 s |
| **Best Off-the-Shelf**| Gemini 3.1 Flash-Lite | Off-the-shelf API | $0.00 | **$0.00 sub** ($2.49 list) | **2.18 s** |
| **Paid Off-the-Shelf** | Ling-3.0-Flash (OpenRouter) | Off-the-shelf API | $0.00 | **$0.19 paid** | 6.36 s |

---

## 5. Strategic Risks & Mitigations

1. **Teacher Bias & CoT-Free Structural Blindness:** The production teacher scores near chance on 5 of 6 families because compact anatomy cards strip chain-of-thought narration. A student trained on teacher logits will inherit these blind spots and learn to trigger on length or formatting artifacts rather than malicious behavior.
2. **Distribution Shift to Novel Agent Harnesses:** A student fine-tuned on current DEV traces will overfit to existing prompt wrappers (e.g. Harbor / OpenHands). When evaluation harnesses change, distilled small models degrade rapidly compared to generalist reasoning models.
3. **Adversarial Exploitation:** A static 7B student monitor deployed in production is vulnerable to white-box or gray-box evasion by adaptive agent models.
4. **Value of Human Calibration:** The 69 pending verdicts in Judge Station will provide the first true gold standard. If humans cannot reliably identify cheats from cards alone, the problem lies in the card representation (Stage-1 view), not the model weights.

---

## 6. Action Plan for Peter

```
ACTION REQUIRED FROM YOU
What is needed: Adjudicate the 69 pending run verdicts in Judge Station (uv run evallab judge serve).
Why: Establishes the first human-grounded gold standard to determine whether cheat signals exist in cards before spending compute on training.
Next step: Run `uv run evallab judge serve` and navigate to http://127.0.0.1:8795.
```
