# A6-STATIC-VS-EXEC: Cheap Static Task QA vs Executable Grader Weakness

## Overview
This experiment evaluates whether cheap, static task QA—specifically rule-based structural features from `PackageAnatomy` and zero-tool single-turn LLM audits (`google-antigravity/gemini-3.8-flash`)—can predict executable grader weakness (as established by the A2 STING mutation audit).

## Preregistration
Preregistration was sealed prior to reading or joining any A2 ground truth labels:
- Preregistration file: `cert/raw/A6-STATIC-VS-EXEC/prereg.md`
- Preregistration sha256[:12]: `d16c63181115`
- Full sha256: `d16c63181115b92ca9014cd94d10617b5d368e04807b6f0c4f8bf3b0002ee7af`

## Cohort
- 28 DEV tasks from `census.json` audited by A2 without control failure (`control_status == "passed"`).
- Ground truth: 18 weak tasks (mutant survivors > 0), 10 robust tasks (0 mutant survivors).
- Weakness rate: 18/28 = 64.3% (95% Wilson CI: 45.8% - 79.3%).

## Predictors (Label-Free)
1. **PackageAnatomy Structural Metrics:**
   - `anatomy_weak_hypotheses_count`: Count of `weak_check` vulnerability hypotheses.
   - `anatomy_max_weak_score`: Maximum score (0-100) among weak-check hypotheses.
   - `anatomy_sum_weak_score`: Sum of scores among weak-check hypotheses.
   - `inv_test_count`: `1 / (1 + tests)`.
   - `inv_assertions_per_test`: `1 / (1 + assertions_per_test)`.
   - `surface_assertion_share`: Share of assertions checking existence / surface properties.
   - `inv_value_assertion_share`: `1 - literal_eq_share` (share of non-value assertions).
2. **LLM Static Audit (`google-antigravity/gemini-3.8-flash`, attested, no tools):**
   - `llm_variant_s_score`: Instruction + tests only (0-100 score).
   - `llm_variant_r_score`: Instruction + reference patch + tests (0-100 score).
3. **Pre-Registered Fixed Composites:**
   - `composite_static_r`: `0.5 * llm_variant_r_score + 0.5 * anatomy_max_weak_score`.
   - `composite_static_s`: `0.5 * llm_variant_s_score + 0.5 * anatomy_max_weak_score`.

## Headline Summary

| Predictor | AUROC (95% Bootstrap CI) | P@3 | P@5 | P@10 | LOO Skip % | LOO Misses | Modal $ Saved |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `anatomy_weak_hypotheses_count` | 0.631 [0.414, 0.825] | 1.00 | 1.00 | 0.70 | 60.7% | 10 / 18 | $0.14 |
| `anatomy_max_weak_score` | 0.431 [0.194, 0.678] | 0.33 | 0.20 | 0.60 | 32.1% | 6 / 18 | $0.10 |
| `anatomy_sum_weak_score` | 0.631 [0.389, 0.856] | 0.67 | 0.60 | 0.70 | 35.7% | 5 / 18 | $0.10 |
| `inv_test_count` | 0.564 [0.300, 0.811] | 0.33 | 0.40 | 0.70 | 42.9% | 7 / 18 | $0.07 |
| `inv_assertions_per_test` | 0.481 [0.258, 0.711] | 0.67 | 0.40 | 0.70 | 35.7% | 6 / 18 | $0.08 |
| `surface_assertion_share` | 0.467 [0.300, 0.603] | 0.67 | 0.60 | 0.60 | 89.3% | 16 / 18 | $0.21 |
| `inv_value_assertion_share` | 0.611 [0.406, 0.822] | 0.67 | 0.80 | 0.80 | 35.7% | 4 / 18 | $0.06 |
| `llm_variant_s_score` | 0.478 [0.253, 0.708] | 0.33 | 0.40 | 0.70 | 39.3% | 7 / 18 | $0.07 |
| `llm_variant_r_score` | 0.558 [0.333, 0.775] | 0.67 | 0.80 | 0.70 | 39.3% | 7 / 18 | $0.09 |
| `composite_static_r` | 0.553 [0.322, 0.769] | 1.00 | 0.60 | 0.70 | 35.7% | 6 / 18 | $0.07 |
| `composite_static_s` | 0.461 [0.244, 0.694] | 0.67 | 0.60 | 0.60 | 35.7% | 6 / 18 | $0.06 |

## Key Findings & Primary Gate Verdict
- **Gate Verdict: NO.** No cheap static predictor achieves an AUROC whose 95% bootstrap confidence interval lower bound exceeds 0.50. The point estimates range from 0.431 to 0.631, and all confidence intervals span across 0.50.
- **Triage Failure:** When static scores are used to triage tasks under leave-one-out cross-validation (aiming to skip $\ge 30\%$ of tasks while permitting $\le 1$ training false negative), the static classifiers miss between 4 and 10 weak tasks out-of-fold (a 22.2% to 55.6% false-negative rate on weak graders).
- **Economic Inefficiency:** Attempting static triage saved between $0.06 and $0.14 across 28 tasks while leaking multiple broken graders.
- **Mechanism Failure:** When the LLM was asked to identify the 3 weakest assertions in Variant R, only 5 of 18 weak tasks (27.8%, 95% Wilson CI: 12.5% - 50.9%) pointed to code in the module where surviving mutants were located.
