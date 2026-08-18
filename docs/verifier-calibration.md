---
status: living
audience:
  - builder
  - analyst
---

# Verifier Calibration and Selection Lift

This document defines the methodology, metric design, and platform boundaries for
evaluating LLM-based verifiers (`llm-verifier` / LLM-as-a-Verifier) against execution
ground truth in the evaluation lab.

## Core Principle: Execution Ground Truth as the Anchor

The evaluation lab possesses execution rewards from deterministic containerized runs (Harbor
test suites, pytest ctrf, oracles, and adversarial controls). Because execution outcomes
are unambiguous ground truth ($y \in \{0, 1\}$), LLM verifiers are never trusted blindly;
they are graded, calibrated, and evaluated for selection efficacy.

Two primary measurements are performed on execution ground truth:

1. **Selection Lift ($k \ge 3$)**: Using the verifier to select the best candidate among $k$
   attempts and measuring whether selection outperforms random sampling ($\text{pass@1}$)
   and approaches the oracle ceiling ($\text{oracle@k}$).
2. **Chance-Corrected Verifier Agreement**: Scoring individual rollout trajectories against
   execution ground truth using metrics robust to severe class imbalance.

---

## The Hard Architectural Boundary

> **Hard Boundary**: LLM verifiers **NEVER** replace execution verifiers in the battery,
> workbench, or registry admission gates. This is an instrument experiment
> (`purpose=calibration` or `purpose=elicitation`).

This boundary is enforced structurally:
- **Dependency Isolation**: `llm-verifier` is isolated in its own optional extras group
  (`[project.optional-dependencies] verifier = ["llm-verifier>=0.2.0"]`), excluded from default
  and development dependencies.
- **Graceful Degradation**: Modules degrade cleanly with `MissingVerifierDependencyError` when
  the optional extra is not installed.
- **Import Boundary**: No module in `evallab.task_workbench`, `evallab.registry`, or the battery
  imports `llm_verifier` or `evallab.calibrate`.
- **Token Spend Gate**: Real LLM verifiers refuse execution and raise
  `PaidModelAuthorizationError` unless explicitly authorized via `allow_paid_tokens=True`.
  Development, testing, and CI exclusively utilize deterministic `StubVerifier` controls.

---

## 1. Selection Lift (Best-of-$k$)

For suite tasks with $k \ge 3$ execution attempts:

- **$\text{pass@1}$**: The baseline expectation from randomly selecting a single attempt:
  $$\text{pass@1} = \frac{1}{k} \sum_{i=1}^k r_i$$
- **$\text{selected@k}$**: The execution reward of the candidate picked by the verifier:
  $$\text{selected@k} = r_{j}, \quad j = \arg\max_{i} \hat{s}_i$$
- **$\text{oracle@k}$ (Oracle Ceiling)**: The theoretical ceiling achieved by perfect selection:
  $$\text{oracle@k} = \max_{i=1}^k r_i$$
- **Selection Lift**: $\Delta = \text{selected@k} - \text{pass@1}$.

### Uncertainty and Power Contracts

- All estimates carry **TRUTH bootstrap 95% confidence intervals** computed across task
  evidence units via `evallab.cohort.bootstrap_mean_interval`.
- **Underpowered Cohorts**: When $n_{\text{tasks}} < 2$ or bootstrap intervals are unavailable,
  the report explicitly renders `"not distinguishable"` (`NOT_COMPARABLE`) rather than
  presenting misleading point estimates.

---

## 2. Verifier Agreement on Unbalanced Corpora

### Never-Measured Trials Are Not Failures

A trial with a non-null `exception_class` (such as container crash `NonZeroAgentExitCodeError`
or parameter parsing `ValueError`) produced **no execution measurement**. Conflating harness
exceptions with capability failures inverts statistical conclusions.

- **Rule**: All unmeasured exception trials are strictly excluded from agreement and confusion
  matrices and reported separately as `never_measured_trials`.

### Metric Choice for Class-Imbalanced Corpora

In real evaluation corpora, passes often dominate (e.g. 68 pass / 8 fail / 16 unmeasured in the
92-trial baseline corpus, pass prevalence 89.5%). On such data, **raw agreement is actively
misleading**: a trivial verifier that always predicts "pass" achieves 89.5% raw accuracy.

To expose degenerate verifiers and calibrate discriminating power, the lab computes:

1. **Cohen's Kappa ($\kappa$)**: Chance-corrected inter-rater agreement:
   $$\kappa = \frac{p_o - p_e}{1 - p_e}$$
   *An always-pass verifier yields $\kappa = 0.000$, unmasking zero informational value.*
2. **Balanced Accuracy**: Arithmetic mean of sensitivity and specificity:
   $$\text{Balanced Accuracy} = \frac{\text{TPR} + \text{TNR}}{2}$$
   *An always-pass verifier yields $\text{Balanced Accuracy} = 50.0\%$, equivalent to coin-tossing.*
3. **Matthews Correlation Coefficient (MCC)**: Pearson correlation between binary labels:
   $$\text{MCC} = \frac{TP \times TN - FP \times FN}{\sqrt{(TP+FP)(TP+FN)(TN+FP)(TN+FN)}}$$
4. **Macro F1**: Unweighted mean of per-class F1 scores ($F1_{\text{pass}}$ and $F1_{\text{fail}}$).

---

## Data Models and Contracts

### `CalibrationRecord` (§2.1 / `evallab.schemas`)

```python
class CalibrationRecord(ContractModel):
    schema_version: Literal[1] = 1
    calib_id: str = Field(description="ULID primary key")
    judge_model: str = Field(min_length=1)
    rubric_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    corpus_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    per_criterion_agreement: dict[str, CriterionAgreement]
    date: datetime
```

Records are stored as JSON under `research/calibration/records/verifier/{calib_id}.json` and
queried via `evallab.calibrate.load_calibration_records(repo_root)`.

---

## Eval Card Generation

Calibration evaluations emit eval cards with `purpose="calibration"` through the standard
platform renderer (`evallab.calibrate.build_verifier_calibration_card` / `cards.py`):

- Reports both Selection Lift ($\text{pass@1}$, $\text{selected@k}$, oracle ceiling) and
  Verifier Agreement ($\kappa$, Balanced Accuracy, Raw Agreement, Class Balance).
- Explicitly labels verifier provenance (e.g. `INJECTED STUB VERIFIER (Deterministic local control)`
  vs `LIVE LLM VERIFIER`).
- Records threats to validity: cohort power, class imbalance, and unmeasured exceptions.
