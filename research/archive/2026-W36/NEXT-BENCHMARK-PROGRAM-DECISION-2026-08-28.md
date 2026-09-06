---
type: program-decision
topic: focused-benchmark-trajectory-program
date: 2026-08-28
status: distilled
owner: OMP Main
program_status: implementation-authorized-campaign0-only
claim_scope: no capability claims; no cross-benchmark pooling
source_url: https://github.com/PeterMakhnatch/eval-lab
source_type: repo
retrieved: 2026-08-28
license_note: Internal program decision; Eval Lab repository license applies.
feeds:
  - parked
---

# Benchmark Portfolio Decision

## Decision

Build three Harbor-native benchmark families before authorizing sustained billable campaigns:

| Construct | Primary vertical | External anchor | Current reason |
|---|---|---|---|
| Context and actionable memory | `action-memory-v1` | LOCA-Bench / LOCA-Lean | Current LOCA-Lean exposes one hardcoded 8k seed-42 aggregation canary. It cannot supply a matched actionable-memory cohort or state-grounded opportunity denominators. |
| Tool selection and composition | `mcp-funcdag-v1` | FuncBenchGen; existing file-based FuncDAG | Current FuncDAG records one shell/exec surface. It verifies the final dependency trace but cannot observe node-level tool selection, schema conformance, or value propagation from ATIF tool calls. |
| Error detection and recovery | `mcp-recovery-v1` | Recovery-Bench replay arm | No MCP fault interceptor exists. Recovery-Bench is a real MIT runnable Harbor-compatible pipeline, but fresh-container command replay does not certify exact pre-fault state and cannot by itself license causal recovery claims. |

This is an implementation decision for instrumentation and Campaign 0. It is not a finding that these synthetic families are valid capability measurements. Their own controls and calibration pilots must establish that.

## Why Campaign 0 comes first

The existing 107-row trajectory table has almost no opportunity-bearing state, recovery, or long-sequence evidence. Power calculations based on assumed baseline rates or assumed denominator yield are not admissible. Campaign 0 therefore measures:

1. task materialization and verifier control outcomes;
2. opportunity count and non-null feature yield per trial;
3. baseline model success and saturation band;
4. state-journal, ATIF, reasoning-token, error, and reference coverage;
5. latency, token, cache, and dollar ceilings;
6. within-task correlation across repeated seeds;
7. secret-sanitization and deterministic backfill behavior.

Campaign 0 emits descriptive coverage and calibration records only. It cannot emit capability, comparative, or causal conclusions.

## Campaign 0 shape

Each family begins with the smallest deterministic grid that exercises every evidence path. Exact task counts may change after materializer controls; no cell is powered for an effect claim.

### Actionable memory

- clean state inversion;
- neutral padding at one non-zero dose;
- semantic distractor padding at the same byte/token dose;
- stale-value and recall-only negative controls;
- at least three deterministic seeds per cell.

The final action must bind the newest value as a real MCP tool argument. Printing or repeating the value is not success. Retrieval/injection bytes and placement are recorded so harness memory policy is not silently called model memory.

### MCP FuncDAG

- a shallow clean DAG;
- one higher-depth arm at fixed width;
- one higher-width arm at fixed depth;
- one distractor-count arm with names and schema volume held fixed;
- one schema-drift clean twin;
- at least three deterministic seeds per cell.

Every node is a discrete MCP tool. The event log must reconstruct selected nodes, arguments, observations, schema outcomes, and value flow. Oracle truth remains verifier-only. Ceiling and floor saturation are reportable outcomes.

### MCP recovery

- clean twin;
- permission, not-found, timeout, malformed-output, and silent-wrong-result cells;
- persistence one and two where meaningful;
- fixed-policy blind-retry controls at the same persistence;
- permanent-fault and wrong-repair mutants;
- at least three deterministic seeds per cell.

The injection ledger is the opportunity denominator. Silent-wrong and malformed truth is computed verifier-side and absent from the agent image. Recovery credit requires final invariant restoration and task success; auto-clearing after blind retry is reported separately.

## Feature contracts

Every rate has a denominator sibling and is `NULL` at zero opportunity.

### Actionable memory

| Feature | Denominator | Evidence / causal grade | Campaign 0 decision |
|---|---|---|---|
| `binding_survival_rate` | `binding_opportunity_count` | A / C1 | Does the task produce actionable binding opportunities and discriminate controls? |
| `stale_value_override_rate` | `conflicting_value_opportunity_count` | A / C1 | Does state inversion expose stale-value use? |
| `prompt_cache_hit_rate` | `prompt_token_count` | A / C0 | Are matched padding arms operationally balanced? |
| `context_burn_velocity` | `llm_step_count` with minimum sequence length | A / C0 screening | Is sequence coverage adequate for a later slope study? |

Post-compaction re-read is deferred until compaction markers and unchanged-path state evidence are both present.

### MCP FuncDAG

| Feature | Denominator | Evidence / causal grade | Campaign 0 decision |
|---|---|---|---|
| `tool_schema_conformance_rate` | `tool_invocation_count` | A if runtime emits typed rejection; otherwise B / C0 | Does the runtime emit authoritative schema outcomes? |
| `dag_conformance_rate` | `required_edge_count` | A / C1 | Can required edges be reconstructed and verified? |
| `value_propagation_accuracy` | `required_binding_count` | A / C1 | Can wrong selection be separated from wrong wiring? |
| `payload_loop_index_screening` | `tool_call_count` | A / C0 screening | Does the cell expose repeated payloads without naming them failure? |
| `tool_selection_entropy` | `tool_call_count` | A / C0 descriptive | Is the task surface varied enough to make entropy non-constant? |

### MCP recovery

| Feature | Denominator | Evidence / causal grade | Campaign 0 decision |
|---|---|---|---|
| `fault_detection_rate` | `injected_fault_count` | A / C2 | Are faults and subsequent diagnostics joined deterministically? |
| `blind_retry_rate` | `post_fault_retry_opportunity_count` | A / C0 | Does fixed-policy retry remain distinct from adaptation? |
| `strategy_mutation_rate` | `post_fault_retry_opportunity_count` | A / C0 | Are argument/tool mutations observable without prose interpretation? |
| `certified_recovery_rate` | `injected_fault_count` | A / C3 | Do state certificate and task verdict agree? |
| `recovery_latency_steps` | `recovered_fault_count` | A / C0 | Is event ordering dense and stable enough for sequence analysis? |

Fault class is categorical, not a scalar dose. Persistence is ratio-scale and may become a curve after calibration.

## Shared data and analysis capability

The common pipeline must:

1. ingest versioned `benchmark-events.jsonl`, `final-state.json`, benchmark contract, ATIF, state journal, result, and loss manifest into immutable trial bundles;
2. join tool calls to observations by call id and benchmark events by monotone index/call id;
3. retain benchmark/task/cell/seed/attempt and opportunity denominators as first-class keys;
4. compute benchmark-specific features in isolated producer modules and register them centrally without embedding benchmark logic in `traj.py`;
5. emit coverage diagnostics before statistics;
6. support matched-arm analyses, task-cluster intervals, order-restricted monotonicity for real dose axes, saturation bounds, and explicit refusal states;
7. preserve separate per-benchmark reports and prohibit universal aggregates.

## Campaign runner capability

Long-running execution must use a durable campaign manifest and queue/PolicyGate rather than billable `ExperimentMatrix` direct execution. Required behavior:

- immutable cell/task/attempt spec digests;
- deterministic job identity;
- explicit billable approval and per-trial/campaign ceilings;
- fail-closed credential preflight and file-backed secret transport;
- bounded concurrency and provider circuit breaker;
- leases and crash-safe invocation journal;
- exact-digest resume that skips completed attempts and refuses drift;
- sanitized evidence archive plus deterministic targeted backfill;
- plan/status/run/resume commands;
- no automatic registration or publication.

## Workstream ownership

- Architect: integrated topology, schemas, boundaries, and PR dependency DAG.
- Eval Platform: campaign manifest, queue execution, resume, budget, and operator CLI.
- Agent Data: benchmark-event ingestion, isolated feature producers, coverage mart, analysis surfaces.
- Gemini builder: actionable-memory vertical.
- Gemini builder: MCP FuncDAG vertical.
- Grok builder: MCP recovery vertical and adversarial negative controls.
- Eval Runner: controls and Campaign 0 only after merged code, focused validation, credential readiness, and explicit campaign ceiling.

## Gates before sustained campaigns

1. All family controls pass on Linux and produce certification packets.
2. Oracle truth and credentials are absent from agent-visible bytes.
3. Campaign 0 event/feature coverage is non-zero and deterministic on repeat backfill.
4. The model-success band is not uniformly saturated; otherwise adjust the ladder and report the bound.
5. Opportunity yield and baseline success replace assumed power inputs.
6. Cost ceiling is computed from configured caps and current provider pricing, not an optimistic point estimate.
7. Tutor accepts construct validity and falsification controls.
8. Grok and Gemini exact-head reviews have no unresolved blocking finding.
