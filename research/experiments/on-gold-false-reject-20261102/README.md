# A4C-FALSE-REJECT-FIX: Remedies for Gold v1.1 Over-Strict Grader Tests

## Overview
This experiment investigates and resolves the over-strict test phenomena discovered in Gold v1.1 (A4B), where hardened tests falsely rejected real frontier agent submissions accepted by the original benchmark grader (observed in A8B: 5/9, and VF-A4B: 7/12).

Two deterministic filtering remedies are implemented and evaluated:
1. **R1 (Reference-Only Symbol Filter):** Static AST analysis that computes identifiers introduced by the human reference patch (new function, method, class, attribute, and config-key names), subtracts identifiers present in the task specification or pre-patch repository, and drops test functions in the contract suite whose AST references any remaining reference-only symbol.
2. **R2 (Multi-Positive Leave-One-Out Filter):** Dynamic per-submission filtering requiring test functions to pass the human reference AND every other original-accepted real submission for that task ($s' \ne s$).
3. **S3 (R1 + R2 Combined):** Sequential application of R1 followed by R2.

## Experimental Arms
- **S0 (Baseline):** A4B shipped test suites across all 26 accepted tasks.
- **S1 (R1 only):** Shipped test suites filtered by reference-only symbols.
- **S2 (R2 only):** Shipped test suites filtered by leave-one-out multi-positive executions.
- **S3 (R1 + R2):** Combined filter.

## Summary Results

| Metric / Cohort | S0 (Baseline) | S1 (R1 only) | S2 (R2 only) | S3 (R1 + R2) |
| :--- | :---: | :---: | :---: | :---: |
| **Real Submission Rejection Rate** | 7/12 = 58.3% (32.0–80.7) | 3/12 = 25.0% (8.9–53.2) | 2/12 = 16.7% (4.7–44.8) | 1/12 = 8.3% (1.5–35.4) |
| **Clear Over-Strict Rejections (002391)** | 3 / 3 remaining | **0 / 3 remaining (100% fixed)** | **0 / 3 remaining (100% fixed)** | **0 / 3 remaining (100% fixed)** |
| **Retained Weak-Grader Demonstrations** | 8/8 = 100.0% (67.6–100.0) | 7/8 = 87.5% (52.9–97.8) | 8/8 = 100.0% (67.6–100.0) | 7/8 = 87.5% (52.9–97.8) |
| **Retained Unseen EVAL Mutant Kills** | 12/12 = 100.0% (75.8–100.0) | 12/12 = 100.0% (75.8–100.0) | 12/12 = 100.0% (75.8–100.0) | 12/12 = 100.0% (75.8–100.0) |
| **Tasks Maintaining Acceptance (>=3 fns)** | 26 / 26 (100.0%) | 26 / 26 (100.0%) | 26 / 26 (100.0%) | 26 / 26 (100.0%) |
| **Paired McNemar vs S0 (b, c, p)** | — | b=4, c=0, p=0.1250 | b=5, c=0, p=0.0625 | b=6, c=0, **p=0.0312** |
| **Pre-Registered Decision Gate** | — | **WORTH ADOPTING** | **WORTH ADOPTING** | **WORTH ADOPTING** |

## Key Insights
1. **R1 completely eliminates the clear over-strict rejections:** On task `format-code-task-002391`, R1 identifies `alias_of` and `merge_aliases` as reference-only symbols and drops the exact 4 test functions that asserted them. All 3 previously rejected submissions pass under live Modal execution (`reward == 1.0`).
2. **Efficacy is preserved:** All 12 of 12 unseen EVAL mutant kills survive under R1, R2, and S3. Weak-grader demonstration kills retain 7/8 (R1/S3) or 8/8 (R2).
3. **Bug-sharing trap in Multi-Positive Filtering:** On task `format-code-task-001809`, both real submissions failed to support `BinaryValue` attributes (a legitimate shared bug). Under R2 leave-one-out, each submission's failure causes the test catching the bug to be dropped for the other submission, creating a false accept. Static symbol filtering (R1) correctly preserves the test because `BinaryValue` existed in the pre-patch repository.

## Reproduction
```bash
uv run --extra laminar python research/experiments/on-gold-false-reject-20261102/run_experiments.py
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --no-sync python -m pytest -q -p no:warnings research/experiments/on-gold-false-reject-20261102/test_false_reject.py
```
