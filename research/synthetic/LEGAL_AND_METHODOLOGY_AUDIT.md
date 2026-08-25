# Legal and Methodology Audit: Synthetic Agent-Capability Evaluation

**Document Version:** 1.2.0  
**Date:** 2026-08-25  
**Scope:** Primary-Source Licensing Tiers, Cleanroom Reimplementation Guidelines, Verification Safeguards, and Grounded Methodology Boundaries for Synthetic Benchmark Generation in `eval-lab`.

---

## 1. Executive Summary & Core Policy

The synthetic agent-capability evaluation system (`eval-lab` synthetic prototype V0) generates controllable, reproducible, and verifiable evaluation tasks to measure agent behaviors across four fundamental perturbation axes:
1. **Tool Unreliability** (`tool_unreliability`): Graceful failure recovery, fallback paths, and anomaly handling under explicit/implicit faults.
2. **Epistemic Restraint** (`epistemic_restraint`): Principled abstention, refusal, and scope boundary adherence when instructions are underspecified, unsolvable, or dangerous.
3. **Context Pressure** (`context_pressure`): Robustness against context noise, distractors, contradictory constraints, and needle-in-haystack context stuffing.
4. **Function Dependency DAGs** (`function_dag`): Multi-step function calling, dependency topology traversal, variable binding, and state preservation across chained tool invocations.

### Primary Legal Invariants
- **No Verbatim Code Copying Without Verified Open License:** All algorithmic pipelines, perturbation generators, verification harnesses, and spec builders in `eval-lab` are developed cleanroom from mathematical abstractions, formal algorithmic definitions, and published methodologies unless an explicit permissive license (BSD-3, MIT, Apache-2.0) is verified at repository root.
- **Primary-Source Verification Requirement:** Unverified secondary or aggregator claims (e.g. unverified Exa-discovered DIVE, VeriEnv, or tool-agent-shift claims) are explicitly **REJECTED** from the lab architecture unless corroborated by primary source inspection.
- **Execution-Based Ground Truth:** Every synthetic task is certified by deterministic execution-based verifiers (static reachability, 3x oracle pass, nop rejection, mutation testing, secret isolation, clean reset) — no subjective LLM judge serves as the sole ground truth. Note: `SyntheticCertificationGate` evaluates evidence explicitly supplied via executable runner callbacks or verified execution records; it does not automatically spawn or execute containerized Harbor controls without supplied runner or record evidence.
- **Cryptographic Provenance:** Every generated artifact carries an immutable content digest (`sha256:<64-hex>`), explicit lineage references, partition bindings (`train`/`dev`/`test`), and formal license provenance annotations.

---

## 2. Primary Source Audit & Compliance Decisions

### 2.1. TASTE (Task Synthesis from Tool Sequence Evolution)
- **Primary Citation:** *A Matter of TASTE: Improving Coverage and Difficulty of Agent Benchmarks* (arXiv:2605.28556, May 2026).
- **Verified License:** Paper publication: **CC BY 4.0**; Upstream repository code and benchmark datasets: **Restrictive / Review-Only** (proprietary/non-commercial review terms; NOT Apache-2.0, MIT, or CC-BY).
- **Decision:** **METHODOLOGY ONLY. Strictly Zero Code or Dataset Copying.**
- **Reused Concepts:** Sequence-space coverage, edit clustering (weighted Levenshtein distance), and validity-before-difficulty generation sequencing from the CC BY 4.0 paper methodology. All generation and evaluation code in `eval-lab` is developed independently cleanroom in Python without incorporating upstream code or data assets.

### 2.2. FuncBenchGen (Function-Dependency DAG Generator)
- **Primary Citation:** *Towards Reliable Benchmarking: A Contamination-Free, Controllable Evaluation Framework for Multi-Step LLM Function Calling* (Megagon Labs, arXiv:2509.26553, Sept 2025).
- **Verified License:** **BSD 3-Clause License.**
- **Decision:** **PERMISSIVE CANDIDATE & CLEANROOM IMPLEMENTATION.**
- **Reused Concepts:** Hidden function-dependency Directed Acyclic Graph (DAG), tunable core depth/width, connected/disconnected distractors, and target value verification. Implemented cleanroom in `src/evallab/synthetic_funcdag.py`.

### 2.3. AgentAbstain & AbstainGen
- **Primary Citation:** *AgentAbstain: Do LLM Agents Know When Not to Act?* (arXiv:2607.10059, July 2026).
- **Verified License:** Code: **MIT License**; Dataset: **CC BY 4.0**.
- **Decision:** **METHODOLOGY ONLY.**
- **Semantic Nuance:** Upstream semantics include deterministic critical-action checks plus response judges, not a formal mathematical proof of infeasibility. In `eval-lab`, we adapt the paired $T_{\text{act}} \leftrightarrow T_{\text{abstain}}$ contrastive formulation with deterministic machine-checkable precondition missing/contradiction assertions and hard zero-forbidden-mutation verifiers.

### 2.4. ToolMaze
- **Primary Citation:** *When Tools Fail: Benchmarking Dynamic Replanning and Anomaly Recovery in LLM Agents* (arXiv:2606.05806, June 2026).
- **Verified License:** **Unclear / No Root License in Primary Inspection.**
- **Decision:** **METHODOLOGY ONLY until formally clarified.**
- **Reused Concepts:** First-touch transient vs. persistent fault taxonomy and recovery cost tracking. Implemented cleanroom in `ToolFaultInjector`.

### 2.5. ToolBench-X
- **Primary Citation:** *Beyond Function Calling: Benchmarking Tool-Using Agents under Tool-Environment Unreliability* (arXiv:2606.25819, June 2026).
- **Verified License:** Code: **MIT**; Dataset Terms: **Prohibit redistribution/modification without approval.**
- **Decision:** **METHODOLOGY ONLY.**
- **Reused Concepts:** 5-hazard classification (specification drift, invocation error, execution failure, output drift, cross-source conflict) and guaranteed alternative recovery paths. Zero dataset harvesting.

### 2.6. Agent World Model (Snowflake)
- **Primary Citation:** *Agent World Model: Infinity Synthetic Environments for Agentic Reinforcement Learning* (Snowflake, arXiv:2602.10090, Feb 2026).
- **Verified License:** HF Dataset (`Snowflake/AgentWorldModel-1K`): **CC BY 4.0**; Repository Code License: **Not evident in primary root listing / unclear (verify before reuse).**
- **Decision:** **METHODOLOGY ONLY.**
- **Reused Concepts:** Borrow the staged pipeline (`scenario -> task -> DB -> spec -> environment -> verifier`) and pure-code execution verifier pattern across SQL and stateful environments.

### 2.7. SyntheticAgentTraceQA
- **Primary Citation:** arXiv:2607.29175 (July 2026).
- **Verified License:** **Paper Method; code/license requires verification before reuse.**
- **Decision:** **METHODOLOGY ONLY.**
- **Reused Concepts:** Execution-first trace generation.

### 2.8. Training-First / Arena Predecessors (SFT/RL Data Generators)
- **Projects:** **APIGen, RandomWorld, TOUCAN, TaskCraft, EnvFactory, Agent-World, ASTRA, EigenData, AReaL-SEA.**
- **Scope & Limitations:** These frameworks primarily generate SFT/RL training datasets or self-evolving arenas rather than evaluation-grade benchmark suites.
- **Decision:** Extract environment and verifier composition patterns only. **Do NOT treat as evaluation-grade without independent held-out partitions and 8-point certification gates.**

---

## 3. Predecessor Classification & Compliance Matrix

| Project / Paper | Primary Reference | Verified License | Compliance Classification | Reused Pattern in `eval-lab` |
|---|---|---|---|---|
| **TASTE** | arXiv:2605.28556 | Paper: CC BY 4.0; Code/Data: Restrictive / Review-Only | **Tier 4: Methodology Only** | Sequence edit clustering, validity-first logic |
| **FuncBenchGen** | arXiv:2509.26553 | BSD 3-Clause | **Tier 1: Permissive / Cleanroom** | Typed function DAG dependency generator |
| **AgentAbstain** | arXiv:2607.10059 | Code MIT, Dataset CC BY 4.0 | **Tier 2: Methodology Only** | Paired act/abstain contrastive tasks |
| **ToolMaze** | arXiv:2606.05806 | Unclear (No root license) | **Tier 3: Methodology Only** | First-touch transient/persistent faults |
| **ToolBench-X** | arXiv:2606.25819 | Code MIT, Data Restricted | **Tier 3: Methodology Only** | Hazard recovery paths |
| **Agent World Model** | arXiv:2602.10090 | Dataset CC BY 4.0 (`Snowflake/AgentWorldModel-1K`), Code Unclear | **Tier 2: Methodology Only** | Staged `scenario->DB->env->verifier` pipeline |
| **SyntheticTraceQA** | arXiv:2607.29175 | Paper Method | **Tier 3: Methodology Only** | Execution-first trace generation |
| **APIGen / TaskCraft** | Academic Pre-prints | SFT / Training Target | **Tier 3: Pattern Extraction** | Compositional tool generation patterns |

---

## 4. Rejection of Unverified Secondary Claims

To preserve scientific rigor and legal safety:
- **Exa Discovery Policy:** Secondary claims or unverified tools discovered via Exa searches (e.g. ungrounded claims regarding *DIVE*, *VeriEnv*, or *tool-agent-shift*) are **REJECTED** from the repository architecture unless verified by primary source code, author repositories, and formal licensing manifests.

---

## 5. Certification Gate Standards

Every synthetic evaluation task generated in `eval-lab` must pass the 8-point `SyntheticCertificate` gate before admission to `experimental` status:
1. `static_reachability`: Environment and tool dependencies resolve without deadlocks.
2. `clean_reset_passed`: Repeated setup/teardown yields identical initial states.
3. `oracle_3x_passed`: Reference solver passes 3 consecutive independent executions.
4. `nop_failed`: Empty agent receives zero reward.
5. `mutants_tested_count >= 3` & `mutants_failed_count == mutants_tested_count`: 100% rejection of intentional defect mutants.
6. `alignment_audit_passed`: Verifier assertions strictly match stated capability construct.
7. `regeneration_idempotent`: Identical `(seed, base_digest)` produces byte-identical task package.
8. `secret_isolation_passed`: Oracle and ground truth hidden outside agent inspection boundary.

**Evidence-Strict Enforcement & Harbor Boundary:** Certification requires affirmative execution evidence. The certification gate does not infer successful executions from spec metadata, nor does it automatically launch Harbor container runs in the background. Callers must supply executable runners or verified execution records for oracle, no-op, mutants, clean reset, and regeneration checks.
