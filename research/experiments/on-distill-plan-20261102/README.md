# Student-Monitor Distillation Plan (D3-DISTILL-PLAN)

Decision memo, experimental protocol, and statistical arithmetic evaluating whether to distill a student trace monitor for cheat detection.

## Verdict

**DO NOT DISTILL (DO X FIRST).**

Distillation is rejected because:
1. **Teacher Lacks Signal:** In 5 of 6 benchmark families (`terminal_wrench`, `cheatbench`, `goodfire`, `deep_swe`, `c1c5`), the reference teacher (`gemini-3.8-flash`) operates at chance (AUROC 95% bootstrap CI contains 0.50). Distilling teacher scores will inherit and amplify random noise.
2. **Zero Headroom on Working Families:** In the only family with demonstrable signal (`sol_astra`: AUROC 1.000 [1.000, 1.000]), cheap off-the-shelf readers (`gemini-3.1-flash-lite`, `gemini-2.5-flash-lite`) already achieve 100% recall (4/4) at 0% false alarms ($X = 0.0\%$).
3. **Severe Data Deficit:** 0 human-calibrated labels exist (69 Judge Station verdicts pending). Only 72 container-verified cases exist across the entire benchmark (14 positives: 6 in `c1c5`, 8 in `sol_astra`).
4. **Negative Economic Value:** Serving a 7B student model on Modal A10G costs ~$0.61/1k cases, which is 3.2x more expensive than off-the-shelf `ling-3.0-flash` ($0.19/1k) and infinitely more expensive than $0.00 marginal subscription cost for `gemini-3.1-flash-lite`.

**Single Next Action:** Adjudicate the 69 pending human verdicts in Judge Station (`docs/judge-guide.md`, `uv run evallab judge serve`) at $0 model cost.

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
