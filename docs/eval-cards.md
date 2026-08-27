---
status: living
audience:
  - analyst
  - operator
  - builder
---

# Eval Cards (E11)

Eval cards are the platform's citable, provenance-bearing experimental results (`docs/platform-architecture.md` v2 §4).
They represent the terminus of the experiment specification lifecycle:

```
draft -> (purpose=comparison => prereg required) -> submitted -> gated -> dispatched -> analyzed -> carded
```

Every eval card is generated from completed evidence using the unified DuckDB attach surface (`evallab.storage.attach`), calculates uncertainty with the task as the evidence unit (`evallab.cohort`), and carries explicit lineage digests for upstream tracing.

---

## 1. Purpose-to-Card Matrix

The experiment's declared `purpose` (`ExperimentSpec.purpose`) strictly binds the generated card shape and admission rules:

| Purpose | Card Shape | Lifecycle Contract & Statistical Handling |
|---|---|---|
| `baseline` | Per-agent pass@k card | Task-clustered pass@k with task-bootstrap 95% interval; reports unmeasured exceptions separately from scored failures. |
| `comparison` | Paired comparison card | Paired analysis quoting the preregistration block verbatim. **Refuses to render without a prereg block.** |
| `elicitation` | One-variable ablation card | Contrasts spec against its `ref_spec`, verifying diff touches exactly one elicitation field. |
| `drift` | Canary drift card | Compares canary task performance against trailing 7-day baseline. |
| `calibration` | Judge calibration card | Reports judge criterion agreement rates against sealed gold labels. |
| `craft` | Batch classification card | Aggregates cheap LLM classification facets across task suites. |
| `practice` | **Excluded (Refusal)** | Excluded from eval cards and lessons. Attempting to generate a card raises a refusal. |

---

## 2. Refusal Conditions and Exact Messages

Eval-card generation enforces strict platform policies and fail-closed integrity checks:

### 2.1 Practice Purpose Exclusion
Practice runs are temporary or exploratory calibration checks and must never pollute citable research surfaces or lessons views:
```
Refusal: purpose='practice' is excluded from eval cards and lessons.
```

### 2.2 Missing Preregistration for Comparisons
Comparison specs require a preregistration block (`expected` outcome + `decision_rule`) submitted before dispatch. The generator refuses to draft a comparison card if this block is absent or incomplete:
```
Refusal: purpose='comparison' requires a prereg block with expected result and decision rule before generating an eval card.
```

### 2.3 Incomplete Job Evidence
A job whose recorded trial count does not match `n_total_trials` from its Harbor run is rejected:
```
job '<job_name>' is incomplete: <actual> of <expected> trials recorded
```

### 2.4 Unlocatable Target
When a target `spec_id`, `job_id`, or path cannot be resolved:
```
Could not locate completed Harbor job for target: '<target>'
```

---

## 3. Statistics and Statistical Source Map

All statistical procedures are imported directly from stable interfaces in `src/evallab/cohort.py`. Reimplementing statistics in generator code is strictly prohibited.

```
+--------------------------------------------------------------------------------+
|                             src/evallab/cards.py                               |
+---------------------------------------+----------------------------------------+
                                        |
                 +----------------------+----------------------+
                 |                                             |
                 v                                             v
    +-------------------------+                   +-------------------------+
    |   src/evallab/cohort.py |                   |   src/evallab/attach.py |
    +-------------------------+                   +-------------------------+
    | - summarize_job_evidence|                   | - attach()              |
    | - _task_evidence()      |                   | - trial_facts view      |
    | - _pass_any_first_k()  |                   | - reward_facts view     |
    | - bootstrap_mean_       |                   | - jobs view             |
    |   interval()            |                   +-------------------------+
    | - wilson_interval()     |
    | - power_requirements()  |
    +-------------------------+
```

### 3.1 Task as the Evidence Unit (Tenet T4)
Attempts from the same task are clustered by `task_digest` (or `task_name`). Realized first-k (`pass_any_first_k` / `pass_all_first_k`) orders eligible attempts by timezone-aware Harbor `started_at` and then asks whether any or all of those first $k$ attempts meet the threshold. Unbiased Chen/Yao fields (`pass_at_k_unbiased` / `pass_power_k_unbiased`) use every eligible attempt and no temporal order. `pass_at_k_probability(p, k)` is a model-based independent-attempt planning transform and is not either empirical estimator.
Repeated attempts on the same task are never treated as independent samples.

### 3.2 Mandatory Uncertainty and Underpowered Cohorts
- Every pass rate must carry sample size $n$ and a 95% bootstrap confidence interval.
- **Underpowered Cohorts**: When a cohort has fewer than 2 task evidence units ($n_{\text{tasks}} < 2$) or its bootstrap interval is unavailable, the card renders **`not distinguishable`** in the result line rather than displaying a misleading bare point estimate (e.g. `1.000` or `0.000`).
- "Not distinguishable" is treated as a first-class experimental result (v2 §5).

### 3.3 Separation of Harness Exceptions from Scored Failures
- A trial that terminates with a harness or runtime exception (`exception_class` is present, e.g. `ValueError`, `NonZeroAgentExitCodeError`) is **never measured** as a capability failure ($0.0$).
- Exception trials are excluded from the capability realized-first-k denominator and are reported separately under `Execution/harness exceptions` and explicitly highlighted in `Threats to validity`.

---

## 4. Human Review and Draft Lifecycle

All generated eval cards are **drafts pending human review**:

1. **Status Banner**: Retains the committed template banner:
   ```markdown
   Status: automatically drafted from completed evidence; human review required before publication.
   ```
2. **Reviewer Checklist**:
   - Confirm task and verifier identity (`task_digest`, `verifier_digest`).
   - Confirm the elicitation tuple accurately describes the execution environment.
   - Resolve the contamination note with concrete evidence (benchmark exposure, pretraining plausibility).
   - Decide whether the reported interval supports the intended claim.
   - Record reviewer identity, date, and publication disposition.
3. **No Automated Verdicts**: The card generator never marks a card published and never writes verdicts to permanent journals.

---

## 5. Lineage and Determinism

- **Deterministic**: Same evidence inputs produce byte-identical markdown cards. No execution timestamps or nondeterministic seeds appear in card bodies.
- **Lineage (E14)**: Every card metadata bundle carries an `inputs` array:
  ```json
  "inputs": [
    {"path": "queue/done/example-spec.json", "digest": "sha256:..."},
    {"path": "runs/example-job", "digest": "sha256:..."}
  ]
  ```
  This allows lineage tools to trace citable cards directly back to their immutable trial records.

---

## 6. CLI Usage

Generate an eval card from a completed `spec_id`, `job_id`, or file path:

```bash
# Render card to stdout
uv run evallab card generate canary-event-summary-20260815

# Write card to file
uv run evallab card generate queue/done/event-summary.json -o research/cards/event-summary.md

# Output JSON summary for automated pipelines
uv run evallab card generate canary-event-summary-20260815 --json
```
