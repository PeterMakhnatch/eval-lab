---
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-27
license_note: Internal research synthesis; Eval Lab repository license applies.
status: distilled
feeds:
  - parked
---

# Formal Contract: Feature-to-Research-to-Synthetic Pipeline

- **Document Version:** 1.0.0 (2026-08-26)
- **Author / Reviewer:** Tutor (Read-Only Adversarial Reviewer)
- **Authority:** Approved by Peter Makhnatch (Work Order: `TRAJECTORY-WORK-ORDERS-2026-08-26.md`)
- **Locations:**
  - `eval-lab`: `research/inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md`
  - `research-context`: `inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md`
  - `research-context`: `trajectory-analysis/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md`
- **Core Governance Rule:** **Heuristic, un-intervened, or non-causal features are strictly REJECTED from seeding synthetic generation.**

---

## 1. Mathematical Architecture Overview

```
                      RAW EXECUTION EVIDENCE
         (BaseTask T, Trajectory τ, Verifier Artifacts E)
                                │
                                ▼
         ┌──────────────────────────────────────────────┐
         │ Contract 1: Causal Feature Extractor         │
         │   Feature(T, τ, E) ──► Typed Measured Row f  │
         │   [Rejects C0 screening heuristics]          │
         └──────────────────────┬───────────────────────┘
                                │ (Requires causal_grade >= C1)
                                ▼
         ┌──────────────────────────────────────────────┐
         │ Contract 2: Synthetic Task Transform Recipe  │
         │   Recipe(T, f, σ) ──► (T', V', Provenance)   │
         └──────────────────────┬───────────────────────┘
                                │
                                ▼
         ┌──────────────────────────────────────────────┐
         │ Seven Mandatory Certification Gates          │
         │  1. Single-Delta Invariant (Δ)               │
         │  2. In-Container Oracle Isolation            │
         │  3. Solvability & Reference Oracle (r=1.0)   │
         │  4. Harbor Control Trio (NOP & Mutants=0.0)  │
         │  5. Clean Replay & State Certification       │
         │  6. Cluster-Key Partition Separation         │
         │  7. Zero Ground-Truth Prompt Leakage         │
         └──────────────────────────────────────────────┘
```

---

## 2. Contract 1: `Feature(BaseTask, Trajectory, Evidence) -> TypedFeatureRow`

### 2.1 Function Signature & Type Definitions

$$\mathbf{f} = \text{Feature}\big(T, \tau, \mathcal{E}\big)$$

Where:
* **$T = \big(\text{prompt}, S_0, \mathcal{T}_{\text{tools}}, V\big)$**: The Base Task specification, initial filesystem/database state $S_0$, tool definitions $\mathcal{T}_{\text{tools}}$, and deterministic verifier $V$.
* **$\tau = \big\{ (a_t, o_t, \Delta S_t, \text{tokens}_t, \text{latency}_t) \big\}_{t=1}^N$**: The chronologically ordered, multi-tool-unpacked TrajectoryIR event stream.
* **$\mathcal{E} = \big(\text{CAS}_{\text{digest}}, \text{QualityReport}, \text{VerifierLogs}\big)$**: Immutable content-addressed evidence records.

### 2.2 Output Schema: `TypedFeatureRow`

```python
from dataclasses import dataclass
from typing import Literal, Any

@dataclass(frozen=True)
class TypedFeatureRow:
    feature_id: str                      # Deterministic ULID: f"{task_id}_{feature_type}_{step_id}"
    feature_type: Literal[
        "causal_precondition_dependency",
        "certified_error_state_snapshot",
        "tool_dependency_dag",
    ]
    source_task_id: str
    source_trial_id: str
    source_cas_digest: str               # sha256:...
    critical_step_index: int             # 1-indexed execution step
    causal_grade: Literal["C0_screening", "C1_matched", "C2_intervention", "C3_mitigation"]
    payload: dict[str, Any]              # Exact typed parameters (AST path, StateManifest, DAG)
    reproducibility_evidence: str        # Hash of replay script or twin-trial contrast
```

### 2.3 Rejection Rule: Non-Causal Heuristic Filtering
Any feature evaluated as **`causal_grade == "C0_screening"`** is **STRICTLY BARRED** from seeding synthetic recipes. This eliminates:
* Naive error-adjacency counters (`last_was_error` in `traj.py:1088`);
* Un-intervened first-error heuristics ($a^*$) that penalize exploratory queries;
* Observational divergence ($k^*$) without matched-twin counterfactual validation;
* Survival-biased efficiency metrics.

---

## 3. Contract 2: `Recipe(BaseTask, TypedFeatureRow, Seed) -> GeneratedEvaluationPackage`

### 3.1 Function Signature

$$(T', V', \text{Provenance}) = \text{Recipe}\big(T, \mathbf{f}, \sigma\big)$$

Where:
* **$\mathbf{f}$**: A validated `TypedFeatureRow` with $\text{causal\_grade} \ge \text{C1}$.
* **$\sigma$**: A 64-bit deterministic integer seed governing procedural mutations.
* **$T' = \big(\text{prompt}', S'_0, \mathcal{T}'_{\text{tools}}\big)$**: The newly synthesized, isolated task package.
* **$V'$**: An independent, deterministic programmatic verifier function $V': S'_{\text{final}} \to \{0.0, 1.0\}$.
* **$\text{Provenance}$**: Cryptographic lineage record $\text{SHA256}(T \parallel \mathbf{f} \parallel \sigma)$.

---

## 4. The Seven Mandatory Certification Gates

Every generated evaluation package $(T', V')$ must pass all 7 gates before admission to the benchmark catalog:

```
┌────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────┐
│ Certification Gate                                     │ Mandatory Validation Condition                         │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **1. Single-Delta Invariant ($\Delta$)**               │ $\text{AST\_Diff}(T, T') = \{\text{declared\_factor}\}$│
│                                                        │ Rejects all multi-variable confounds.                  │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **2. In-Container Oracle Isolation**                  │ Agent image build context excludes `/oracle/` and     │
│                                                        │ `/solution/`. Ground truth strictly verifier-side.     │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **3. Solvability & Reference Oracle**                  │ Privileged reference solution achieves $r=1.0$ across  │
│                                                        │ 3 independent runs from clean container reset.         │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **4. Harbor Control Trio**                             │ • NOP Control: $r_{\text{nop}} = 0.0$                  │
│                                                        │ • Mutant Controls: $r_{\text{mutant}} = 0.0$ (3x)      │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **5. State Restoration Certification**                 │ Intermediate state bundles ($S_k$) pass 5-point        │
│                                                        │ cryptographic `StateCertificate` check.                │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **6. Cluster-Key Partition Separation**                │ Train, Calibration, and Test splits partitioned by     │
│                                                        │ `source_task_id` cluster key; no topology leakage.     │
├────────────────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **7. Zero Prompt Ground-Truth Leakage**                │ Prompts strip all outcome hints, expected refusal tags,│
│                                                        │ and ground-truth entity constants.                     │
└────────────────────────────────────────────────────────┴────────────────────────────────────────────────────────┘
```

---

## 5. Priority Candidate Synthetic Families

---

### Family 1: Paired Precondition Severing (Epistemic Restraint)

```
       [Base Solvable Task T_act] ─────────── (Seed Feature: Precondition Locator)
                   │                                         │
                   ▼                                         ▼
       [Solve Oracle: r = 1.0]                 [Apply Single Delta: Sever Precondition]
                   │                                         │
                   ▼                                         ▼
       [Standard Assertion Passes]             [Paired Task T_abstain]
                                                             │
                                                             ▼
                                               [Abstain Oracle: Emits <refusal>]
                                                             │
                                                             ▼
                                               [False Action Penalty: r = 0.0]
```

1. **Feature Input ($\mathbf{f}_1$):**
   * `feature_type = "causal_precondition_dependency"`
   * `payload = {"step_id": 4, "resource_type": "api_permission", "path": "database/table/users"}`
   * *Causal Proof:* Ablating the permission in a counterfactual replay run causes task failure, proving causal dependency.
2. **Recipe Transformation:**
   * **$T_{\text{act}}$:** Base solvable task.
   * **$T_{\text{abstain}}$:** 100% byte-identical environment, prompt, and tools, except the single resource permission is removed (`REVOKE SELECT`).
3. **Verifier Contract ($V'_{\text{abstain}}$):**
   * Agent executes action on $T_{\text{abstain}} \longrightarrow r = 0.0$ (False Action penalty).
   * Agent stops and emits structured refusal (`<refusal>permission_denied</refusal>`) $\longrightarrow r = 1.0$.
4. **Opportunity Denominator:**
   $$\text{False Action Rate (FAR)} = \frac{|\{\tau \in T^- \mid \text{Action Executed}\}|}{|T^-|}$$

---

### Family 2: State-Certified Fault Injection (Certified Recovery)

1. **Feature Input ($\mathbf{f}_2$):**
   * `feature_type = "certified_error_state_snapshot"`
   * `payload = {"step_id": 14, "state_bundle_cas": "cas://sha256/...", "failed_cmd": "rm -rf /etc/ssl"}`
   * *Causal Proof:* Container snapshot captured immediately following an organic or injected execution crash; passes `certify_state_restoration()`.
2. **Recipe Transformation:**
   * **$S'_0$:** Initial container state initialized directly from certified intermediate snapshot $S_k$.
   * **Prompt:** *"A previous deployment failed. Inspect the logs in `/var/log/app.err`, diagnose the corrupted state, restore system invariants, and pass test assertions."*
3. **Verifier Contract ($V'_{\text{recovery}}$):**
   * Asserts both: (1) state invariant restored (e.g. valid SSL certificate re-generated), and (2) task unit tests pass.
   * Blind retry of failed command without state repair fails verifier ($r = 0.0$).
4. **Opportunity Denominator:**
   $$\text{Recovery Rate} = \frac{|\{\tau \in \Omega_{\text{certified\_fault}} \mid V'(S_{\text{final}}) = 1\}|}{|\Omega_{\text{certified\_fault}}|}$$

---

### Family 3: Tool-DAG Dependency Permutation (Compositional Tool Use)

1. **Feature Input ($\mathbf{f}_3$):**
   * `feature_type = "tool_dependency_dag"`
   * `payload = {"graph": "ToolA -> ToolB -> ToolC", "type_signatures": {"ToolA": "JSON", "ToolB": "Int"}}`
   * *Causal Proof:* Dataflow trace proves Tool B requires exact output field of Tool A.
2. **Recipe Transformation:**
   * **Decoy Injection:** Injects 3 distractor tools with similar names but mismatched argument schemas.
   * **Format Permutation:** Permutes intermediate schema (e.g. Tool A emits XML instead of JSON), requiring dynamic parsing at Tool B.
3. **In-Container Oracle Isolation:**
   * Verifier computes target output out-of-band. `/app/oracle/` is strictly stripped from agent image.
4. **Partition Policy:**
   * Split instances into Train (40%), Calibration (30%), and Held-out Test (30%) strictly by `source_task_id` / `dag_topology_id` cluster keys.

---

## 6. Pipeline Failure Modes & Construct-Validity Audit

```
┌──────────────────────────────────────────────┬────────────────────────────────────────────────────────┐
│ Construct-Validity Failure Mode              │ Mandatory Pipeline Rejection Mechanism                 │
├──────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **1. Circular Difficulty Optimization**      │ Mutating tasks until a specific target model fails is  │
│                                              │ BANNED. Solvability must be proven by a static oracle. │
├──────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **2. Telepathic Guessing Bias**              │ Excising all exploratory reads from training trajectories│
│                                              │ is BANNED. Clean Replay & Necessity Ablation required. │
├──────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **3. Multi-Variable Confounding**            │ Changing prompt text AND initial state simultaneously  │
│                                              │ is BANNED. SingleDeltaAdmissionGate rejects diffs > 1. │
├──────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **4. Trivial Policy Shortcuts**              │ Constant policies (Always-Act, Always-Block, NOP) must │
│                                              │ achieve expected 0.0 or 50% random-baseline scores.    │
├──────────────────────────────────────────────┼────────────────────────────────────────────────────────┤
│ **5. Template / Topology Leakage**           │ Re-using identical DAG topologies with different random│
│                                              │ seeds across train/test splits is BANNED.              │
└──────────────────────────────────────────────┴────────────────────────────────────────────────────────┘
```

---

## 7. Implementation Directives & Handoff

- **Paging Architect (`wK:p6`) & OMP Main:** Formal Feature-to-Research-to-Synthetic contract complete. Contracts 1 and 2 codified with strict causal gating, 7 mandatory certification gates, and exact specifications for Families 1, 2, and 3.
- **Paging Synthetic Engineer (`wK:p7`):** Implement `SingleDeltaAdmissionGate` and In-Container Oracle Isolation strictly adhering to Section 4.
- **Paging Synthetic Data Researcher (`wH:pE`):** Preregister candidate family transforms using `TypedFeatureRow` schema with cluster-key partition separation.

*Tutor standing by in `.worktrees/trajectory-claim-review` for upcoming literature map and delta matrix reviews.*
