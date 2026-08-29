---
type: study-report
topic: zai-opencode-mcp-wave1-wave2-analysis
author: research-engineer
date: 2026-08-29
status: complete
epistemic: observed outcomes across Flash, Full, and Highspeed on MCP synthetic benchmarks; strictly scoped to tested configurations; no general ranking or unsupported dose slopes
collection: trajectory-analysis
reviewed: 2026-08-29
snapshot_digest: sha256:dbaa75f35816e2374ffa1c0f3b9af43ceb0975b07daa59815bdba333d096a478
---

# Z.ai OpenCode MCP Experiment Program — Wave 1 & Wave 2 Consolidated Analysis

## 1. Executive Summary & Observed Facts

This report consolidates empirical findings from the expanded Z.ai Coding Plan evaluation program using Harbor 0.21, OpenCode 1.18.25, and ATIF v1.7 trajectory capture across three synthetic agent-capability benchmark categories:

1. **Function DAG (Tool Selection, Composition & Value Propagation)**
2. **Action Memory (Context Dilation & Distraction Resistance: 4k, 16k, 64k)**
3. **Recovery (Error Detection & Autonomous Adaptation: transient 5xx, persistent signature, silent wrong)**

The evaluated corpus comprises **47 total attempts** (45 scored trials + 2 infrastructure exclusions):

- **Combined Program Scored Total:** 33/45 passed (73.3%) across Wave 1 (15/18) and Wave 2 (18/27).
- **Wave 2 Scored Total:** 18/27 passed (66.7% across 27 scored trials):
  - **GLM-5.3-Flash (Wave 2):** 16/24 passed (16/24, 66.7%).
  - **GLM-5.3 Full (Wave 2):** 2/3 passed (2/3, 66.7% — FuncDAG depth 5 canary 1/1, Recovery persistent signature 1/1, Action 64k semantic 0/1).
- **GLM-5.3-Highspeed:** 0/0 scored trials (all 3 mini-battery attempts were excluded from scored denominators due to upstream subscription HTTP 429 access restrictions).
- **Infrastructure Exclusions (Non-Scored):** 2 trials (Highspeed subscription 429 entitlement error and default-timeout sequential chunk retrieval `AgentTimeoutError`).

| Benchmark Family | Wave | Model | Tasks / Cells | Completed Trials | Reward 1.0 | Pass Rate |
|---|---|---|---|---:|---:|---:|
| action_memory    | wave1 | glm-5.3-flash  | 16k                  |                6 |          5 |     83.3% |
| action_memory    | wave1 | glm-5.3-flash  | 4k                   |                3 |          3 |    100.0% |
| action_memory    | wave2 | glm-5.3-flash  | 64k                  |               10 |          3 |     30.0% |
| action_memory    | wave2 | glm-5.3-full   | 64k                  |                1 |          0 |      0.0% |
| funcdag          | wave1 | glm-5.3-flash  | easy                 |                3 |          2 |     66.7% |
| funcdag          | wave2 | glm-5.3-flash  | depth_5              |                3 |          3 |    100.0% |
| funcdag          | wave2 | glm-5.3-flash  | name_similarity_high |                3 |          2 |     66.7% |
| funcdag          | wave2 | glm-5.3-full   | depth_5              |                1 |          1 |    100.0% |
| recovery         | wave1 | glm-5.3-flash  | clean_twin           |                3 |          3 |    100.0% |
| recovery         | wave1 | glm-5.3-flash  | fault                |                3 |          2 |     66.7% |
| recovery         | wave2 | glm-5.3-flash  | persistent_signature_error |                4 |          4 |    100.0% |
| recovery         | wave2 | glm-5.3-flash  | silent_wrong_payload |                4 |          4 |    100.0% |
| recovery         | wave2 | glm-5.3-full   | persistent_signature_error |                1 |          1 |    100.0% |

---

## 2. Per-Construct Diagnostic & Failure Trace Evidence

### 2.1 Function DAG Tool Composition

- **Easy (Wave 1, Flash, 3 trials):** 2/3 passed. One trial failed due to shell output format pollution: `Invalid JSON format: Extra data: line 2 column 1 (char 2)` where the agent printed a diagnostic number before emitting `/app/output/result.json`.
- **Depth 5 (Wave 2, Flash, seeds 42, 101, 2024):** 3/3 passed. Both the single-task canary (`seed42__PBZKQYS`) and the matrix runs (`seed101__mJU4JQC`, `seed2024__gXLQWSh`) correctly discovered FastMCP tool endpoints, traversed prerequisite nodes, and wrote valid `/app/result.json` integer payloads.
- **Depth 5 (Wave 2, Full GLM-5.3, seed 42):** 1/1 passed (`seed42__Ca9ToPk`).
- **Depth 5 Highspeed Attempt:** The single planned Highspeed attempt (`mcp-funcdag-depth_5-seed42__q22U798`) threw `NonZeroAgentExitCodeError` before creating `/app/result.json` due to an upstream provider subscription entitlement error (HTTP 429: 'Your current subscription plan does not yet include access to GLM-5.3-Highspeed'). It is classified as an observed non-scored access failure, not an agent reasoning outcome.
- **Name Similarity High (Wave 2, Flash, seeds 42, 101, 2024):** 2/3 passed (seeds 42 and 101 passed; seed 2024 failed value propagation).

### 2.2 Action Memory Context Dilation & Distraction

- **4k Clean (Wave 1, Flash, 3 trials):** 3/3 passed.
- **16k Neutral vs. Semantic (Wave 1, Flash, 3 pairs):** 3/3 passed on neutral padding; 2/3 passed on semantic distractor (one trial failed with 66 reads vs. expected 65 due to a duplicate chunk retrieval).
- **64k Seed 42 vs. Seed 1337 (Wave 2, Flash):**
  - **Seed 42:** Both `neutral_padding` (1.0) and `semantic_distractor` (1.0) passed on Flash with complete 257/257 chunk retrieval and exact state binding.
  - **Seed 1337:** Failed across unscaffolded neutral (0/3) and semantic (0/3) runs (matrix and repeats).
- **64k Seed 1337 Handle-Level Failure Analysis (Wave 2, Flash, 6 trials):**
  All six seed 1337 Action64 trials failed (0/3 neutral padding, 0/3 semantic distractor across the matrix and repeat runs). Detailed trajectory trace parsing reveals this is a **specific agent issuance, transcription, and long-sequence maintenance failure mode**, rather than complete coverage, simple reordering, or a pure token-capacity deficit:
  1. **ATIF Issuance vs. Benchmark Events Alignment:** ATIF tool-call issuance order strictly equals the server `benchmark-events.jsonl` execution order across all six failures. This rules out server-side capture anomalies, transport dropouts, or async network reordering.
  2. **Omitted Handle Invariant:** In all 6 trials, the agent omitted the valid listed chunk `ctx_2110473c018845ab0cc32bf4` (`...32bf4`).
  3. **Near-Typo Hallucination:** In 5 of 6 trials (`xTVP9AZ`, `Qoz3nbU`, `wCHLZ4M`, `FLiG7jy`, `Pgukjp8`), the agent transcribed a 1-character hallucinated typo handle `ctx_2110473c018845ab0cc32bf6` (`...32bf6`). In 1 trial (`JvdEs9Y`), the agent transcribed `ctx_2110473c018845ab0cc32bf3` (`...32bf3`).
  4. **Duplicate & Mismatch Dynamics:** In `Pgukjp8`, the agent issued 259 total calls including a duplicate typo call (`...32bf6` called twice) plus a duplicate final handle (`ctx_f4e8c2abe047ae311b1b587b`). Three trials deviated from listed order early (call index 2 or 10), whereas three trials preserved exact prefix order until encountering the omitted handle at call index 83.
  5. **Event-Level Success:** 256/257 calls (99.6%) succeeded on the FastMCP server; the single unlisted typo handle returned a 404 error.
- **64k Semantic Distractor (Wave 2, Full GLM-5.3, seed 42):** Scored 0.0 under context pressure.

### 2.3 Recovery Fault Detection & Autonomous Adaptation

- **Transient HTTP 5xx, Persistence 1 (Wave 1, Flash, 3 pairs):** 3/3 passed on clean twin; 2/3 passed on fault arm. In the failing fault trial, the agent retried and wrote the record without executing the mandatory recovery mutation (`refresh_auth`), resulting in `causal_mutation=false` from the verifier.
- **Persistent Signature Error (Wave 2, Flash & Full GLM-5.3):** 4/4 passed on Flash (seeds 42 & 1337); 1/1 passed on Full GLM-5.3.
- **Silent Wrong Payload (Wave 2, Flash):** 4/4 passed on Flash (seeds 42 & 1337).

### 2.4 Sequential Retrieval Scaffold Pacing & Infrastructure Timeout Feasibility

- **Final Timeout×3 Execution (t3 Rerun):** The clean rerun with `agent-timeout-multiplier=3` (`zai-wave2-action64k-s1337-sequential-scaffold-t3`) completed 2 valid scored trials:
  1. `action-64k-neutral_padding-s1337__u4CZxsA` **passed (reward 1.0)**, reading exact 257/257 chunks sequentially and executing the exact final state binding `f3e822e6_v2` for `entity_817.routing_key` (**6,683,558 prompt tokens**, 10,662 completion).
  2. `action-64k-semantic_distractor-s__A67eDZ2` **scored 0.0**, reading 232/257 chunks before early mutation under semantic distractor pressure (**7,454,261 prompt tokens**, 13,526 completion).
- **Prompt Token Expansion:** Total prompt tokens across t3: **14,137,819 prompt tokens** with a two-concurrent wall-clock duration of **23 minutes 50 seconds**. In comparison, the 9 unscaffolded Action 64k trials averaged **412,753 prompt tokens** (range 227,610 – 539,198), representing a **~16–18x prompt expansion** due to accumulating prompt history across 257 single-chunk turns.
- **Policy & Non-Generalization:** While the sequential scaffold resolved the single-character transcription error on neutral padding, it failed on semantic distractors and imposed severe token and latency overhead. **No general effectiveness claim is made.**
- **Default Timeout Execution (First Run):** The initial sequential scaffold run (`zai-wave2-action64k-s1337-sequential-scaffold`) produced 2 `AgentTimeoutError` outcomes when cut off by Harbor's default agent timeout ceiling while issuing chunk reads. These are classified as non-scored harness budget constraints (`reward = None`).

---

## 3. Seed-Blocked Descriptive Contrasts

All comparisons are strictly blocked on matching task, seed, and perturbation parameters. No cross-cell pooling or unweighted averaging is performed.

| Contrast Identifier | Dimension | Arm A (Baseline / Clean) | Arm B (Perturbed / Treatment) | Observed Mean A | Observed Mean B | Delta (B - A) | Notes |
|---|---|---|---|---:|---:|---:|---|
| Action Memory 64k Neutral vs Semantic Distractor | context_dilation_distractor | 64k neutral padding (n=6) | 64k semantic distractor (n=6) | 0.400 | 0.200 | -0.200 | Neutral arm n=6, Semantic arm n=6; Seed-matched pairs for s42 and s1337. |
| Recovery persistent_signature_error: Fault vs Clean Twin | fault_injection_effect | persistent_signature_error clean twin (n=2) | persistent_signature_error fault arm (n=2) | 1.000 | 1.000 | +0.000 | Clean twin n=2, Fault arm n=2; Tests whether unperturbed state passes vs causal recovery under perturbation. |
| Recovery silent_wrong_payload: Fault vs Clean Twin | fault_injection_effect | silent_wrong_payload clean twin (n=2) | silent_wrong_payload fault arm (n=2) | 1.000 | 1.000 | +0.000 | Clean twin n=2, Fault arm n=2; Tests whether unperturbed state passes vs causal recovery under perturbation. |
| Paired Model Contrast: funcdag_depth5_s42 | model_variant_pairing | GLM-5.3-Flash (n=1) | GLM-5.3 Full (n=1) | 1.000 | 1.000 | +0.000 | Flash n=1, Full n=1; Direct paired comparison on identical task configuration. Not a general model ranking. |
| Paired Model Contrast: action_64k_semantic_s42 | model_variant_pairing | GLM-5.3-Flash (n=1) | GLM-5.3 Full (n=1) | 1.000 | 0.000 | -1.000 | Flash n=1, Full n=1; Direct paired comparison on identical task configuration. Not a general model ranking. |
| Paired Model Contrast: recovery_persistent_s42_fault | model_variant_pairing | GLM-5.3-Flash (n=1) | GLM-5.3 Full (n=1) | 1.000 | 1.000 | +0.000 | Flash n=1, Full n=1; Direct paired comparison on identical task configuration. Not a general model ranking. |

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
- **Snapshot Digest:** `sha256:dbaa75f35816e2374ffa1c0f3b9af43ceb0975b07daa59815bdba333d096a478`
- **Report Digest:** `sha256:4e8288bff8bdf0ae703869f3d6e53de520c88c694ff58981700256f3a3414958`

| Feature Name | Lineage / Metric Inputs | Verdict | Epistemic Basis | CI Disposition | Requires Allowlist |
|---|---|---|---|---|---|
| `dag_edge_conformance_rate` | `LINEAGE_VIOLATION` | **LINEAGE_VIOLATION** | `REGISTRY_CONFIRMED` | `BLOCK` | `False` |
| `prompt_tokens` | `CLEAR` | **CLEAR** | `EMPIRICAL_DIAGNOSTIC` | `CLEAR` | `False` |
| `step_count` | `CLEAR` | **CLEAR** | `EMPIRICAL_DIAGNOSTIC` | `CLEAR` | `False` |
| `tool_call_count` | `CLEAR` | **CLEAR** | `EMPIRICAL_DIAGNOSTIC` | `CLEAR` | `False` |
| `value_propagation_accuracy` | `LINEAGE_VIOLATION` | **LINEAGE_VIOLATION** | `REGISTRY_CONFIRMED` | `BLOCK` | `False` |

**Key Invariant Verified:** The two PR #267 known-positive features (`value_propagation_accuracy` and `dag_edge_conformance_rate`) are flagged statically as `LINEAGE_VIOLATION` with `basis = REGISTRY_CONFIRMED` and `ci_disposition = BLOCK` because they read post-verdict fields (`invariants_passed`).

### 5.2 T1.2 Opportunity-Conditioned Recovery
- **Result Digest:** `sha256:58e0f28c0300601dbd5c5ff33165289791519122de0cc785c99b0f8046a2aece`
- **Status:** `REFUSAL` (Refusal: `REPEAT_INELIGIBLE`)
- **Point Estimand:** Fault-weighted recovery rate over eligible fault opportunities = NULL
- **Cluster Bootstrap:** `percentile_cluster_bootstrap` with 0 resamples, clustered by `coalesce(repeat_group_id, trial_id)`.
- **Sample Power:** n_total = 8, n_effective = 5 clusters.

### 5.3 T1.3 Cascade Distance Analysis
- **Report Digest:** `sha256:62ed78463934148ef589427238c15c79ed29c352ce728e8af78b71c2c31c6f98`
- **Evaluated Trajectories (steps $\ge 5$):** 39
- **Observed Lock Events:** 0
- **Right-Censored Trajectories:** 39
- **Conjunctive Refusals:** 0

---

## 6. Prohibited Claims & Methodological Boundaries

In accordance with repository epistemic governance standards, the following claims are explicitly **barred**:

- **No General Model Ranking:** GLM-5.3 Full vs. Flash differences are reported only for the three exact matched tasks (Full 2/3 vs. Flash 3/3), not as general capability or ranking claims.
- **No Parametric Dose-Response Scaling Law:** Action Memory context degradation is non-linear and confounded across wave seeds.
- **No Cost / Throughput Extrapolations:** No per-token billing rates or latency guarantees are claimed.
- **No Unchecked Causal Assertions:** Recovery pass rates are causal only when conditioned on verified verifier mutations (`causal_mutation=true`).

---

## 7. Recommended Next Discriminating Cells

1. **Action Memory 32k Intermediate Dose:** Bridge the 16k $\to$ 64k gap with matched neutral/semantic pairs across seeds 42, 101, 1337.
2. **FuncDAG v2 Discrete MCP Server Decomposition:** Transition from file-based execution to multi-container discrete tool nodes to enable true edge-traversal observability.
3. **Recovery Persistence Scaling:** Evaluate persistence ladders $p \in \{1, 2, 4\}$ on persistent signature errors to measure adaptive backoff.
