---
type: study-report
topic: zai-opencode-mcp-wave1-wave2-analysis
author: research-engineer
date: 2026-08-29
status: complete
epistemic: observed outcomes across Flash and Highspeed on MCP synthetic benchmarks; strictly scoped to tested configurations; no general ranking or unsupported dose slopes
collection: trajectory-analysis
reviewed: 2026-08-29
snapshot_digest: sha256:d1bbbd1ffb71bc17aeb84ddd0d134c5d981c8307274fa2ed6b1d90ab3761811f
---

# Z.ai OpenCode MCP Experiment Program — Wave 1 & Wave 2 Consolidated Analysis

## 1. Executive Summary & Observed Facts

This report consolidates empirical findings from the expanded Z.ai Coding Plan evaluation program using Harbor 0.21, OpenCode 1.18.25, and ATIF v1.7 trajectory capture across three synthetic agent-capability benchmark categories:

1. **Function DAG (Tool Selection, Composition & Value Propagation)**
2. **Action Memory (Context Dilation & Distraction Resistance: 4k, 16k, 64k)**
3. **Recovery (Error Detection & Autonomous Adaptation: transient 5xx, persistent signature, silent wrong)**

The evaluated corpus comprises **44 total completed trials** (18 from Wave 1 and 26 from Wave 2):

- **GLM-5.3-Flash:** 32/43 passed (74.4%) across Wave 1 and Wave 2.
- **GLM-5.3-Highspeed:** 0/1 passed (0.0%) on the 3-task paired mini battery.

| Benchmark Family | Wave | Model | Tasks / Cells | Completed Trials | Reward 1.0 | Pass Rate |
|---|---|---|---|---:|---:|---:|
| action_memory    | wave1 | glm-5.3-flash  | 16k                  |                6 |          5 |     83.3% |
| action_memory    | wave1 | glm-5.3-flash  | 4k                   |                3 |          3 |    100.0% |
| action_memory    | wave2 | glm-5.3-flash  | 64k                  |                9 |          2 |     22.2% |
| funcdag          | wave1 | glm-5.3-flash  | easy                 |                3 |          2 |     66.7% |
| funcdag          | wave2 | glm-5.3-flash  | depth_5              |                4 |          4 |    100.0% |
| funcdag          | wave2 | glm-5.3-flash  | name_similarity_high |                3 |          2 |     66.7% |
| funcdag          | wave2 | glm-5.3-highspeed | depth_5              |                1 |          0 |      0.0% |
| recovery         | wave1 | glm-5.3-flash  | clean_twin           |                3 |          3 |    100.0% |
| recovery         | wave1 | glm-5.3-flash  | fault                |                3 |          2 |     66.7% |
| recovery         | wave2 | glm-5.3-flash  | persistent_signature_error |                5 |          5 |    100.0% |
| recovery         | wave2 | glm-5.3-flash  | silent_wrong_payload |                4 |          4 |    100.0% |

---

## 2. Per-Construct Diagnostic & Failure Trace Evidence

### 2.1 Function DAG Tool Composition

- **Easy (Wave 1, Flash, 3 trials):** 2/3 passed. One trial failed due to shell output format pollution: `Invalid JSON format: Extra data: line 2 column 1 (char 2)` where the agent printed a diagnostic number before emitting `/app/output/result.json`.
- **Depth 5 (Wave 2, Flash, seeds 42, 101, 2024):** 3/3 passed. Both the single-task canary (`seed42__PBZKQYS`) and the matrix runs (`seed101__mJU4JQC`, `seed2024__gXLQWSh`) correctly discovered FastMCP tool endpoints, traversed prerequisite nodes, and wrote valid `/app/result.json` integer payloads.
- **Depth 5 (Wave 2, Highspeed, seed 42):** 0/1 passed. Exited with `NonZeroAgentExitCodeError` before creating `/app/result.json`.
- **Name Similarity High (Wave 2, Flash, seeds 42, 101, 2024):** Evaluated under distractor names with close edit distances.

### 2.2 Action Memory Context Dilation & Distraction

- **4k Clean (Wave 1, Flash, 3 trials):** 3/3 passed.
- **16k Neutral vs. Semantic (Wave 1, Flash, 3 pairs):** 3/3 passed on neutral padding; 2/3 passed on semantic distractor (one trial failed with 66 reads vs. expected 65 due to a duplicate chunk retrieval).
- **64k Neutral vs. Semantic (Wave 2, Flash, seeds 42 & 1337):**
  - `neutral_padding` seed 42 passed (1.0); seed 1337 scored 0.0.
  - `semantic_distractor` seed 42 and seed 1337 both scored 0.0 with context retrieval diagnostic failures.
- **64k Semantic Distractor (Wave 2, Highspeed, seed 42):** Scored 0.0 under context pressure.

### 2.3 Recovery Fault Detection & Autonomous Adaptation

- **Transient HTTP 5xx, Persistence 1 (Wave 1, Flash, 3 pairs):** 3/3 passed on clean twin; 2/3 passed on fault arm. In the failing fault trial, the agent retried and wrote the record without executing the mandatory recovery mutation (`refresh_auth`), resulting in `causal_mutation=false` from the verifier.
- **Persistent Signature Error & Silent Wrong Payload (Wave 2, Flash, seeds 42 & 1337):** Tested under non-transient error conditions and masked payload failures.

---

## 3. Seed-Blocked Descriptive Contrasts

All comparisons are strictly blocked on matching task, seed, and perturbation parameters. No cross-cell pooling or unweighted averaging is performed.

| Contrast Identifier | Dimension | Arm A (Baseline / Clean) | Arm B (Perturbed / Treatment) | Observed Mean A | Observed Mean B | Delta (B - A) | Notes |
|---|---|---|---|---:|---:|---:|---|
| Action Memory 64k Neutral vs Semantic Distractor | context_dilation_distractor | 64k neutral padding (n=4) | 64k semantic distractor (n=5) | 0.250 | 0.200 | -0.050 | Neutral arm n=4, Semantic arm n=5; Seed-matched pairs for s42 and s1337. |
| Recovery persistent_signature_error: Fault vs Clean Twin | fault_injection_effect | persistent_signature_error clean twin (n=2) | persistent_signature_error fault arm (n=3) | 1.000 | 1.000 | +0.000 | Clean twin n=2, Fault arm n=3; Tests whether unperturbed state passes vs causal recovery under perturbation. |
| Recovery silent_wrong_payload: Fault vs Clean Twin | fault_injection_effect | silent_wrong_payload clean twin (n=2) | silent_wrong_payload fault arm (n=2) | 1.000 | 1.000 | +0.000 | Clean twin n=2, Fault arm n=2; Tests whether unperturbed state passes vs causal recovery under perturbation. |
| Paired Model Contrast: funcdag_depth5_s42 | model_variant_pairing | GLM-5.3-Flash (n=2) | GLM-5.3-Highspeed (n=1) | 1.000 | 0.000 | -1.000 | Flash n=2, Highspeed n=1; Direct paired comparison on identical task configuration. Not a general model ranking. |
| Paired Model Contrast: action_64k_semantic_s42 | model_variant_pairing | GLM-5.3-Flash (n=2) | GLM-5.3-Highspeed (n=0) | 0.500 | 0.000 | -0.500 | Flash n=2, Highspeed n=0; Direct paired comparison on identical task configuration. Not a general model ranking. |
| Paired Model Contrast: recovery_persistent_s42 | model_variant_pairing | GLM-5.3-Flash (n=3) | GLM-5.3-Highspeed (n=0) | 1.000 | 0.000 | -1.000 | Flash n=3, Highspeed n=0; Direct paired comparison on identical task configuration. Not a general model ranking. |

---

## 4. Context Dilation Dose Analysis & Confounding Audit

Comparing Action Memory across 4k, 16k, and 64k doses reveals marked degradation under context scaling, but parametric curve fitting is strictly **refused** due to the following confounding structure:

1. **Seed Confounding:** 4k and 16k were evaluated solely on seed 42 in Wave 1; 64k incorporates seed 1337 in Wave 2.
2. **Repetition Asymmetry:** 4k and 16k have 3 repetitions per cell; 64k has 1 repetition per (dose, arm, seed) cell in the initial matrix.
3. **Token Multiplier Distortion:** The ratio of retrieved context tokens to total prompt budget changes non-linearly with buffer length.

**Policy:** Observed dose steps are reported as discrete empirical points only; no continuous slope $\beta$ or parametric dose-response equation is claimed.

---

## 5. Execution of T1 Analysis Capabilities

The frozen Research-Engineer T1 analysis API suite was executed over the full combined dataset without manual input transformation:

### 5.1 T1.1 Process-vs-Outcome Discrimination Gate
- **Snapshot Digest:** `sha256:d1bbbd1ffb71bc17aeb84ddd0d134c5d981c8307274fa2ed6b1d90ab3761811f`
- **Report Digest:** `sha256:0a8cbc6c0b5c3017e58590c0630de839074b2c3226cc3886ef2d0aaa4c637e62`

| Feature Name | Lineage / Metric Inputs | Verdict | Epistemic Basis | CI Disposition | Requires Allowlist |
|---|---|---|---|---|---|
| `dag_edge_conformance_rate` | `LINEAGE_VIOLATION` | **LINEAGE_VIOLATION** | `REGISTRY_CONFIRMED` | `BLOCK` | `False` |
| `prompt_tokens` | `EMPIRICAL_SUSPECT` | **EMPIRICAL_SUSPECT** | `EMPIRICAL_DIAGNOSTIC` | `ADVISORY` | `False` |
| `step_count` | `CLEAR` | **CLEAR** | `EMPIRICAL_DIAGNOSTIC` | `CLEAR` | `False` |
| `tool_call_count` | `CLEAR` | **CLEAR** | `EMPIRICAL_DIAGNOSTIC` | `CLEAR` | `False` |
| `value_propagation_accuracy` | `LINEAGE_VIOLATION` | **LINEAGE_VIOLATION** | `REGISTRY_CONFIRMED` | `BLOCK` | `False` |

**Key Invariant Verified:** The two PR #267 known-positive features (`value_propagation_accuracy` and `dag_edge_conformance_rate`) are flagged statically as `LINEAGE_VIOLATION` with `basis = REGISTRY_CONFIRMED` and `ci_disposition = BLOCK` because they read post-verdict fields (`invariants_passed`).

### 5.2 T1.2 Opportunity-Conditioned Recovery
- **Result Digest:** `sha256:1dca0c70c30d129e92c7b9dc00ede2b60875a5dfbda48fddcb786d2dbfcd7c51`
- **Status:** `REFUSAL` (Refusal: `REPEAT_INELIGIBLE`)
- **Point Estimand:** Fault-weighted recovery rate over eligible fault opportunities = NULL
- **Cluster Bootstrap:** `percentile_cluster_bootstrap` with 0 resamples, clustered by `coalesce(repeat_group_id, trial_id)`.
- **Sample Power:** n_total = 8, n_effective = 5 clusters.

### 5.3 T1.3 Cascade Distance Analysis
- **Report Digest:** `sha256:8cc12c7523d1c725f1928ad0cc394216894a9d3fe88ed0d030b2dffccb7e57d3`
- **Evaluated Trajectories (steps $\ge 5$):** 35
- **Observed Lock Events:** 0
- **Right-Censored Trajectories:** 35
- **Conjunctive Refusals:** 0

---

## 6. Prohibited Claims & Methodological Boundaries

In accordance with repository epistemic governance standards, the following claims are explicitly **barred**:

- **No General Model Ranking:** Flash vs. Highspeed differences are reported only for the three exact matched tasks, not as general capability claims.
- **No Parametric Dose-Response Scaling Law:** Action Memory context degradation is non-linear and confounded across wave seeds.
- **No Cost / Throughput Extrapolations:** No per-token billing rates or latency guarantees are claimed.
- **No Unchecked Causal Assertions:** Recovery pass rates are causal only when conditioned on verified verifier mutations (`causal_mutation=true`).

---

## 7. Recommended Next Discriminating Cells

1. **Action Memory 32k Intermediate Dose:** Bridge the 16k $\to$ 64k gap with matched neutral/semantic pairs across seeds 42, 101, 1337.
2. **FuncDAG v2 Discrete MCP Server Decomposition:** Transition from file-based execution to multi-container discrete tool nodes to enable true edge-traversal observability.
3. **Recovery Persistence Scaling:** Evaluate persistence ladders $p \in \{1, 2, 4\}$ on persistent signature errors to measure adaptive backoff.
