# Trajectory Interpretation Card: har81-l-d-a2-arvo-18737__2kbVhKB

## 1. Identity & Final Outcome
- **Trial Name:** `har81-l-d-a2-arvo-18737__2kbVhKB`
- **Task Name:** `mimo-v2.6-rl/arvo_18737`
- **Job ID / Name:** `85600ea9-c7b8-4a00-8961-a95c6040ed73` (har81-l-d-a2-arvo-18737)
- **Agent / Model:** `terminus-2` (v: 2.0.0) | `selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B`
- **Final Verdict:** **EXCEPTION (TrialBudgetExhaustedError)** (Primary Reward: `0.00` | Trajectory Status: `featured`)
- **Evidence Limitation:** *Harness or runtime exception occurred (TrialBudgetExhaustedError).*
- **Exception Details:** `TrialBudgetExhaustedError` — the trial proxy refused a model call: trial budget exhausted
- **Quality Status:** `unknown` — *Quality Ledger uncomputed for this trial*
- **Execution Telemetry:** Duration: `525.9s` | Cost: `$0.0000` | Tokens: `2,420,779`

## 2. Execution Phases
| Phase | Steps (Span) | Tool Calls | Errors | Summary |
|---|---|---|---|---|
| `prompt` | 1 (1) | 0 | 0 | 1 step(s), 0 tool(s), 0 error(s) |
| `work` | 2..118 (117) | 0 | 0 | 117 step(s), 0 tool(s), 0 error(s) |

## 3. Mechanical Baseline Metrics
| Metric | Value | Provenance Category / Screening Semantics |
|---|---|---|
| **Steps (total / agent / sys / user)** | 118 (117 / 0 / 1) | `mechanical_fact` (exact step counts) |
| **Tool Calls (total / unique)** | 0 (0) | `mechanical_fact` (exact tool call counts) |
| **Errors / Recoveries** | 0 / 0 | `mechanical_fact` (exact execution error counts) |
| **Expected Negative / Probes** | NO (0 probes) | `mechanical_fact` (intentional negative control screening) |
| **Step / Time to First Error** | none (none) | `mechanical_fact` (first error latency) |
| **Recovery Latency** | none (none) | `mechanical_fact` (step/time delay to recovery) |
| **Terminal Error State** | Recovered / Clean | `mechanical_fact` (active error at final step) |
| **Linearity Index (`LI_screening`)** | NULL (0 tool calls) | `screening_heuristic` (unique_tools / tool_calls) |
| **Tool Error Rate (`TER_screening`)** | NULL (0 tool calls) | `screening_heuristic` (errors / tool_calls) |
| **Recovery Rate (`recovery_rate_screening`)** | NULL (0 errors) | `screening_heuristic` (recoveries / errors) |
| **Context Burn Velocity (`CBV_screening`)** | +174.81 tok/step | `screening_heuristic` (regr_slope prompt_tokens over steps) |
| **Cache Hit Rate (`cache_hit_rate_screening`)** | 0.0% | `screening_heuristic` (cached / prompt) |
| **Subagent Overhead (`subagent_overhead_screening`)** | 0.0% | `screening_heuristic` (subagent_steps / total_steps) |
| **Autonomous Ratio (`autonomous_step_ratio_screening`)** | 99.2% | `screening_heuristic` (autonomous / total) |
| **Assisted Ratio (`assisted_step_ratio_screening`)** | 0.9% | `screening_heuristic` (assisted / total) |
| **Edit Efficiency (`edit_efficiency_screening`)** | NULL (0 edits) | `screening_heuristic` (mutations / edit_calls) |
| **Path Reference Validity (`path_reference_validity_screening`)** | NULL (0 path refs) | `screening_heuristic` (valid_paths / total_paths) |
| **Citation Validity (`citation_validity_screening`)** | 100.0% (2/2) | `screening_heuristic` (valid_cites / total_cites) |
| **State Journal Status** | `not_observed` (0 events) | `mechanical_fact` (canonical state journal observation) |
| **State Diff Mutations** | NO (0 mutations: +0/~0/-0, +0B) | `mechanical_fact` (observed state diff mutations) |
| **Unobserved State Mutations** | 0 unobserved | `mechanical_fact` (filesystem mutations without tool reference) |

## 4. Cited Error Observations & Stderr
No tool or command execution errors recorded in trajectory.

## 5. Loop & Cascade Reason Codes
- **Loop Suspicion:** **not detected** (Score: `0.00`)
- **Triggered Reason Codes:** none
- **Max Exit-Code Cascade:** `0` consecutive error steps

## 6. Intervention Provenance
- **Intervention Category:** `autonomous`
- **Summary:** Autonomous execution (initial task instruction only; no intermediate user steering)
- **Turn Breakdown:** Autonomous agent steps: `117` | Assisted steps: `1` | Post-error interventions: `1`
## 7. Semantic Coverage
- **Coverage Status:** `unprojected`
- **Details:** No semantic fact projections found; relying on mechanical screening

## 8. Source Citations & Exact Provenance
- **Trial Directory:** `/Users/petermakhnatch/Developer/eval-lab/derived/trace-lab/viewers/harbor-raw-jobs/har81-l-d-a2-arvo-18737/har81-l-d-a2-arvo-18737__2kbVhKB`
- **Trajectory File:** `/Users/petermakhnatch/Developer/eval-lab/derived/trace-lab/viewers/harbor-raw-jobs/har81-l-d-a2-arvo-18737/har81-l-d-a2-arvo-18737__2kbVhKB/agent/trajectory.json` (SHA-256: `ca7e741a49f6bfd2eba5d4ded44f5278acbbd5aec3269b223d9f05deec7bd56f`)
- **Result File:** `result.json` (SHA-256: `7d0eb4f4a5e8e10f3558d079908ac1c1d8cde1eae711391f54b5bd3de4980f49`)

## 9. C0 Mechanical Screening
- **Projection Status:** `SCREENING_ONLY`
- **Mechanical Source:** `atif_trajectory`
- **Causal Grade:** `C0`
- **Claim Scope:** mechanical facts and screening only
- **Causal Claims:** **PROHIBITED**
- **Synthetic Recipe Eligibility:** **false**
- **Refusals:** `HARNESS_EXCEPTION, MISSING_BENCHMARK_CONTRACT, MISSING_BENCHMARK_EVENTS, MISSING_FINAL_STATE`
- **Quality Disposition:** `HARNESS_EXCEPTION`

### C0 Facts and Explicit Denominators
| Metric | Value | Role |
|---|---|---|
| `event_count` | `None` | mechanical fact |
| `tool_call_count` | `0` | denominator |
| `opportunity_denominator` | `tool_call_count` | explicit |
| `opportunity_count` | `0` | denominator value |
| `tool_error_count` | `0` | mechanical fact |
| `tool_error_rate_screening` | `None` | C0 screening ratio |
| `source_sha256` | `7d0eb4f4a5e8e10f3558d079908ac1c1d8cde1eae711391f54b5bd3de4980f49` | digest |
| `trajectory_sha256` | `ca7e741a49f6bfd2eba5d4ded44f5278acbbd5aec3269b223d9f05deec7bd56f` | digest |
| `verifier_sha256` | `fdfc34bc52e1a09f27c29f0338e31e46504786ec18afbfcbfa3e17dd985b777e` | digest |
