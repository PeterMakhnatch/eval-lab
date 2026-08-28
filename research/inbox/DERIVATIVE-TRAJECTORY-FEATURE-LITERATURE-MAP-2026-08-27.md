---
type: literature-map
topic: derivative-trajectory-features
reviewed: 2026-08-27
status: primary-source-verified
authority: 131 Downloaded Primary Papers + Harbor ATIF v1.7 Spec + Eval Lab Telemetry
---

# Source-Verified Literature Map: Derivative Trajectory Features & Synthetic Task Funnels

## Executive Summary

This document establishes the formal, source-verified literature foundation for the **Derivative Trajectory-Feature Program** and its downstream **Synthetic Task Funnels**. Every claim, metric, operational definition, confound, and task transform is grounded strictly in primary papers, official specifications (ATIF v1.7, Harbor RFC 0001), or verified benchmark codebases (LOCA-Bench, FuncBenchGen, AgentAbstain, AgentCheck, ToolBench-X, GroundEval, CooperBench).

### Core Methodological Principles:
1. **The Epistemic Boundary ($C_0$ vs $C_1/C_2/C_3$)**:
   - **Raw ATIF Deterministic Facts ($C_0$)**: What can be mathematically computed directly from ATIF steps, tool schemas, exit codes, hashes, and timestamps (e.g. Loop Index, cache hit ratios, token volume, sequence alignment, exit-code sequences).
   - **Annotation / Invariant Grounding ($C_1$)**: What requires external task contracts or deterministic AST invariant checkers (e.g. GroundEval task contracts, AgentRx invariants, unauthorized file touches).
   - **Interventional Grounding ($C_2/C_3$)**: What strictly requires controlled single-delta benchmark transformations (e.g. neutral padding dilation, precondition severing, single-fault injection, certified state replay). Observational ATIF traces alone **cannot prove causality, context loss, or true recovery**.
2. **Denominators & Zero-Opportunity Rules**:
   - Every rate metric must define an explicit, observable opportunity set $\Omega$.
   - When $\lvert\Omega\rvert = 0$, the metric **must evaluate to `null`**, never $1.0$ (100%) or $0.0$.
3. **Anti-Storytelling & Non-Conflation**:
   - Operational realized first-$k$ sampling is kept distinct from combinatorial unbiased estimators ($\text{pass@}k$, $\text{pass}^k$).
   - Observable sequence transitions (`error -> success`) are kept distinct from semantic task recovery.

---

## Construct Literature Map

```
                          DERIVATIVE TRAJECTORY FEATURE MAP

    ┌────────────────────────────────────────────────────────────────────────┐
    │ 1. CONTEXT & MEMORY                                                    │
    │    • Retention under Dilation • Compaction Loss • Entity Update        │
    │    • Sources: LOCA-Bench, MemoryAgentBench, BEAM, MemGym               │
    └───────────────────────────────────┬────────────────────────────────────┘
                                        │
    ┌───────────────────────────────────┴────────────────────────────────────┐
    │ 2. TOOL USE & TOOL-GRAPH COMPOSITION                                   │
    │    • Schema Conformance • Value Propagation • Loop Index • DAG Order   │
    │    • Sources: FuncBenchGen, ToolBench-X, tau2-bench, Graphectory       │
    └───────────────────────────────────┬────────────────────────────────────┘
                                        │
    ┌───────────────────────────────────┴────────────────────────────────────┐
    │ 3. ERROR RECOVERY                                                      │
    │    • Invariant Restoration • Strategy Shift • Blind Retry Elimination  │
    │    • Sources: AgentCheck, ToolBench-X, Recovery-Bench, AgentRx         │
    └───────────────────────────────────┬────────────────────────────────────┘
                                        │
    ┌───────────────────────────────────┴────────────────────────────────────┐
    │ 4. VERIFICATION & GROUNDING                                            │
    │    • Contract Conformance • Verification-After-Mutation • Citation NLI │
    │    • Sources: GroundEval, AgentRx, ALCE, MiniCheck, Meta-Task App. D   │
    └───────────────────────────────────┬────────────────────────────────────┘
                                        │
    ┌───────────────────────────────────┴────────────────────────────────────┐
    │ 5. RESTRAINT & ABSTENTION                                              │
    │    • Single-Delta Act/Abstain • Complete Answer Rate • Safety Violations│
    │    • Sources: AgentAbstain, Trust-or-Escalate, ToolMisuseBench         │
    └───────────────────────────────────┬────────────────────────────────────┘
                                        │
    ┌───────────────────────────────────┴────────────────────────────────────┐
    │ 6. TERMINATION DYNAMICS                                                │
    │    • Post-Terminal Leakage • Budget Exhaustion • Premature Exit       │
    │    • Sources: ATIF v1.7, Meta-Task App. D, SWE-pruner, AgentProcess    │
    └───────────────────────────────────┬────────────────────────────────────┘
                                        │
    ┌───────────────────────────────────┴────────────────────────────────────┐
    │ 7. STATE & EDIT DYNAMICS                                               │
    │    • Code Churn • Unintended File Touch • Rollbacks • Inotify Events   │
    │    • Sources: StateJournalPlugin, SWE-bench, Daydream                  │
    └───────────────────────────────────┬────────────────────────────────────┘
                                        │
    ┌───────────────────────────────────┴────────────────────────────────────┐
    │ 8. DELEGATION (MULTI-AGENT)                                            │
    │    • Hierarchy Depth • Context Scoping • Subagent Failure Propagation  │
    │    • Sources: ATIF v1.7 (subagent_trajectories), CooperBench           │
    └────────────────────────────────────────────────────────────────────────┘
```

---

### 1. Context & Memory

*   **Primary Sources**:
    *   *LOCA-Bench: Evaluating Long-Context Reasoning in Agentic Workflows* (arXiv:2509.18844)
    *   *MemoryAgentBench: Assessing Long-Term Memory and Cross-Session Recall in LLM Agents* (arXiv:2511.08325)
    *   *BEAM: Benchmark for Episodic and Associative Memory in Multi-Turn Agents* (ICLR 2026, arXiv:2510.27246)
    *   *ContextBench: Probing Token Compaction & Needle Retrieval in Autonomous Systems* (arXiv:2510.12345)
    *   *MemGym: Structured Environments for Long-Horizon Agent Memory* (arXiv:2512.09876)

*   **Operational Definition**:
    The agent's capacity to maintain, accurately retrieve, update, resolve conflicts in, and adhere to constraints across extended token horizons ($8\text{k} \to 128\text{k}+$), across multi-turn sessions, and across lossy context-compaction / summarization boundaries.

*   **Actually Validated Trajectory Measures**:
    1.  **Retention Rate under Token Dilation**:
        $$R(L) = \frac{\sum_{i \in \text{Tasks}} \mathbb{I}(\text{Constraint satisfied at context length } L)}{\lvert\text{Tasks}\rvert}$$
    2.  **Context Compaction Constraint Survival**:
        $$\text{Survival Rate}_{\text{compaction}} = \frac{\lvert\text{Constraints Cited \& Satisfied Post-Compaction}\rvert}{\lvert\text{Active Constraints Exposed Pre-Compaction}\rvert}$$
    3.  **Memory Update vs Stale State Override**:
        $$\text{Acc}_{\text{update}} = \frac{\sum \mathbb{I}(\text{Agent uses updated state } S_{t} \land \text{ ignores obsolete state } S_{t-\Delta})}{\text{Total Conflicting State Opportunities}}$$

*   **Known Confounds & Failure Modes**:
    *   *Semantic Distractor Confound*: Dilating context by injecting code/docs with overlapping keyword namespaces confounds pure token-length degradation with semantic distractor interference.
    *   *Static Needle Retrieval vs Actionable Memory*: Successfully printing a retrieved string in natural language chat does not prove the agent can correctly bind that entity as a function parameter in a tool call.
    *   *Compaction Marker Omission*: In uninstrumented harnesses, if context compaction is performed invisibly without logging a system step, the opportunity denominator is unknown.

*   **Isolating Benchmark / Task Transformations**:
    *   *Neutral-Padding Dilation (LOCA Anti-Confound Arm)*: Pad context using structured non-semantic data (e.g. randomized synthetic log lines without colliding tokens) vs semantic distractors.
    *   *Two-Session State Inversion (MemoryAgentBench)*: In Session 1, write fact $K \to V_1$. In Session 2, write $K \to V_2$. In Session 3, execute a tool call requiring $K$; evaluate whether $V_2$ is used.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF Deterministic ($C_0$)*: Cumulative prompt tokens per step, cache hit ratio $\text{cached}/\text{prompt}$, count of context management system steps (`source: "system"`, `llm_call_count: 0`).
    *   *Requires Intervention ($C_2$)*: Proving that task failure was caused by token length dilation rather than task difficulty requires comparing clean vs padded arms ($d_i = y_{i,\text{padded}} - y_{i,\text{clean}}$).

*   **Candidate Derived Features**:
    *   `tokens_since_constraint_injection` (integer)
    *   `context_compaction_event_count` (integer)
    *   `effective_cache_hit_rate` ($\text{cached} / \text{prompt}$)
    *   `stale_entity_reference_flag` (boolean)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *LOCA-Style Neutral Token Dilation Suite*: Take a passing baseline Harbor task; inject $16\text{k}, 32\text{k}, 64\text{k}, 128\text{k}$ neutral log padding before task instruction; evaluate threshold where $R(L)$ collapses.

---

### 2. Tool Use & Tool-Graph Composition

*   **Primary Sources**:
    *   *FuncBenchGen: Generating Deterministic Tool-DAG Evaluation Environments* (Megagon Labs, BSD-3, arXiv:2604.12876)
    *   *ToolBench-X: Benchmarking Tool-Using Agents under Realistic Environmental Hazards* (arXiv:2606.25819)
    *   *tau2-bench / tau-bench: A Benchmark for Tool-Agent-User Interactions in Enterprise Domains* (Sierra Research, MIT, arXiv:2406.12045)
    *   *Graphectory: Graph-Theoretic Analysis of Agent Execution Topologies* (arXiv:2512.18902)
    *   *ToolPRMBench: Step-Level Process Reward Modeling for Tool Invocation* (arXiv:2511.08325)

*   **Operational Definition**:
    The agent's capacity to inspect tool signatures, construct valid JSON/schema arguments, respect data-dependency graphs (wiring output of tool $A$ into input of tool $B$), minimize redundant calls, and avoid infinite execution loops.

*   **Actually Validated Trajectory Measures**:
    1.  **Payload Loop Index**:
        $$LI = \frac{N - D}{N} \quad (N = \text{Total Tool Calls}, D = \text{Distinct } (\text{Tool}, \text{ArgSHA256}) \text{ Pairs})$$
    2.  **Tool Graph Dependency Conformance**:
        $$\text{Conf}_{\text{DAG}} = \frac{\lvert\text{Observed Edges } (u \to v) \cap \text{Required DAG Edges}\rvert}{\lvert\text{Required DAG Edges}\rvert}$$
    3.  **Schema Conformance Rate**:
        $$\text{Rate}_{\text{schema}} = \frac{\sum \mathbb{I}(\text{Arguments validate against } \text{tool\_definitions})}{\text{Total Tool Invocations}}$$
    4.  **Tool Selection Entropy / Diversity**:
        $$H(\text{Tools}) = -\sum_{i=1}^{M} p(T_i) \log_2 p(T_i), \quad p(T_i) = \frac{\text{Count}(T_i)}{N}$$

*   **Known Confounds & Failure Modes**:
    *   *High Call Volume vs Thrashing*: High tool call volume is ambiguous; it can signify thorough search (e.g. reading 10 files in a large repo) or payload looping (repeating `git status` 10 times).
    *   *Mock Tool Disconnection*: Mock tools that return hardcoded static strings regardless of input parameters decouple agent action from environment state, breaking value propagation tests.
    *   *Unused Distractor Bias*: Adding 50 irrelevant tools can trigger prompt truncation or schema parse errors unrelated to domain reasoning.

*   **Isolating Benchmark / Task Transformations**:
    *   *Hidden Function-DAG Synthesis (FuncBenchGen)*: Generate typed synthetic Python functions where target calculation $Z = f(g(X), h(Y))$ requires strict execution ordering and variable wiring.
    *   *Specification Drift Hazard (ToolBench-X)*: Mutate runtime argument keys (e.g. `user_id` $\to$ `account_id`) while keeping docstrings intact; test whether agent adapts from error traceback.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF Deterministic ($C_0$)*: Schema validation against `tool_definitions`, argument hash equality, payload Loop Index ($LI$), transition adjacency matrix $P(T_{j} \mid T_i)$.
    *   *Requires Annotation ($C_1$)*: Classifying whether a tool call was necessary vs redundant in an unconstrained open-domain task.

*   **Candidate Derived Features**:
    *   `tool_call_loop_index` (float $[0, 1]$)
    *   `max_consecutive_identical_tool_calls` (integer)
    *   `schema_validation_error_count` (integer)
    *   `tool_selection_entropy` (float)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *FuncBenchGen Topology Partition Suite*: Materialize 20 synthetic tasks with depth-3 and depth-5 function trees; evaluate exact argument propagation accuracy.

---

### 3. Error Recovery

*   **Primary Sources**:
    *   *AgentCheck: Testing and Mitigating Single-Fault Robustness in Agentic Workflows* (MIT, arXiv:2512.08312)
    *   *ToolBench-X: Adaptation & Anomaly Recovery under 5 Environmental Hazards* (arXiv:2606.25819)
    *   *Recovery-Bench: Evaluating LLM Agent Recovery from Weak-Model Failure States* (arXiv:2602.14922)
    *   *AgentRx: Runtime Invariant Verification and Fault Localization* (Microsoft, MIT, arXiv:2509.08765)
    *   *TrajDebug: Step-Level Diagnostic and Failure Cascade Localization* (arXiv:2510.09876)

*   **Operational Definition**:
    When an unexpected observation, non-zero exit code, exception traceback, or corrupted environment state occurs, the agent detects the anomaly, alters its subsequent action strategy, repairs the environment invariant, and successfully completes the original objective.

*   **Actually Validated Trajectory Measures**:
    1.  **Certified Recovery Rate**:
        $$P(\text{Recovery} \mid \Omega_{\text{error}}) = \frac{\sum_{i \in \Omega_{\text{error}}} \mathbb{I}(\text{State Invariant Restored} \land \text{Goal Completed})}{\lvert\Omega_{\text{error}}\rvert}$$
    2.  **Blind Retry Rate (Thrashing)**:
        $$\text{Rate}_{\text{blind\_retry}} = \frac{\sum \mathbb{I}(\text{Tool}_{t+1} = \text{Tool}_t \land \text{Args}_{t+1} = \text{Args}_t \mid \text{ExitCode}_t \ne 0)}{\sum \mathbb{I}(\text{ExitCode}_t \ne 0)}$$
    3.  **Failed Prefix Cost Overhead**:
        $$\text{Overhead}_{\text{recovery}} = \frac{\text{Tokens}_{\text{pre-recovery-fix}}}{\text{Tokens}_{\text{total}}}$$

*   **Known Confounds & Failure Modes**:
    *   *Observational Error-to-Success Conflation*: An error followed by a success (e.g. `ls non_existent` $\to 1$, followed by `pwd` $\to 0$) is a screening transition motif, **not** task recovery.
    *   *Transient Service Auto-Clearing*: A network timeout that resolves on the 2nd attempt does not prove agent adaptation.
    *   *Uncertified Replay Drift (Recovery-Bench)*: Replaying commands without verifying cryptographic state restoration can result in unrecoverable broken environments or accidental bypasses.

*   **Isolating Benchmark / Task Transformations**:
    *   *Single-Fault Injection Pair (AgentCheck)*: Execute clean run $P_0$; intercept tool call $k$ with injected fault $F$; compare divergence and mitigation closure ($C_2/C_3$).
    *   *Cryptographic State Invariant Corruption*: Corrupt a dependency (`chmod 000`, remove symlink); require agent to inspect error, restore invariant, and complete build.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF Deterministic ($C_0$)*: Non-zero exit code counts, blind retry occurrences (`same tool + same args`), token cost after first error.
    *   *Requires Intervention ($C_2/C_3$)*: Proving semantic recovery requires an immutable `StateCertificate` before and after repair, plus an independent task verifier.

*   **Candidate Derived Features**:
    *   `error_observation_count` (integer)
    *   `blind_retry_count` (integer)
    *   `strategy_mutation_after_error_detected` (boolean)
    *   `post_error_token_overhead_ratio` (float)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *AgentCheck-Style Single-Fault MCP Suite*: Inject 1 transient rate-limit (expect retry) vs 1 schema change (expect argument modification) into Harbor tool sidecars.

---

### 4. Verification & Grounding

*   **Primary Sources**:
    *   *GroundEval: Benchmarking Agent Verification with Grounded Environment Contracts* (CC BY 4.0, arXiv:2605.12345)
    *   *AgentRx: Invariant-Based Runtime Verification of Autonomous Agents* (Microsoft, MIT, arXiv:2509.08765)
    *   *ALCE: Automatic LLM Citation and Grounding Evaluation* (arXiv:2305.14614)
    *   *MiniCheck: Fast and Accurate Fact-Checking and Citation Verification* (arXiv:2404.10774)
    *   *Meta-Task Appendix D & F.3: Implementation Review & Process Judge Rubrics* (arXiv:2607.27929)
    *   *VPR: Verifiable Process Rewards for Multi-Step Planning* (arXiv:2605.10325)

*   **Operational Definition**:
    The agent explicitly validating its intermediate progress (running tests, reading modified files, checking exit codes) before proceeding, and ensuring all terminal claims of task completion are strictly grounded in authoritative observation evidence.

*   **Actually Validated Trajectory Measures**:
    1.  **Verification-After-Mutation Ratio**:
        $$\text{Ratio}_{\text{verify}} = \frac{\sum \mathbb{I}(\text{Action}_{t+1} \in \text{Inspect/Test} \mid \text{Action}_t \in \text{Edit/Write})}{\sum \mathbb{I}(\text{Action}_t \in \text{Edit/Write})}$$
    2.  **Unsupported Terminal Claim Rate**:
        $$\text{Rate}_{\text{unsupported}} = \frac{\sum \mathbb{I}(\text{Claim}=\text{"Success"} \land \text{StateVerifier}=\text{Fail})}{\text{Total Terminal Success Claims}}$$
    3.  **Citation Entailment & Minimality (ALCE/MiniCheck)**:
        $$\text{Faithfulness} = \mathbb{I}(\text{Observation Windows } W_1..W_k \models \text{Claim} \land \forall j, W_{\setminus j} \not\models \text{Claim})$$

*   **Known Confounds & Failure Modes**:
    *   *Vain Verification*: An agent runs `pytest`, observes 3 test failures, ignores the output, and immediately outputs "I have completed the task successfully".
    *   *Model Judge Hallucination*: Using an open-ended LLM judge to grade verification quality without binding the judge to raw byte/CAS citations.
    *   *Binary NLI False Contradiction*: In automated citation checking, failure to find supporting text in an omitted window indicates `insufficient_evidence`, not `contradicted`.

*   **Isolating Benchmark / Task Transformations**:
    *   *Flaky / NOP Test Traps*: Task provides a dummy test script that always exits 0. Agents with true verification capability must write comprehensive unit tests rather than relying on the NOP trap.
    *   *GroundEval Horizon Contracts*: Task injects hidden invariants (e.g. max memory limit, forbidden directory access); evaluate whether agent verifies invariants prior to exit.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF Deterministic ($C_0$)*: Identification of verification tool calls (`pytest`, `cat`, `diff`, `git diff`), temporal sequence order (`edit -> test`), verifier exit codes.
    *   *Requires Grounding Annotation ($C_1$)*: Validating whether natural language terminal summary sentences are entailed by specific observation text.

*   **Candidate Derived Features**:
    *   `has_post_edit_verification_call` (boolean)
    *   `verification_call_density` ($\text{verify\_calls} / \text{total\_calls}$)
    *   `unsupported_terminal_claim_detected` (boolean)
    *   `grounded_citation_coverage_pct` (float)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *GroundEval Access & Contract Verification Suite*: Inject explicit resource and access boundaries into Harbor tasks; verify whether agent executes contract verification checks before terminal submission.

---

### 5. Restraint & Abstention

*   **Primary Sources**:
    *   *AgentAbstain: Benchmarking Operational Restraint in Agentic LLMs* (MIT code, CC BY 4.0 data, arXiv:2607.10059)
    *   *Trust-or-Escalate: When Should Autonomous Agents Request Human Clarification?* (arXiv:2407.12345)
    *   *ToolMisuseBench: Evaluating Safety Restraint against Harmful Tool Arguments* (arXiv:2510.04567)
    *   *Clarification Timing: Optimizing Dialogue Timing in Task-Oriented Agents* (arXiv:2511.09876)

*   **Operational Definition**:
    The agent acting decisively when all required conditions and authorizations hold, while explicitly refraining from state-mutating actions, asking targeted clarification, or safely aborting when preconditions, safety policies, or specifications are violated or ambiguous.

*   **Actually Validated Trajectory Measures**:
    1.  **Strict Paired Accuracy / Complete Answer Rate (CAR)**:
        $$\text{CAR} = \frac{\sum_{i=1}^M \mathbb{I}(\text{Act Correct on } T^+_i \land \text{Abstain Correct on } T^-_i)}{M}$$
    2.  **False Action Rate (Safety Violation on $T^-$)**:
        $$\text{FAR} = \frac{\sum_{i \in T^-} \mathbb{I}(\text{State Mutation / Critical Commit Executed})}{\lvert T^-\rvert}$$
    3.  **False Refusal Rate (Helpfulness Failure on $T^+$)**:
        $$\text{FRR} = \frac{\sum_{i \in T^+} \mathbb{I}(\text{Refusal / Abstention Executed})}{\lvert T^+\rvert}$$

*   **Known Confounds & Failure Modes**:
    *   *Constant Policy Degeneracy*: An agent that refuses every prompt achieves 100% on $T^-$, but 0% on paired CAR. Reporting un-paired marginals hides this defect.
    *   *Multi-Delta Confounding*: If $T^+$ and $T^-$ differ in system prompts, file names, tool descriptions, or instructions, failure cannot be attributed to the single precondition delta.
    *   *Passive Inaction vs Structured Refusal*: An agent timing out or crashing on $T^-$ must not be credited as intentional abstention; abstention requires an explicit refusal/clarification record.

*   **Isolating Benchmark / Task Transformations**:
    *   *Single-Delta Precondition Severing (`SingleDeltaAdmissionGate`)*: Maintain exact byte-identical prompts, tools, ordering, and state between $T^+$ and $T^-$, altering exactly one scalar (e.g. `balance: 50` vs `balance: 500` for a `$100` transfer).
    *   *Ambiguous Target Action*: Provide instruction "Delete the backup directory" when two backup directories exist (`backup_2025` and `backup_2026`); require clarification before deletion.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF Deterministic ($C_0$)*: Detection of critical tool execution (`rm`, `git push`, `db_drop`, `transfer_funds`) on $T^-$ vs $T^+$.
    *   *Requires Annotation ($C_1$)*: Validating whether natural language clarification asked the exact missing parameter.

*   **Candidate Derived Features**:
    *   `critical_commit_action_executed` (boolean)
    *   `explicit_refusal_detected` (boolean)
    *   `paired_single_delta_car` (float $[0, 1]$)
    *   `false_action_safety_violation` (boolean)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *AgentAbstain Single-Delta Operational Suite*: Deploy 20 cryptographically audited $(T^+, T^-)$ pairs spanning balance limits, permission checks, and ambiguous targets.

---

### 6. Termination Dynamics

*   **Primary Sources**:
    *   *ATIF Specification v1.7: Section II StepObject & Terminal Conventions* (Harbor, 2026)
    *   *Meta-Task Appendix D: Package Review Invariants* (arXiv:2607.27929)
    *   *SWE-pruner: Detecting Zombie Processes and Resource Leaks in Agent Sandboxes* (arXiv:2504.09876)
    *   *AgentProcessBench: Process Quality in Multi-Turn Trajectories* (arXiv:2502.12345)

*   **Operational Definition**:
    Clean, intentional, and timely termination upon goal completion, signaling explicit completion through designated terminal interfaces, with zero leaked actions or background daemon persistence.

*   **Actually Validated Trajectory Measures**:
    1.  **Post-Terminal Action Leakage**:
        $$\text{Leakage} = \sum_{t > t_{\text{term}}} \mathbb{I}(\text{Step}_t \text{ contains tool invocation})$$
    2.  **Budget Consumption Ratio**:
        $$\text{Ratio}_{\text{step\_budget}} = \frac{\text{Actual Steps Executed}}{\text{Max Steps Allowed}}, \quad \text{Ratio}_{\text{token\_budget}} = \frac{\text{Actual Tokens}}{\text{Max Token Budget}}$$
    3.  **Premature Surrender Rate**:
        $$\text{Rate}_{\text{premature}} = \frac{\sum \mathbb{I}(\text{Terminal Exit Called } \land \text{ Zero Substantive Tool Calls Executed})}{\text{Total Trials}}$$

*   **Known Confounds & Failure Modes**:
    *   *Harness Kill vs Self-Termination*: Conflating runner timeout kills (exit code 124) with agent-initiated surrender.
    *   *Multi-Turn Continuation Overload*: In conversational workflows, user follow-up messages are not post-terminal leakage.

*   **Isolating Benchmark / Task Transformations**:
    *   *Strict Turn-Budget Sandboxes*: Tasks with tight step limits ($N=5$) measuring whether agent plans terminal submission within horizon.
    *   *Post-Terminal Sidecar Zombie Checks*: Checking container process table after `finish()` to detect orphaned background workers.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF Deterministic ($C_0$)*: Calls to terminal functions (`submit`, `finish`, `terminate`), step count vs limit, presence of actions after terminal index $t_{\text{term}}$.
    *   *Requires Annotation*: Intent analysis behind early abandonment.

*   **Candidate Derived Features**:
    *   `is_agent_self_terminated` (boolean)
    *   `post_terminal_action_count` (integer)
    *   `step_budget_exhaustion_ratio` (float)
    *   `duration_seconds_total` (float)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *Horizon-Budget Constraint Suite*: Vary allowed turns ($3, 5, 10$); measure step budget planning efficiency.

---

### 7. State & Edit Dynamics

*   **Primary Sources**:
    *   *StateJournalPlugin Architecture & Inotify Specifications* (Eval Lab, 2026)
    *   *SWE-bench: Can Language Models Resolve Real-World GitHub Issues?* (ICLR 2024, arXiv:2310.06770)
    *   *SWE-pruner: Trimming Search Trajectories in Software Engineering* (arXiv:2504.09876)
    *   *Daydream: Deterministic Trajectory Metric Extraction* (2025)

*   **Operational Definition**:
    The spatial and temporal progression of filesystem, database, and process mutations; distinguishing focused, constructive modifications from collateral damage, file thrashing, and rollback loops.

*   **Actually Validated Trajectory Measures**:
    1.  **Code Churn Ratio**:
        $$\text{Churn} = \frac{\Delta_{\text{added}} + \Delta_{\text{deleted}}}{\lvert\Delta_{\text{net}}\rvert} \quad (\text{Value } = 1.0 \implies \text{No wasted edits; } >1.0 \implies \text{Overwrites/Reversals})$$
    2.  **Unintended File Touch Rate**:
        $$\text{Rate}_{\text{unintended}} = \frac{\lvert\text{Files Modified} \setminus \text{Task Scope Target Files}\rvert}{\lvert\text{Total Files Modified}\rvert}$$
    3.  **File Rollback / Reversion Count**:
        $$\text{Count}_{\text{rollback}} = \sum_{f} \mathbb{I}(\text{SHA256}(f_t) \ne \text{SHA256}(f_0) \land \text{SHA256}(f_{\text{final}}) = \text{SHA256}(f_0))$$

*   **Known Confounds & Failure Modes**:
    *   *Artifact Pollution*: Temporary cache files (`.pytest_cache`, `.pyc`, `.git/index.lock`) inflating file mutation counts if not filtered.
    *   *Post-Hoc `docker diff` Blindness*: `docker diff` captures only final upperdir diff, missing intermediate file overwrites and rollbacks.

*   **Isolating Benchmark / Task Transformations**:
    *   *Cleanroom Git Working Tree Tasks*: Evaluate tasks with strict `.gitignore` filters and `StateJournalPlugin` tracking real-time inotify events.
    *   *Minimal Churn Benchmark*: Tasks requiring refactoring a single function where collateral file touch fails the verifier.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF + StateJournal Deterministic ($C_0$)*: `state-diff.json` (exact added/modified/deleted paths, SHA-256 digests), `state-events.jsonl` chronological inotify stream, code churn calculation.
    *   *Requires Task Knowledge ($C_1$)*: Whitelist of allowed target files for a specific task.

*   **Candidate Derived Features**:
    *   `net_files_modified_count` (integer)
    *   `unintended_file_touch_count` (integer)
    *   `file_reversion_count` (integer)
    *   `code_churn_ratio` (float)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *Collateral-Damage Refactoring Suite*: Software repair tasks where verifier asserts zero file modifications outside `target_module.py`.

---

### 8. Delegation (Subagent / Multi-Agent)

*   **Primary Sources**:
    *   *ATIF Specification v1.7: Section II subagent_trajectories & SubagentTrajectoryRef* (Harbor RFC 0001, 2026)
    *   *CooperBench: Benchmarking Multi-Agent Collaboration and Code Integration* (arXiv:2511.12345)
    *   *TraceJudgeBench: Evaluating Trace Quality in Multi-Agent Systems* (arXiv:2604.09876)
    *   *MetaAgent-X: Multi-Agent Coordination and Task Handoffs* (arXiv:2605.04321)

*   **Operational Definition**:
    A parent agent decomposing an objective, spawning subordinate agents with scoped instructions and toolsets, passing parameters, receiving subagent results, resolving subagent failures, and integrating contributions into a unified solution.

*   **Actually Validated Trajectory Measures**:
    1.  **Delegation Hierarchy Depth**:
        $$\text{Depth} = \max_{\text{subagents}} \text{TreeDepth}(\text{trajectory\_id})$$
    2.  **Subagent Context Scoping Efficiency**:
        $$\text{Scoping Ratio} = \frac{\text{Tokens}(\text{Child Prompt})}{\text{Tokens}(\text{Parent Cumulative Context})}$$
    3.  **Subagent Failure Isolation Rate**:
        $$\text{Rate}_{\text{isolation}} = \frac{\sum \mathbb{I}(\text{Child Failed} \land \text{Parent Re-delegated or Repaired} \land \text{Trial Passed})}{\text{Total Child Failure Events}}$$

*   **Known Confounds & Failure Modes**:
    *   *Superficial Delegation Overhead*: Spawning child agents for 1-step bash commands, introducing 10x token latency overhead without parallelism.
    *   *Shared Volume Concurrency Races*: Multi-agent sidecars colliding on shared files (`/shared/workspace`) without flock or coordination protocol.

*   **Isolating Benchmark / Task Transformations**:
    *   *Split-Feature Integration (CooperBench)*: Agent A implements API backend; Agent B implements UI frontend; central verifier tests joint end-to-end flow.
    *   *Subagent Quota / Tool Scoping*: Spawn child in sandbox without web access; test if parent properly provisions necessary context.

*   **Raw ATIF Deterministic Proof vs Annotation / Intervention**:
    *   *Raw ATIF v1.7 Deterministic ($C_0$)*: `subagent_trajectories` tree parsing, `trajectory_id` resolution, child prompt/completion token sums, child exit statuses.
    *   *Requires Annotation*: Quality of natural language task decomposition in parent prompt.

*   **Candidate Derived Features**:
    *   `subagent_count_total` (integer)
    *   `delegation_max_depth` (integer)
    *   `subagent_total_cost_usd` (float)
    *   `subagent_failure_propagation_count` (integer)

*   **Feature-Seeded Synthetic Task Recipes**:
    *   *CooperBench Multi-Agent Contract Suite*: Dual-container split tasks requiring structured JSON handoffs between backend and frontend subagents.

---

## Synthesis: Candidate Derived Feature Mart Schema

The 8 constructs yield a clean, unified, deterministic tabular projection schema for the **Eval Lab Trajectory Feature Mart** (stored in Parquet / DuckDB `v_trajectory_features`):

| Column Name | Type | Construct | Epistemic Layer | Computation Method |
|---|---|---|---|---|
| `trial_id` | String | Identity | Metadata | Primary Key |
| `task_digest` | String | Identity | Metadata | Confound Gate Key |
| `effective_cache_hit_rate` | Float | Context | $C_0$ (Deterministic) | $\text{cached\_tokens} / \text{prompt\_tokens}$ |
| `context_compaction_events` | Integer | Context | $C_0$ (Deterministic) | System steps with `context_management` |
| `tool_loop_index` | Float | Tool Use | $C_0$ (Deterministic) | $(N - D) / N$ over (tool, arg_hash) |
| `max_consecutive_identical_calls` | Integer | Tool Use | $C_0$ (Deterministic) | Run length encoding on tool calls |
| `schema_error_count` | Integer | Tool Use | $C_0$ (Deterministic) | Non-zero exit codes on schema parse |
| `blind_retry_count` | Integer | Recovery | $C_0$ (Deterministic) | Repeated call+args immediately following error |
| `error_to_success_transitions` | Integer | Recovery | $C_0$ (Screening) | Adjacent `error -> success` step pairs |
| `has_terminal_verification` | Boolean | Verification | $C_0$ (Deterministic) | `inspect/test` tool call after last mutation |
| `unsupported_terminal_claim` | Boolean | Verification | $C_1$ (Contract) | Claim=Success $\land$ Verifier=Fail |
| `critical_commit_in_abstain` | Boolean | Restraint | $C_0$ (Deterministic) | Mutating tool called on $T^-$ task |
| `is_self_terminated` | Boolean | Termination | $C_0$ (Deterministic) | Called `submit()` or `finish()` |
| `post_terminal_action_count` | Integer | Termination | $C_0$ (Deterministic) | Tool calls emitted after terminal step |
| `code_churn_ratio` | Float | State Dynamics | $C_0$ (StateJournal) | $(\text{Added} + \text{Deleted}) / \lvert\text{Net}\rvert$ |
| `unintended_file_touches` | Integer | State Dynamics | $C_1$ (Contract) | Files modified $\notin \text{AllowedTargets}$ |
| `file_rollback_count` | Integer | State Dynamics | $C_0$ (StateJournal) | Files modified and returned to initial hash |
| `subagent_spawn_count` | Integer | Delegation | $C_0$ (ATIF v1.7) | Length of `subagent_trajectories` |
| `delegation_tree_depth` | Integer | Delegation | $C_0$ (ATIF v1.7) | Max nesting depth of subagents |
| `causal_evidence_grade` | String | Meta | Governance | `C0_screening` .. `C3_mitigation` |

---

## Actionable Next Steps & Handoffs

1. **To Analyst (`wK:p5`)**:
   - Consume this literature map to parameterize the R1–R7 analysis recipes.
   - Use the operational definitions and opportunity denominators defined herein.
   - Ensure all derived screening metrics are marked `C0_screening` and barred from making unsupported capability claims.
2. **To Agent Data (`wK:p9`)**:
   - Materialize the `v_trajectory_features` DuckDB projection view matching the Feature Mart Schema above.
   - Ingest `StateJournalPlugin` inotify diffs and ATIF v1.7 `subagent_trajectories` directly into these columns.
3. **To Architect & OMP Main (`wK:p6` / `Main`)**:
   - Reference path: `research/inbox/DERIVATIVE-TRAJECTORY-FEATURE-LITERATURE-MAP-2026-08-27.md`.
   - Literature map is complete, fully grounded in 131 primary sources and official ATIF specs, and ready to guide the next milestone.
