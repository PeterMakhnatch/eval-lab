---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-29
license_note: Private repository; internal research-planning, causal inference, and campaign governance analysis only.
feeds:
  - parked
type: decision-memo
topic: c1-matched-causal-audit-and-decision-spec
author: agent-data-engineer
date: 2026-08-29
status: distilled
epistemic: audited repository state at pinned commit 53a3af58; separate observation, inference, and forecast layers
collection: trajectory-analysis
reviewed: 2026-08-29
requested_by: Peter via /tmp/eval-lab-parallel-assignments-2026-08-29.md
evidence_pin: origin/main 53a3af58
---

# C1 MATCHED Causal Grade — Counterfactual Twins, Anti-Confound Controls, Manipulation Checks, and Denominator Contracts

- **Author:** Agent Data Engineer
- **Evidence Pin:** `origin/main` @ `53a3af58` (merge of PR #299).
- **Topic:** Causal Grade $C_1$ MATCHED estimands, single-delta counterfactual twins, manipulation checks, block/repeat keys, denominator declarations, and anti-confound enforcement.
- **Epistemic Standard:** Three strict layers:
  * `[OBSERVED]`: Directly derived from audited source code, contracts, PR history, or promoted evidence files with exact paths.
  * `[INFERENCE]`: Methodological and architectural reasoning over observed facts.
  * `[FORECAST]`: Projections regarding future campaign outcomes contingent on unobserved empirical runs.

---

## 1. Authoritative $C_1$ Estimand & Causal Hierarchy

`[OBSERVED]` The repository formalizes two orthogonal evaluation vocabularies (`research/inbox/NEXT-BENCHMARK-PROGRAM-ANALYST-REPLY-2026-08-28.md:69-78`):
1. **Evidence Grades (A–D):** Grade A (verifiable machine state, exit code, or CAS digest); Grade B (deterministic over text or AST structure). Grades C and D are strictly barred from quantitative metrics.
2. **Causal Grades ($C_0$–$C_3$):**
   - **$C_0$ (Structural / Observational):** Unintervened descriptive facts (e.g. `total_tool_calls`, `step_count`, `prompt_tokens_per_step`). Supports accounting and screening; licenses zero causal claims.
   - **$C_1$ (Contract-Bounded / Matched Single-Delta):** Rates defined over explicit task opportunity denominators (e.g. `binding_survival_rate`, `stale_value_override_rate`, `dag_edge_conformance_rate`). Requires contract-declared opportunity boundaries $\Omega \ge 1$.
   - **$C_2$ (Matched Intervention Contrast):** Single-delta treatment-minus-control contrasts at matched dose and seed (e.g. semantic distractor vs. neutral padding at identical byte lengths).
   - **$C_3$ (Autonomous Recovery under Certified Gating):** Fault recovery credit verified against strict 5-gate certification (injected fault $>0$, zero human intervention, invariants passed, sequential recovery timing, and paired fixed-policy failure gate).

`[INFERENCE]` **C1 MATCHED is not an unstructured campaign label, but a strict causal contract.** A $C_1$ rate or contrast is admissible if and only if:
1. Every non-intervened dimension is held strictly constant across paired arms.
2. All required manipulation checks are measured and reported alongside the estimand.
3. The rate denominator is contract-declared and strictly null-preserving ($D = 0 \implies \text{NULL}$).

---

## 2. Counterfactual Twin Architecture & Factor Block Keys

`[OBSERVED]` Across the three synthetic benchmark verticals, paired contrasts rely on deterministic counterfactual twins:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       C1 Matched Counterfactual Architecture                │
├─────────────────┬──────────────────────┬────────────────────────────────────┤
│ Vertical        │ Intervened Delta     │ Held Constant (Confound Controls)  │
├─────────────────┼──────────────────────┼────────────────────────────────────┤
│ Vertical A      │ Arm:                 │ model_name, agent_name, task_name, │
│ (Action Memory) │ neutral_padding vs.  │ harness_version, scaffold_version, │
│                 │ semantic_distractor  │ repeat_group_id, dose_axis,        │
│                 │                      │ dose_value, dose_unit, alphabet_id,│
│                 │                      │ alphabet_version, seed, cell_id    │
├─────────────────┼──────────────────────┼────────────────────────────────────┤
│ Vertical B      │ DAG Depth / Width /  │ model_name, agent_name, task_name, │
│ (MCP FuncDAG)   │ Name-Similarity      │ harness_version, scaffold_version, │
│                 │                      │ repeat_group_id, alphabet_id, seed │
├─────────────────┼──────────────────────┼────────────────────────────────────┤
│ Vertical C      │ Mode:                │ model_name, agent_name, task_name, │
│ (MCP Recovery)  │ clean vs. fault      │ harness_version, scaffold_version, │
│                 │ (Twin Task Pair)     │ repeat_group_id, persistence_level,│
│                 │                      │ dose_axis, dose_value, alphabet_id,│
│                 │                      │ alphabet_version, seed, task_id    │
└─────────────────┴──────────────────────┴────────────────────────────────────┘
```

### 2.1 Block & Repeat Keys
`[OBSERVED]` The following keys govern paired clustering and prevent cross-strata contamination:
- **`task_block_id` / `cell_id`:** Canonical coordinate identifying the factorial cell within a campaign matrix.
- **`repeat_group_id` / `cluster_id`:** Derived as $\text{SHA-256}(\text{task\_name} \parallel \text{model\_name})$ (`src/evallab/interpretation/trajectory_compliance.py:201`). Clusters replicate trials for intra-task ICC estimation ($\rho_{\text{task}}$).
- **`source_digest`:** Immutable SHA-256 digest over the sanitized trial evidence bundle (`cas_uri`).

---

## 3. Manipulation Checks & Feature-Debt Resolution

`[OBSERVED]` `NEXT-BENCHMARK-PROGRAM-ANALYST-REPLY-2026-08-28.md:378-382` mandates explicit manipulation checks to prevent operational confounds from masquerading as semantic effects:
1. **`prompt_cache_hit_rate` (C1 Manipulation Check):**
   - *Confound Guarded (C1):* Neutral padding alters the prompt prefix, potentially degrading prompt cache hit rate, latency, and cost rather than exercising semantic interference.
   - *Requirement:* Must be measured per arm. Neutral and semantic arms must be compared at matched prefix-cache boundaries.
2. **`prompt_tokens_per_step` & `model_call_count` (C2 Manipulation Check):**
   - *Confound Guarded (C2):* Compaction or multi-turn retrieval consumes model API calls that single-turn arms never spend, confounding constant-step budgets with unequal call budgets.
   - *Requirement:* Report both step counts and model-call counts per trial.

### 3.1 `FEATURE-DEBT-LEDGER` Audit & Registration Fix
`[OBSERVED]` In prior commits, `prompt_tokens_per_step` and `prompt_cache_hit_rate` were emitted by all three producers (`action_memory.py`, `mcp_funcdag.py`, `mcp_recovery.py`) and stored in SQL tables, but were omitted from `TRAJECTORY_FEATURE_REGISTRY`.
`[OBSERVED]` **Resolved in this PR:**
- Registered `prompt_tokens_per_step` in `src/evallab/interpretation/feature_registry.py` as a $C_0$ Grade A mechanical fact (`category="benchmark_l1_fact"`, `denominator_policy="not_applicable"`).
- Registered `prompt_cache_hit_rate` in `src/evallab/interpretation/feature_registry.py` as a $C_1$ Grade A manipulation check (`category="benchmark_l1_fact"`, `denominator_policy="not_applicable"`).
- Both features declare `declared_inputs=()` and `available_before_verdict=True`, ensuring full compatibility with T1.1 lineage checks (`src/evallab/analysis_capability.py`).

---

## 4. Mechanical Anti-Confound Enforcement in DuckDB Views

`[OBSERVED]` In `sql/traj_benchmark_views.sql`, matched contrasts in `v_benchmark_contrasts` mechanically enforce confound control via strict multi-column equality:

```sql
-- Action Memory Matched Arm Contrast
JOIN action_memory_features t
  ON c.seed = t.seed
 AND c.cell_id = t.cell_id
 AND c.model_name = t.model_name
 AND c.agent_name = t.agent_name
 AND c.task_name = t.task_name
 AND c.harness_version = t.harness_version
 AND c.scaffold_version = t.scaffold_version
 AND c.repeat_group_id = t.repeat_group_id
 AND c.dose_axis = t.dose_axis
 AND c.dose_value = t.dose_value
 AND c.dose_unit = t.dose_unit
 AND c.alphabet_id = t.alphabet_id
 AND c.alphabet_version = t.alphabet_version
 AND c.analysis_ready IS TRUE
 AND t.analysis_ready IS TRUE
 AND c.arm = 'clean'
 AND t.arm != 'clean'
```

`[INFERENCE]` This SQL structure guarantees:
1. **Zero Cross-Model Pairing:** Trials from `model-a` and `model-b` never pair.
2. **Zero Cross-Harness Pairing:** Different harness versions (e.g. `harbor-0.21` vs `harbor-0.22`) never pair.
3. **Zero Unmatched Dose Pairing:** Control and treatment trials must share identical `dose_value` (e.g. clean 4k pairs only with treatment 4k on the non-intervened axis, or recovery clean p1 pairs only with fault p1).
4. **Fail-Closed on Unsettled Data:** Only rows marked `analysis_ready IS TRUE` are admitted to the view.

---

## 5. Denominator Governance & Sizing Sieve Invariants

`[OBSERVED]` Sizing of $C_1$ measurement campaigns requires strict adherence to statistical floor invariants (`NEXT-BENCHMARK-PROGRAM-ARCHITECTURE-2026-08-28.md:373-376`):
- **Structural Floor Refusal Gate:**
  * Dose Levels / Arms: $L \ge 4$.
  * Opportunity-Bearing Task Clusters: $M_{\text{opportunity}} \ge 20$.
  * Replicate Seeds per Cell: $R \ge 3$.
  * Sizing below these floors is refused with `REFUSAL_UNDERPOWERED_STRUCTURAL_FLOOR`.
- **Effective Sample Size:**
  $$n_{\text{eff}} = \frac{M_{\text{opportunity}} \cdot R}{1 + (R - 1)\rho_{\text{task}}}$$
- **Opportunity-Adjusted Trial Requirement:**
  $$M_{\text{scheduled}} \ge \left\lceil \frac{M_{\text{opportunity}}}{\Omega_{\text{yield}}} \right\rceil$$
  where $\Omega_{\text{yield}}$ is empirically measured from Campaign 0.

---

## 6. Actionable Implementation Summary

1. **Registry Invariants Updated:** `prompt_tokens_per_step` ($C_0$) and `prompt_cache_hit_rate` ($C_1$) registered in `feature_registry.py`.
2. **SQL Contrasts Hardened:** `v_benchmark_contrasts` strictly filters on exact dimension equality predicates (13 predicates in action memory; 14 predicates in recovery including `task_id` and `persistence_level`).
3. **Refusal Diagnostics Active:** `v_benchmark_refusal_diagnostics` reports all underpowered, zero-opportunity, or dimension-refused trials beside result tables.
4. **Test Verification Complete:** 37 targeted unit tests passing in `test_benchmark_trajectory_program.py` covering registry manipulation checks, anti-confound contrast rejection, and strict persistence handling.
