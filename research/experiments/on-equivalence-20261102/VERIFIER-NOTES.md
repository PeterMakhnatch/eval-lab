# Verifier Notes: A2B Equivalence Check of Surviving Mutants

**Review context:** Coordinator overnight review; FIX-A2B-SPECIFICITY addendum (`prereg-addendum.md`)  
**Status:** 22/22 behavior-changing classification CONFIRMED; specificity calibration ADDED

## Key Notes and Guidance

| Topic | Finding | Status | What Reader Should Use Instead |
|---|---|---|---|
| Survivor Equivalence (N=22) | 22/22 surviving mutants from A2 classified as `behavior_changing` by both blind LLM judges (`gemini-3.8-flash` and `gemini-3.1-flash-lite`). Equivalent mutants: 0/22 (0.0%). | CONFIRMED | Surviving mutants represent genuine semantic behavior alterations, not dead-code artifacts. |
| Sensitivity Calibration | Both judges achieved 51/51 = 100% sensitivity on ground-truth killed mutant controls. | CONFIRMED | Validated sensitivity on known semantic alterations. |
| Specificity Calibration Addendum | Initial run lacked negative controls (pure formatting/whitespace diffs). FIX-A2B-SPECIFICITY evaluated 20 negative controls to ensure judges do not universally label every syntactic edit as behavior-changing; specificity was confirmed. | ADDED | Refer to `prereg-addendum.md` and `summary_v2.json` for full specificity controls. |
| Weak-Grader Prevalence | Weak-grader rate remains 18/28 = 64.3% (45.8–79.3%) under both syntactic (Def A) and semantic (Def B/C) definitions ($b=0, c=0, p=1.0$). | CONFIRMED | Equivalence filtering does not alter the 64.3% weak-grader prevalence. |
