# Pre-Registration: A2B-EQUIV (Equivalence Check of A2 Surviving Mutants)

**Date:** 2026-11-02  
**Item:** A2B-EQUIV  
**Experiment Directory:** `research/experiments/on-equivalence-20261102/`  
**Parent Item:** A2-AUDIT-SWEEP (`research/experiments/on-weak-grader-census-20261102/`, PR #1746 merged)

---

## 1. Research Question and Motivation

In A2-AUDIT-SWEEP, 18 of 28 audited original MiMo graders (64.3%, 95% Wilson CI: 45.8–79.3%) accepted at least one deterministic AST mutant derived from the reference patch, with 22 of 74 mutants surviving execution (`reward == 1.0`).
However, AST mutations (e.g. dropping a defensive check, flipping an inequality boundary, dropping a logging call) are syntactically distinct but may be semantically equivalent within the operational domain of the software or the benchmark task (e.g. unreachable dead code, redundant guard already enforced by caller, identical value computed).

**Primary Research Questions:**
1. What proportion of A2's 22 surviving mutants are genuine semantic behavior changes vs functionally equivalent mutations?
2. What is the weakness prevalence of original MiMo graders under calibrated semantic equivalence judging, and what share of A2's 64.3% weak-grader headline survives?

---

## 2. Evaluation Cohort and Ground Truth Controls

To evaluate mutant equivalence without bias, blind LLM judges require a ground-truth calibration set.

### 2.1 The Cohort
- **Survivors ($N_{\text{surv}} = 22$):** All 22 mutants from A2 that survived original grader evaluation (`reward == 1.0`, applied successfully).
- **Controls ($N_{\text{ctrl}} = 51$):** All 51 mutants from the same 28 audited tasks from A2 that were successfully applied and KILLED by the original grader (`reward == 0.0`, failed test suite).
  - Ground-truth invariant: Mutants killed by the test suite demonstrably change observable program behavior on at least one test input. They serve as guaranteed positive controls for `behavior_changing`.
- **Excluded from Calibration:** 1 mutant (`format-code-task-001426`, cell `original-m1-equality_flip-L318`) failed sandbox execution during A2 (`applied == False`, `reward == None` due to Modal name collision) and was never executed against the grader's test suite. It is judged blindly alongside the cohort to maintain full 74-mutant coverage, but excluded from the ground-truth control calibration set.
- **Secondary Control Cohort ($N_{\text{ctrl-weak}} = 28$):** The subset of 28 killed mutants originating from the 18 weak tasks that yielded survivors.
- **Total Cohort Size:** Exactly 74 mutants (22 survivors + 51 killed controls + 1 unexecuted mutant).
---

## 3. Judge Rubric and Allowed Outputs

Each judge will be presented with a blind evaluation prompt containing:
1. The task instruction describing the required behavior and problem context.
2. The enclosing function, class, or method excerpt from the post-patch reference solution (with surrounding context lines).
3. The unified diff showing the exact AST mutation relative to the reference solution.

### 3.1 Strict Information Boundary
The judge is **NEVER** provided:
- The task ID, repository URL, or git commit hashes.
- The test files, test names, test assertions, or verifier scripts.
- The grader execution outcome, reward (`1.0` vs `0.0`), stdout/stderr, or test output.
- The survival or killed status.

### 3.2 Verdicts and Formatting
The judge must output a structured JSON object with two fields:
- `verdict`: Exactly one of:
  - `"behavior_changing"`: The mutant alters observable behavior (return value, raised exception, side effect, state mutation, termination) on at least one legal or plausible input within the scope of the task.
  - `"equivalent"`: The mutant cannot alter observable program behavior on any input (e.g. dead code, redundant guard already enforced by caller, identical value computed via alternative syntax, unreachable branch).
  - `"uncertain"`: The judge cannot determine equivalence from the provided context (e.g., depends on undefined external library internals).
- `reason`: Exactly one sentence naming the specific distinguishing input, execution path, or condition (for `behavior_changing`), or why no distinguishing input exists (for `equivalent`), or what critical information is missing (for `uncertain`).

---

## 4. Judge Routes and Execution Harness

Two independent LLM judges will evaluate every item in the blind cohort:
1. **Judge 1:** `google-antigravity/gemini-3.8-flash` (medium thinking)
2. **Judge 2:** `google-antigravity/gemini-3.1-flash-lite` (medium thinking)

### Execution Constraints:
- Invocation: `omp -p --model <model> --config /Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/worker.yml --no-skills --no-tools`
- Concurrency: At most 4 concurrent processes in the execution script.
- Model Attestation: Every session `.jsonl` will be inspected to verify that `model` equals the requested model and `resolvedModelIsFallback == false`. Zero fallbacks are permitted.
- Fallback / Unapproved Models: Zero calls to `gemini-4-argon` or unapproved models.

---

## 5. Metrics and Pre-Registered Decision Rules

### 5.1 Judge Calibration on Controls (Ground Truth: `behavior_changing`)
Because all 52 controls were killed by the grader, the ground truth is `behavior_changing`.
- **Sensitivity:** Fraction of killed controls classified as `behavior_changing`:
  $$\text{Sensitivity} = \frac{\#(\text{Killed controls judged } \texttt{behavior\_changing})}{N_{\text{ctrl}}}$$
  Reported with 95% Wilson interval for Judge 1, Judge 2, and their consensus.
- **Uncertainty Rate:** Fraction of controls classified as `uncertain`. Reported separately and **never** collapsed into `equivalent` or `behavior_changing`.
- **Inter-Rater Agreement:** Cohen's $\kappa$ between Judge 1 and Judge 2 on the 3-class outcome (`behavior_changing`, `equivalent`, `uncertain`) and the collapsed binary outcome, with the full contingency table.

### 5.2 Pre-Registered Weak-Grader Rates
A task is classified as "weak" if its grader accepted at least one qualifying mutant. We evaluate three definitions:
- **Definition (A) [Syntactic Survivor - A2 Baseline]:** A task is weak if at least one generated AST mutant survived on the grader (A2's headline: 18 / 28 = 64.3%).
- **Definition (B) [Consensus Semantic Survivor]:** A task is weak if at least one surviving mutant is judged `behavior_changing` by **BOTH** Judge 1 AND Judge 2.
- **Definition (C) [Union Semantic Survivor]:** A task is weak if at least one surviving mutant is judged `behavior_changing` by **EITHER** Judge 1 OR Judge 2.

For each definition, we report:
- The count and fraction of weak tasks $k/n$ with 95% Wilson interval.
- The list of surviving mutant IDs qualifying under that definition.
- Exact two-sided McNemar test comparing paired weakness status of tasks under (B) vs (A) and (C) vs (A).

### 5.3 Sensitivity-Corrected Prevalence Estimation
If the judges have imperfect sensitivity $S < 1.0$ on ground-truth behavior-changing mutants, the observed count of behavior-changing survivors is biased downward. Assuming high specificity ($Sp \approx 1.0$, i.e. judges rarely falsely claim a dead-code change alters behavior):
$$\hat{N}_{\text{true behavior-changing}} = \frac{N_{\text{observed behavior-changing}}}{S}$$
We will project the sensitivity-adjusted survivor count and the resulting expected weak-grader rate, explicitly documenting all assumptions.

---

## 6. Deterministic Static Reachability Cross-Check

For every survivor judged `equivalent` (under Definition B or C):
- We perform a static AST analysis of the task package:
  1. Inspect the test suite files in `package/tests/` or equivalent test directory.
  2. Determine whether the enclosing function or mutated line is called or statically reachable from any test function.
- Report the count of `equivalent` survivors whose mutated line is:
  - Statically uncalled / unreachable from test code (strong evidence of dead code / test omission).
  - Statically reachable (suggesting either semantic equivalence or test assertion weakness).

---

## 7. Pass / Fail Acceptance Gate

The experiment passes if and only if:
1. All 74 mutants (22 survivors + 52 controls) are evaluated by both Judge 1 and Judge 2 with zero missing calls (100% blind coverage).
2. Attestation verifies 0 model fallbacks and 0 invocations of unapproved models.
3. Judge sensitivity on controls is reported with 95% Wilson intervals.
4. Weak-grader prevalence is computed under all three pre-registered definitions (A, B, C) with 95% Wilson intervals.
5. All raw receipts, logs, and tables are preserved under `cert/raw/A2B-EQUIV/receipts/` and copied to `~/Developer/eval-lab-results/2026-11-02/A2B-EQUIV/`.
6. Code, prereg, aggregate tables, and sha256 manifest are committed in a PR under `research/experiments/on-equivalence-20261102/` with auto-merge enabled.
