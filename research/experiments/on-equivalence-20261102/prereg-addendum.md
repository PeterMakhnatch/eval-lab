# Pre-Registration Addendum: Judge Specificity with Known-Equivalent Controls (FIX-A2B-SPECIFICITY)

**Date:** 2026-11-02  
**Experiment:** Equivalence Check of A2 Surviving Mutants (Follow-up to PR #1763)  
**Author:** FIX-A2B-SPECIFICITY Agent  

---

## 1. Scientific Motivation and Core Question

In study A2B-EQUIV (PR #1763), two independent LLM judges (`gemini-3.8-flash` and `gemini-3.1-flash-lite`) evaluated 74 mutants (22 surviving mutants from A2, 51 killed positive controls, and 1 unexecuted mutant). Both judges classified **100% (74 / 74)** of items as `behavior_changing`, yielding zero `equivalent` verdicts and zero `uncertain` verdicts ($\kappa = 1.0000$ with zero variance).

The original report assumed a specificity $Sp = 1.0$ (assuming judges never falsely classify equivalent code as behavior-changing). However, an evaluator that assigns only one class has zero demonstrated discrimination. Mutants are presented as code diffs, which may induce strong confirmation bias in LLM evaluators to find or invent plausible behavioral differences.

**Core Question:** Can these two LLM judges reliably recognize semantic equivalence when presented with known-equivalent code transformations in the exact same diff and context format, or do they suffer from near-zero specificity? What is their true specificity on easy (local AST rewrites) versus hard (non-local semantic redundancy) equivalent controls?

---

## 2. Cohort Definition and Negative Controls by Construction

We evaluate a unified blind cohort consisting of:
1. **Original Cohort ($N = 74$):** The exact 74 items from A2B (22 survivors, 51 killed positive controls, 1 unexecuted mutant).
2. **Known-Equivalent Negative Controls ($N \ge 40$):** At least 40 semantics-preserving transformations constructed on the exact same reference code and added lines from which the 22 survivors were generated.

### Equivalence Proof Standards
Every negative control must satisfy two rigorous conditions:
1. **Syntactic Non-Identity:** The Python AST of the rewritten code must differ from the reference code (`ast.dump(rewritten) != ast.dump(reference)`), ensuring a non-trivial mutation diff is generated.
2. **Behavioral Invariance Proof:** The task's reference test suite / grader must be executed against the rewritten code and achieve a score of `1.0` (all tests pass), confirming that the mutation does not break any expected behavior or contracts.

### Control Categories
- **Easy Controls (Local Semantics-Preserving AST Rewrites):**
  - Consistent local variable renaming within a function scope (e.g. `tmp` -> `_tmp_var`).
  - Redundant parentheses around pure expressions: `(a) == (b)`.
  - Commutativity on pure names / literals: `x == y` -> `y == x`, `a and b` -> `b and a` where `a, b` are side-effect-free identifiers.
  - Wrapping statements in `try: ... finally: pass`.
  - Adding dead branches: `if False: pass`.
  - Redundant guard removal: removing a second guard that is provably identical to an immediate preceding guard.
  - Redundant statement insertion: `assert True` or `pass`.
- **Hard Controls (Non-Local Semantic Redundancy):**
  - Redundant guard removal where the condition is guaranteed true/false by prior early-returns or typing invariants.
  - Redundant default assignments or defensive checks guaranteed by caller contracts.

---

## 3. Blind Evaluation Protocol

- All items ($74 + N_{\text{ctrl}}$) are shuffled under deterministic pseudo-random permutation with seed `20261102` and assigned new opaque identifiers (`BLIND-001` .. `BLIND-N`).
- Evaluated independently by:
  1. `Judge 1`: `google-antigravity/gemini-3.8-flash:medium`
  2. `Judge 2`: `google-antigravity/gemini-3.1-flash-lite:medium`
- Worker configuration: `--config cert/worker.yml --no-skills --no-tools`.
- Prompts, task instructions, enclosing context windows, and unified diff formats are completely identical to A2B.
- Attestation requirement: 100% served model match, 0 fallbacks, at most 4 concurrent processes.

---

## 4. Decision Rules, Metrics, and Pass/Fail Gate

### Metrics
- **Specificity ($Sp$):** Fraction of negative controls classified as `equivalent`:
  $$Sp = \frac{K_{\text{equiv}}}{N_{\text{ctrl}}}$$
  Reported with 95% Wilson score intervals, computed separately for:
  - Overall controls
  - Easy controls
  - Hard controls
  - Judge 1, Judge 2, Consensus (J1 AND J2), and Union (J1 OR J2).
- **Sensitivity ($Se$):** Fraction of ground-truth killed positive controls ($N = 51$) classified as `behavior_changing`.
- **Rerun Stability:** Proportion of the 74 original items whose classification matches the initial A2B run.

### Pre-Registered Pass/Fail Gate
- **Informative Judge Criterion:** Specificity point estimate $Sp \ge 0.80$ AND Wilson 95% lower bound $Sp_{\text{lo}} \ge 0.60$.
- **Outcome A: Gate Fails ($Sp < 0.80$ or $Sp_{\text{lo}} < 0.60$):**
  - The judges are declared **UNINFORMATIVE for equivalence** due to false-alarm rate on equivalent code.
  - The claim that weak-grader prevalence is "verified identical to A2's baseline (64.3%)" must be **retracted**.
  - Headline verdict must be restated: *"A2's 18 of 28 (64.3%) is an upper-leaning estimate; equivalence was not established"*.
  - Apply the two-sided Rogan-Gladen prevalence correction propagating both $Se$ and $Sp$ confidence intervals:
    $$\hat{p} = \frac{p^* + Sp - 1}{Se + Sp - 1}$$
    showing the full, widened uncertainty interval.
- **Outcome B: Gate Passes ($Sp \ge 0.80$ and $Sp_{\text{lo}} \ge 0.60$):**
  - Judges demonstrate genuine discriminative specificity.
  - The equivalence claim is retained, with the calibrated rate updated under the empirical dual-parameter Rogan-Gladen formula.

---

## 5. Deterministic Non-Equivalence Analysis

Independent of LLM judging and with zero Modal spend:
- For each of the 22 surviving mutants, examine the task repository and test suite to determine if any test exercises the mutated line and would distinguish the mutated behavior from the reference behavior.
- Report the count of deterministically proven non-equivalent mutants versus unproven mutants.
