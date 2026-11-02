# A2B-EQUIV: Equivalence Check of A2 Surviving Mutants with Calibrated Blind LLM Judges

This experiment evaluates whether the 22 AST mutants that survived original MiMo graders in the A2 census (`research/experiments/on-weak-grader-census-20261102/`, PR #1746 merged) represent genuine semantic behavior changes vs functionally equivalent mutations (e.g., dead code, redundant guards, unreachable branches).

## Pre-Registration
- Pre-registration document: [`prereg.md`](./prereg.md)
- sha256: `af7eec5fa49502e8dfd977d5d51a372af267b9808d1b8ef158fe054264e4df2d` (`af7eec5fa495`)

## Methodology
- **Blind Cohort ($N = 74$):** All 22 surviving mutants from A2 (`reward == 1.0`) plus all 51 successfully applied mutants killed by original graders (`reward == 0.0`) as ground-truth positive controls for behavior change, plus 1 unexecuted mutant (evaluated blindly, excluded from calibration).
- **De-Identification:** Mutants were assigned opaque IDs (`MUT-01` through `MUT-74`) under deterministic shuffling (`seed=20261102`). All task IDs, file paths, test results, rewards, and survivor/killed labels were completely stripped.
- **Independent Blind Judges:**
  1. `google-antigravity/gemini-3.8-flash` (medium thinking)
  2. `google-antigravity/gemini-3.1-flash-lite` (medium thinking)
- **Attestation:** 100% of calls served requested model with 0 fallbacks (`resolvedModelIsFallback == false`).

## Results Summary
- **Control Calibration ($N = 51$ ground-truth killed mutants):**
  - Judge 1 (`gemini-3.8-flash`): Sensitivity = 51 / 51 = 100.0% (95% Wilson CI: 93.0%–100.0%)
  - Judge 2 (`gemini-3.1-flash-lite`): Sensitivity = 51 / 51 = 100.0% (95% Wilson CI: 93.0%–100.0%)
  - Consensus (Both): 51 / 51 = 100.0% (95% Wilson CI: 93.0%–100.0%)
  - Inter-rater agreement: Cohen's $\kappa = 1.0000$ (perfect agreement across all 74 items)
  - Uncertainty rate: 0 / 51 = 0.0%
- **Survivor Equivalence ($N = 22$ surviving mutants):**
  - Both judges independently classified 22 / 22 = 100.0% (95% Wilson CI: 85.1%–100.0%) of surviving mutants as genuine `behavior_changing` mutations.
  - Equivalent mutants: 0 / 22 = 0.0%.
- **Weak-Grader Prevalence (28 audited tasks):**
  - Definition (A) [Syntactic Survivor, A2 Baseline]: 18 / 28 = 64.3% (95% Wilson CI: 45.8%–79.3%)
  - Definition (B) [Consensus Semantic Survivor]: 18 / 28 = 64.3% (95% Wilson CI: 45.8%–79.3%)
  - Definition (C) [Union Semantic Survivor]: 18 / 28 = 64.3% (95% Wilson CI: 45.8%–79.3%)
  - Paired comparison: $b = 0, c = 0$, exact McNemar $p = 1.0000$.
- **Headline Answer:**
  - **18 / 18 = 100.0% (95% Wilson CI: 82.4%–100.0%)** of A2's weak-grader prevalence survives semantic equivalence checking. Grader weakness is not inflated by dead or equivalent code mutations.


## Specificity Addendum (FIX-A2B-SPECIFICITY)
Following peer review noting that 100% positive classifications on 74 items could reflect uncalibrated judge specificity, we pre-registered an addendum ([`prereg-addendum.md`](./prereg-addendum.md), sha256: `d11eec503f6d`) and evaluated 44 known-equivalent negative controls by construction from the same reference code and added lines as the 22 survivors (39 easy AST rewrites, 5 hard non-local equivalents).

Every negative control was verified by:
1. AST structural non-identity (`ast.dump(rewritten) != ast.dump(reference)`).
2. Empirical grader execution via Modal, scoring `reward == 1.0` (all 44 passed).

### Specificity Results ($N_{\text{ctrl}} = 44$)
- **Judge 1 (`gemini-3.8-flash`):** Specificity = **42 / 44 = 95.5% (95% Wilson CI: 84.9%–98.7%)**
  - Easy controls ($N = 39$): 37 / 39 = 94.9% (95% Wilson CI: 83.1%–98.6%)
  - Hard controls ($N = 5$): 5 / 5 = 100.0% (95% Wilson CI: 56.6%–100.0%)
- **Judge 2 (`gemini-3.1-flash-lite`):** Specificity = **43 / 44 = 97.7% (95% Wilson CI: 88.2%–99.6%)**
  - Easy controls ($N = 39$): 38 / 39 = 97.4% (95% Wilson CI: 86.8%–99.5%)
  - Hard controls ($N = 5$): 5 / 5 = 100.0% (95% Wilson CI: 56.6%–100.0%)
- **Consensus Specificity (Both):** **42 / 44 = 95.5% (95% Wilson CI: 84.9%–98.7%)**
- **Union Specificity (Either):** **43 / 44 = 97.7% (95% Wilson CI: 88.2%–99.6%)**
- **Gate Verdict:** **PASS (GATE MET)**. Point specificity $\ge 0.80$ and Wilson lower bound $\ge 0.60$ for both models.
- **Rerun Stability (74 original items):** 74 / 74 = 100.0% agreement with initial run under fresh opaque shuffling.
- **Dual Rogan-Gladen Correction:** Propagating measured $Se = 1.000$ (95% CI: 93.0%–100.0%) and $Sp = 0.955$ (95% CI: 84.9%–98.7%), the point estimate of true behavior changes remains 22.0 / 22 = 100.0%, with a conservative lower bound of 18.15 / 22 (82.5%), preserving the weak-grader headline of 18 / 28 = 64.3% (95% Wilson CI: 45.8%–79.3%).
- **Deterministic Non-Equivalence Check:** 0 / 22 (0.0%) demonstrated by task package test suites (all 22 passed task tests; repository test suites inside unextracted container images remained unmeasured with $0 Modal spend).
## Files
- `prepare_cohort.py`: Extracts post-patch source context, diffs, and task instructions; assigns opaque IDs.
- `run_judges.py`: Executes blind judging fleet with session attestation and SQLite process isolation.
- `analyze_results.py`: Computes calibration metrics, Wilson intervals, McNemar tests, and summary tables.
- `static_crosscheck.py`: Performs deterministic AST analysis of survivor reachability from task test suites.
- `test_equivalence.py`: Synthetic test suite for pipeline logic.
- `summary.json`, `rates.json`, `contingency_table.json`, `reachability.json`: Aggregate metrics.
- `survivor_classification.json`: Public listing of survivor opaque IDs and definition satisfaction.
- `manifest.sha256`: SHA-256 checksums for all tracked artifacts.
- `prereg-addendum.md`: Pre-registration for specificity controls and Rogan-Gladen decision rules.
- `build_controls.py`: Constructs 44 known-equivalent controls and verifies reward 1.0 on Modal.
- `prepare_specificity_cohort.py`: Shuffles 74 original items + 44 controls into 118 blind items (`BLIND-001` .. `BLIND-118`).
- `run_specificity_judges.py`: Executes 236 attested judge calls with session logging.
- `analyze_all.py`: Computes dual-parameter Rogan-Gladen rates and Wilson intervals.
- `known_equivalent_controls.json`: Tracked control definitions, rewrites, and diffs.
- `summary_v2.json`: Unified summary metrics for specificity evaluation.
