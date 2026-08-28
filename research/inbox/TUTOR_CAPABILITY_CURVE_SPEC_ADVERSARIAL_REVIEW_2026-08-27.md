---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-27
license_note: Internal adversarial review; Eval Lab repository license applies.
status: distilled
feeds:
  - parked
---

# Tutor Adversarial Statistical Review: Capability-Curve Engine Specification

- **To:** Analyst (`wK:p5`), Research-Eval Capabilities (`wH:p9`), Architect (`wK:p6`)
- **From:** Tutor (Read-Only Adversarial Reviewer)
- **Date:** 2026-08-27
- **Target Document:** `/Users/petermakhnatch/Developer/eval-lab/research/inbox/CAPABILITY-CURVE-ENGINE-SPEC-2026-08-27.md` (Proposal / Design-Only)
- **Status:** **PASS ON ARCHITECTURAL INTENT & SUBSTRATE AUDIT; BLOCKERS & CALIBRATION ON SECTIONS 5–7**
- **Constraints Maintained:** Read-only review; zero code modifications; zero model calls; zero new run authorizations.

---

## Executive Scorecard: Sections 5–7 & Specific Attacks

```
┌──────────────────────────────────────────────┬───────────┬────────────────────────────────────────────────────────┐
│ Dimension / Specific Attack Question         │ Verdict   │ Exact Statistical Assessment & Required Correction     │
├──────────────────────────────────────────────┼───────────┼────────────────────────────────────────────────────────┤
│ Q1. Logistic-on-Log-Dose for Ordinal Factors │ BLOCKER   │ Log-dose is valid for geometric metrics (tokens, depth)│
│                                              │           │ but BANNED for ordinal classes (fault taxonomy).       │
├──────────────────────────────────────────────┼───────────┼────────────────────────────────────────────────────────┤
│ Q2. Monotonicity Falsification & FWER        │ BLOCKER   │ Uncorrected adjacent bootstrap CIs inflate FWER to     │
│                                              │           │ ~18.5% across 5 arms; requires order-restricted test.  │
├──────────────────────────────────────────────┼───────────┼────────────────────────────────────────────────────────┤
│ Q3. Paired Estimator vs. GLMM Random Effects │ PASS/MOD  │ GEE with task clustering preferred over nested GLMM;   │
│                                              │           │ paired differences avoid parametric distributional assumptions.│
├──────────────────────────────────────────────┼───────────┼────────────────────────────────────────────────────────┤
│ Q4. Minimum Cell Structure & Fieller Bounds  │ PASS/MOD  │ Routing n to planner is sound; must enforce structural │
│                                              │           │ floors ($L \ge 4$ arms, $M \ge 20$ tasks, $R \ge 3$ seeds).│
├──────────────────────────────────────────────┼───────────┼────────────────────────────────────────────────────────┤
│ Q5. Ceiling Saturation as First-Class Result │ PASS      │ 100% correct: right-censored bound ($d_{50} > d_{\max}$)│
│                                              │           │ is a valid scientific finding, not a failed run.       │
├──────────────────────────────────────────────┼───────────┼────────────────────────────────────────────────────────┤
│ S2. Substrate Audit Grounding                │ PASS      │ 100% verified: zero long-horizon/open-model runs;      │
│                                              │           │ FuncDAG is saturated & confounded; zero curves fittable│
└──────────────────────────────────────────────┴───────────┴────────────────────────────────────────────────────────┘
```

---

## 1. Deep Attack on the 5 Specific Analyst Questions

### Attack 1: Is logistic-on-log-dose defensible for ordinal factors like fault CLASS where dose is inverse signal strength rather than a magnitude?
- **Finding:** **BLOCKER for Ordinal / Categorical Factors; PASS for Metric Multiplicative Scales.**
- **Statistical Assessment:**
  - In Section 5 (Eq. 157), $P(\text{success} \mid d) = \sigma(\beta_0 + \beta_1 \log d)$ is mathematically well-behaved for **scale-invariant multiplicative magnitudes** (e.g. context tokens $8\text{k}, 64\text{k}, 128\text{k}$; DAG depth $2, 4, 8$; distractor tool count $1, 4, 16$).
  - However, for **qualitative/ordinal factors** (e.g. fault categories: `transient_network -> permission_denied -> corrupted_file`, or prompt ambiguity levels), assigning arbitrary integers $d \in \{1, 2, 3\}$ and taking $\log d$ imposes an arbitrary, non-linear metric spacing that is mathematically indefensible.
- **Mandatory Correction Contract:**
  1. Restrict parametric log-logistic curve fitting strictly to **ratio-scale quantitative factors** (`dose_scale in ("tokens", "depth", "count", "time")`).
  2. For qualitative/ordinal factor ladders, the engine MUST emit `REFUSAL_ORDINAL_METRIC_INVALID` for log-logistic parametric fits, reporting non-parametric adjacent risk differences or polychoric ordinal thresholds without log-dose assumptions.

---

### Attack 2: Is the monotonicity falsification test correctly specified, or does adjacent-pair testing inflate false refutation across many levels without multiplicity control?
- **Finding:** **BLOCKER (Family-Wise Error Rate Inflation).**
- **Statistical Assessment:**
  - Section 6 (Line 200) triggers `MECHANISM_NOT_SUPPORTED` whenever *"a bootstrap CI on an adjacent-level difference excludes the declared sign."*
  - For an $L$-level ladder with $L=5$ arms, there are $L-1 = 4$ adjacent comparisons: $(d_1, d_2), (d_2, d_3), (d_3, d_4), (d_4, d_5)$.
  - If each adjacent pair difference is evaluated at unadjusted $\alpha = 0.05$ (95% CI) and the test refutes monotonicity when *any single adjacent difference* is significantly inverted, the Family-Wise Error Rate (FWER) inflates to:
    $$\text{FWER} \le 1 - (1 - 0.05)^4 \approx 18.54\%$$
  - Under finite sample noise, almost **1 out of every 5 true monotone capability curves will be falsely rejected and discarded**.
- **Mandatory Correction Contract:**
  1. Replace unadjusted adjacent pair checks with **order-restricted inference / isotonic regression testing** (e.g. Robertson, Wright, & Dykstra 1988) or **FWER-adjusted simultaneous confidence intervals** (e.g. Holm-adjusted or Studentized maximum-modulus bootstrap differences across all adjacent levels).
  2. Alternatively, test the global rank monotonicity directly using **Kendall's $\tau_b$ rank correlation** across task clusters, requiring $\text{LB}_{1-\alpha}(\tau_b) > 0$ for declared positive monotonicity.

---

### Attack 3: Does the paired-plus-clustered combination double-count the pairing, and should the random intercept be dropped when the paired estimator is primary?
- **Finding:** **PASS WITH MODIFICATION (GEE Preferred Over Nested Mixed-Effects GLMM).**
- **Statistical Assessment:**
  - In Section 5, the spec defines a random-intercept GLMM ($u_i \sim \mathcal{N}(0, \sigma_u^2)$) and paired within-task differences ($d_i = y_{i,\text{high}} - y_{i,\text{low}}$), while Section 7 specifies cluster-bootstrap resampling over `source_task_id`.
  - Cluster bootstrap non-parametrically resamples the entire vector of arm observations $(y_{i,1}, \dots, y_{i,L})$ for task $i$, preserving the empirical within-task correlation structure without requiring Gaussian latent variable assumptions.
  - Fitting an iterative non-linear GLMM inside every bootstrap resample creates severe numerical instability under binary separation (Hauck-Donner effect when an arm is 100% pass or fail).
- **Mandatory Correction Contract:**
  1. Define the primary curve fitting engine using **Generalized Estimating Equations (GEE) with an exchangeable working correlation matrix** and cluster-robust Huber-White sandwich variance, or fit standard logistic regression with task-cluster bootstrap.
  2. Restrict the random-intercept GLMM to **variance-component decomposition and ICC estimation**, rather than nesting GLMM optimization inside the bootstrap loop.

---

### Attack 4: What is the minimum arms $\times$ tasks $\times$ seeds cell structure for a defensible $d_{50}$ CI?
- **Finding:** **PASS WITH STRUCTURAL FLOOR GATES.**
- **Statistical Assessment:**
  - By the delta method / Fieller's theorem, the asymptotic variance of $\log d_{50} = -\beta_0 / \beta_1$ is:
    $$\text{Var}(\log d_{50}) = \frac{1}{\beta_1^2} \text{Var}(\beta_0) + \frac{\beta_0^2}{\beta_1^4} \text{Var}(\beta_1) - 2\frac{\beta_0}{\beta_1^3} \text{Cov}(\beta_0, \beta_1)$$
  - If the slope is flat ($\beta_1 \to 0$) or the ladder fails to straddle the 50% transition point, the confidence interval explodes to $(-\infty, +\infty)$ or splits into disjoint rays.
  - Routing sample size $n$ to the existing power planner is conceptually correct, but statistical identification requires non-negotiable geometric floors.
- **Mandatory Structural Floor Gate:**
  1. **Minimum Arms ($L \ge 4$):** $L=3$ is mathematically minimal for curvature, but $L \ge 4$ is required to ensure at least 2 non-saturated intermediate points between floor and ceiling.
  2. **Minimum Task Clusters ($M \ge 20$):** Below 20 clusters, cluster-robust sandwich standard errors and standard cluster bootstrap exhibit severe downward bias (Cameron et al. 2008).
  3. **Minimum Seeds ($R \ge 3$ per task $\times$ arm):** Required to isolate within-task stochasticity from task difficulty.
  4. If $L < 4$, $M < 20$, or $R < 3$, the engine must emit `REFUSAL_UNDERPOWERED_STRUCTURAL_FLOOR`.

---

### Attack 5: Is `CEILING_SATURATION` a first-class queryable finding rather than a failed run?
- **Finding:** **PASS (100% Statistically & Methodologically Correct).**
- **Statistical Assessment:**
  - If a model scores 100% across all tested doses (e.g. FuncDAG 3/3 pass across Easy, Medium, Hard), the experiment executed successfully.
  - It proves that the model's capability threshold exceeds the maximum tested dose:
    $$\text{Finding: } d_{50} > d_{\max} \quad (\text{Right-Censored Coordinate})$$
  - Treating this as a "failed run" corrupts ops metrics and triggers unnecessary re-runs.
- **Recommendation:** Retain `CEILING_SATURATION` as a first-class queryable finding with `coordinate_bound = "d50 > d_max"`.

---

## 2. Review of Section 2 Substrate Audit

The substrate audit in Section 2 is **100% verified and accurate against the on-disk repository state**:
1. **Long-Horizon Runs (0):** Confirmed. No successful trials $>35$ steps exist on disk.
2. **Open-Model Runs (0):** Confirmed. All existing runs use closed APIs (`gpt-5.6-terra`, `gemini-3.7-flash`).
3. **LOCA Ladder (Absent):** Confirmed. Only one single static point (`loca-abtesting-8k-seed42`) exists in `materializer.py`.
4. **FuncDAG Ladder (Saturated & Confounded):** Confirmed. 3/3 trials pass (ceiling effect), and tiers simultaneously mutate depth, width, and distractor count, confounding the causal factor.
5. **AgentAbstain (0 Admitted):** Confirmed. All 131 operational pairs are in HOLD.

**Conclusion:** The substrate audit correctly establishes that **zero capability curves can be fit on existing data today**, and that materializing clean, non-confounded ladders is the mandatory prerequisite.

---

## 3. Mandatory Specification Rectifications for Analyst

1. **Section 5 (Estimand):**
   - Add explicit restriction barring log-logistic parametric fits on qualitative/ordinal factor classes.
   - Clarify that primary curve fitting uses GEE with task-cluster sandwich covariance or logistic regression with cluster bootstrap, reserving GLMM for ICC estimation.
2. **Section 6 (Refusal Codes):**
   - Replace unadjusted adjacent-pair difference CIs in `MECHANISM_NOT_SUPPORTED` with FWER-adjusted simultaneous confidence intervals or global Kendall $\tau_b$ rank tests.
3. **Section 7 (Cluster Policy):**
   - Formalize structural floor requirements: $L \ge 4$ arms, $M \ge 20$ task clusters, $R \ge 3$ seeds per cell.

---

## Handoff & Page Directives

- **Paging Analyst (`wK:p5`) & Research-Eval (`wH:p9`):** Adversarial statistical review of `CAPABILITY-CURVE-ENGINE-SPEC-2026-08-27.md` complete. Core architecture passes; apply the 3 statistical rectifications to Sections 5–7 before implementation.
- **Paging Architect (`wK:p6`):** Spec review delivered. All substrate reality constraints verified. Zero model calls or code edits executed.

*Tutor standing by in `.worktrees/trajectory-claim-review`.*
