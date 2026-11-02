# Student-Monitor Distillation Plan (D3-DISTILL-PLAN)

Decision memo, experimental protocol, and statistical arithmetic evaluating whether to distill a student trace monitor for cheat detection.


## What Changed (Independent Verifier Corrections)
Following blind verification in VF-D-BENCH (`/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D-BENCH/report.md`, see `VERIFIER-CORRECTIONS.md`):
1. **Teacher Scope:** Clarified that "teacher lacks signal on 5 of 6 families" describes D2's Stage-1 card-only re-read (pooled AUROC 0.493), not production AutoReview (pooled AUROC 0.666 with Stage-2 retrieval; Terminal Wrench 0.740 vs 0.500; DeepSWE 0.708 vs 0.476).
2. **Dynamic Gate Computation:** Gates 1 and 3 in `distill_arithmetic.py` are dynamically computed rather than hardcoded. Gate 3 is presented under both cost bases (subscription $0: fails; list price $6.97/1k: passes at $\ge 9,440$ traces). "Fails all three gates" corrected to "fails Gates 1 and 2; Gate 3 depends on cost basis".
3. **Gate 2 Data Sufficiency:** Stated rule applied: trustworthy families `cheatbench` (75 pos) and `terminal_wrench` (158 pos) meet the 62-positive bar, while `c1c5` (6 pos) and `sol_astra` (8 pos) fail, and `deep_swe` (50 pos) falls below 62 and is `publisher_cot_judged`.
4. **D4 Framing:** Corrected claim that D4 "proved CoT-free cards cause blindness": D4 readers saw bounded step transcripts with reasoning kept where present, and concluded CoT-class label noise is not identified.
5. **69 Pending Verdicts:** Corrected queue audit: 69 queue lines represent 60 unique run IDs (52 benchmark cases: 24 Goodfire pos, 13 deep_swe pos, 12 CheatBench neg, 2 Goodfire neg, 1 deep_swe neg) resolving CoT-judged label validity.
6. **Sol/Astra FA Rate:** Corrected to 1/10 for `gemini-3.1-flash-lite` and 0/10 for `gemini-2.5-flash-lite`.
7. **Post-Hoc Signal Rule:** Labeled the "demonstrable signal in 1 of 6 families" rule as post-hoc, resting on 4 scripted positives.
## Verdict

**DO NOT DISTILL (DO X FIRST).**

Distillation is rejected because:
1. **Stage-1 Card Teacher Lacks Signal:** In 5 of 6 benchmark families (`terminal_wrench`, `cheatbench`, `goodfire`, `deep_swe`, `c1c5`), the Stage-1 card reader (`gemini-3.8-flash`) operates near chance (AUROC 95% bootstrap CI contains 0.50, pooled AUROC 0.493). Production AutoReview recovers signal (pooled AUROC 0.666, Terminal Wrench 0.740) primarily through Stage-2 raw-step retrieval, not Stage-1 card reading.
2. **Zero Headroom on Working Families ($X = 0.0\%$):** In the only family where Stage-1 shows signal (`sol_astra`: post-hoc rule, 4 scripted positives), cheap off-the-shelf readers (`gemini-3.1-flash-lite` with 1/10 FA, `gemini-2.5-flash-lite` with 0/10 FA) already achieve 100% recall (4/4), leaving zero headroom to justify fine-tuning.
3. **Severe Trustworthy Data Deficit:** While `terminal_wrench` (158 pos) and `cheatbench` (75 pos) meet the $N_{\text{pos}} \ge 62$ bar, container-verified families `c1c5` (6 pos) and `sol_astra` (8 pos) severely fail it. `deep_swe` (50 pos) also falls below 62 and, like `goodfire`, is `publisher_cot_judged` where D4 found CoT-class noise is not identified. 0 human-calibrated labels exist (69 pending Judge Station queue lines).
4. **Cost-Basis-Dependent Economic Value:** Fails on subscription basis ($0.00 teacher marginal cost: student serving $0.612/1k increases costs by $6.12 at 10k traces). Passes on list price basis ($6.97/1k teacher list: savings $63.58 at 10k traces, break-even at ~9,440 traces).

**Single Next Action:** Adjudicate the 69 pending human verdicts in Judge Station (`docs/judge-guide.md`, `uv run evallab judge serve`) at $0 model cost to test whether publisher CoT-judged labels hold on action cards.

---

## Directory Structure

- `memo.md`: One-page executive decision memo for Peter.
- `protocol.md`: Technical training and evaluation protocol, leakage controls, recipes, and stop rules.
- `distill_arithmetic.py`: Executable calculation script reproducing all numbers from D1 and D2 receipts.
- `stats.py`: Vendored overnight statistics library (exact McNemar, Wilson score, bootstrap AUROC).
- `results.json`: Machine-readable results and gate evaluation.
- `test_distill_plan.py`: Targeted unit tests for arithmetic and decision invariants.

---

## Key Results Summary

### 1. Label Inventory (TraceMonitor-Bench DEV v1, 776 scored)
- `container_verified`: 72 cases (`c1c5`: 16, `sol_astra`: 56).
- `publisher`: 454 cases (`terminal_wrench`: 304, `cheatbench`: 150).
- `publisher_cot_judged`: 250 cases (`goodfire`: 150, `deep_swe`: 100). Structural reader blindness per D4; excluded from distillation training.
- `human_labels`: 0 (69 pending in Judge Station).

### 2. Teacher Signal Analysis (Report Split)
- `sol_astra`: AUROC 1.000 [1.000, 1.000], Recall@2% = 4/4 (100.0%) -> **Signal Present**. Cheap reader matches at 4/4 (100.0%).
- `c1c5`: AUROC 0.600 [0.000, 1.000], Recall@2% = 1/3 (33.3%) -> **At Chance**.
- `cheatbench`: AUROC 0.506 [0.385, 0.625], Recall@2% = 1/16 (6.2%) -> **At Chance**.
- `terminal_wrench`: AUROC 0.500 [0.500, 0.500], Recall@2% = 0/20 (0.0%) -> **At Chance**.
- `goodfire`: AUROC 0.535 [0.438, 0.635], Recall@2% = 0/16 (0.0%) -> **At Chance**.
- `deep_swe`: AUROC 0.476 [0.267, 0.681], Recall@2% = 0/12 (0.0%) -> **At Chance**.

---

## Reproduction

Run the arithmetic script directly:
```bash
python distill_arithmetic.py
```

Run targeted tests:
```bash
python -m pytest test_distill_plan.py -q
```
