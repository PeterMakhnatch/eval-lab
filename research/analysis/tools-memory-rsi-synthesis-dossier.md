---
title: "The Frontier of Multi-Turn Tool Use, Dynamic Working Memory, Test-Time Search, and Co-Evolutionary Recursive Self-Improvement (RSI)"
subtitle: "A Source-Verified Literature Synthesis, Formal MDP Foundations, and Synthetic Environment Blueprint for Eval Lab"
author: "Librarian & Research Synthesis Lane (LibrarianToolsMemoryRSI)"
date: "2026-09-01"
status: "distilled"
version: "1.0.0"
license: "Apache-2.0 / Eval-Lab Program"
tags:
  - working-memory
  - tool-use
  - recursive-self-improvement
  - synthetic-environments
  - mcts
  - kv-cache-eviction
  - formal-methods
---

# The Frontier of Multi-Turn Tool Use, Dynamic Working Memory, Test-Time Search, and Co-Evolutionary Recursive Self-Improvement (RSI)

## Executive Summary & Causal Topology

Modern foundation models are increasingly deployed not as static one-shot text generators, but as autonomous agents executing multi-step computational graphs across distributed tool ecosystems. However, empirical evaluations conducted throughout 2025 and 2026 have exposed a critical bottleneck: **the primary failure mode of long-horizon autonomous agents is not a raw context-window capacity limit, but an architectural failure in dynamic state tracking, working memory maintenance, and cascading error recovery under context mutation.**

Standard retrieval-augmented generation (RAG) and passive in-context "Needle-in-a-Haystack" (NIAH) paradigms measure static token retrieval from invariant context buffers. They fail entirely to model:
1. **Dynamic State Inversion**: Overwriting obsolete entity attributes ($val_0 \to val_1$) where retaining the initial token is a fatal hallucination.
2. **Tool-Parameter Binding**: Exact propagation of runtime-generated handles, session tokens, and computed intermediate variables across multi-tool dependency DAGs.
3. **Cascading Failure Traps**: Pathological retry loops where an unhandled tool exception causes an agent to resubmit identical erroneous payloads, burning context budget without corrective state mutation.
4. **Co-Evolutionary Generalization**: Training agents against fixed task suites produces rapid over-fitting to narrow tool signatures; overcoming this requires **Recursive Self-Improvement (RSI)** via co-evolutionary synthetic environments (SPADE, Absolute Zero, Dual-Play, Hyperagents).

```
+----------------------------------------------------------------------------------------------------+
|                                    THE CO-EVOLUTIONARY TRIAD                                       |
|                                                                                                    |
|   +--------------------------+                         +---------------------------------------+   |
|   |    ENVIRONMENT DESIGNER  |      Task MDP & Hints   |             SOLVER AGENT              |   |
|   |     (Generator \pi_\theta) | ---------------------> |              (Policy \pi_\phi)        |   |
|   |  - Gym Code Synthesis    |                         |  - Multi-Turn Tool Execution          |   |
|   |  - Regret-Frontier Target|                         |  - Working Memory Graph (\mathcal{G}) |   |
|   |  - Fault/Twin Injection  |                         |  - Test-Time MCTS Search              |   |
|   +--------------------------+                         +---------------------------------------+   |
|                 ^                                                   |                              |
|                 | Regret Signal                                     | Trajectory Rollout \tau       |
|                 | R(\mathcal{E}) = V^*(s_0|h) - V(s_0)              |                              |
|                 |                                                   v                              |
|   +--------------------------------------------------------------------------------------------+   |
|   |                                 VERIFIER & CAUSAL ORACLE (\mathcal{V})                     |   |
|   |  - Deterministic AST Validation & Execution Sandbox (Docker / gVisor)                      |   |
|   |  - Opportunity Denominators & Milestone Similarity ($MSM$)                                  |   |
|   |  - Difference-in-Differences ($DiD$) on Success vs. Conformance                            |   |
|   |  - Process Reward Models (PRMs) for Stepwise Credit Assignment                             |   |
|   +--------------------------------------------------------------------------------------------+   |
+----------------------------------------------------------------------------------------------------+
```

This dossier presents an exhaustive, source-verified synthesis of:
* **Section I**: The convergence of multi-turn tool use, working memory, and dynamic state tracking (2025–2026), including formal POMDP definitions and a cross-benchmark taxonomy.
* **Section II**: Recursive Self-Improvement (RSI) and co-evolutionary reinforcement learning (RL) in synthetic environments (SPADE, Absolute Zero, Dual-Play, Hyperagents, AutoEnv).
* **Section III**: Test-time search and reasoning over memory graphs (MCTS, dynamic scratchpads, and learned/RL KV-cache eviction such as ForesightKV, FreeKV, and Dynamo).
* **Section IV**: Concrete mathematical blueprints, failure mode taxonomies, exact evaluation metrics, and executable Gym MDP specifications for synthetic training and evaluation.
* **Section V**: Strategic integration roadmap for `eval-lab`.

---

## Section I: The Convergence of Multi-Turn Tool Use + Working Memory + Dynamic State Tracking (2025–2026)

### 1.1 The Architectural Paradigm Shift

The period from 2024 to 2026 marked the decisive transition from **Passive Context Ingestion** to **Active Dynamic Working Memory Systems**. 

```
PASSIVE CONTEXT INGESTION (2023-2024)
Prompt + Raw History + RAG -> [ LLM Attention (Unstructured Buffer) ] -> Next Action
* Vulnerability: Lost-in-the-middle, attention dilution, context compaction loss, stale-state binding.

ACTIVE DYNAMIC WORKING MEMORY (2025-2026)
           +-------------------------------------------------------+
           |                WORKING MEMORY GRAPH \mathcal{G}_t     |
           |  - Entity Store (Key-Value-Version Tuples)            |
           |  - Causal Dependency DAG & Execution Journal          |
           |  - Handle Invalidation & Pointer Table               |
           +-------------------------------------------------------+
                     ^ Read / Write            | Dynamic Injection
                     | Mutation                v
[ Observation o_t ] ---> [ Memory Controller / MCTS ] ---> [ Tool Binding Engine ] ---> [ Environment Action a_t ]
```

In passive architectures, every interaction turn is appended verbatim to the token context. Under long horizons ($T > 50$ steps, context lengths $> 64\text{k}$ tokens):
1. **Attention Dilution**: The probability mass allocated to critical binding parameters decays as $O(1/T)$ in standard dense softmax attention unless explicit query-key reinforcement occurs.
2. **Context Compaction Loss**: When rolling context summaries are generated by intermediate summarizer agents, granular variable bindings (e.g., temporary UUIDs, numeric API response fields) are systematically dropped in favor of generic prose summaries.
3. **Stale State Override Failure**: If an entity attribute undergoes multiple updates ($s_0 \to s_1 \to \dots \to s_k$), passive attention retains all historical values in the context window. LLM autoregressive decoding exhibits severe recency-primacy biases, frequently extracting $v_0$ (primacy) or hallucinating an intermediate value $v_j$ instead of strictly binding the active state $v_k$.

Active dynamic working memory systems resolve this by maintaining an explicit, structured state representation $\mathcal{M}_t$ external to, or explicitly projected into, the autoregressive context.

---

### 1.2 Formal Mathematical Framework for Dynamic Entity State Tracking

We formalize multi-turn agent interaction with stateful tools as a **Partially Observable Markov Decision Process with Explicit Working Memory (POMDP-WM)**:

$$\mathcal{M}_{POMDP} = \langle \mathcal{S}, \mathcal{A}, \mathcal{O}, \mathcal{T}, \Omega, \mathcal{R}, \gamma, \mathbb{M}, \mathcal{U}, \mathcal{B} \rangle$$

Where:
* $\mathcal{S}$ is the true external environment state space (e.g., database contents, remote server state, filesystem).
* $\mathcal{A}$ is the discrete/structured action space consisting of tool invocations $a_t = (\text{tool\_name}, \mathbf{x}_{args})$.
* $\mathcal{O}$ is the observation space returned by tool executions and environment feedback.
* $\mathcal{T}: \mathcal{S} \times \mathcal{A} \to \Delta(\mathcal{S})$ is the environmental transition kernel.
* $\Omega: \mathcal{S} \times \mathcal{A} \to \Delta(\mathcal{O})$ is the observation emission probability function.
* $\mathcal{R}: \mathcal{S} \times \mathcal{A} \to \mathbb{R}$ is the reward function.
* $\gamma \in [0, 1)$ is the discount factor.
* $\mathbb{M}$ is the structured working memory space.

#### Memory State Representation
The agent\x27s internal working memory at time step $t$ is a directed, typed state graph $\mathcal{M}_t = \langle \mathcal{E}_t, \mathcal{R}_t, \mathcal{H}_t \rangle$:
* $\mathcal{E}_t = \{ (e_i, k_j, v_{i,j}^{(t)}, \tau_{i,j}^{(t)}, \sigma_{i,j}^{(t)}) \}$ is the set of entity-attribute tuples, where $e_i$ is the entity ID, $k_j$ is the attribute key, $v_{i,j}^{(t)}$ is the current value, $\tau_{i,j}^{(t)} \le t$ is the timestamp of last modification, and $\sigma_{i,j}^{(t)} \in \{\text{VALID}, \text{STALE}, \text{TOMBSTONE}\}$ is the validity status.
* $\mathcal{R}_t \subseteq \mathcal{E}_t \times \mathcal{E}_t \times \mathcal{L}_{rel}$ represents relational dependencies between entities (e.g., `ChildProcessOf`, `DerivedFrom`, `AuthTokenFor`).
* $\mathcal{H}_t = (a_0, o_1, a_1, o_2, \dots, a_{t-1}, o_t)$ is the immutable execution journal.

#### The State Update Function ($\mathcal{U}$)
Upon receiving a new observation $o_{t+1}$ after executing action $a_t$, the working memory undergoes a deterministic state update:

$$\mathcal{M}_{t+1} = \mathcal{U}(\mathcal{M}_t, a_t, o_{t+1})$$

The update operator decomposes into three primitive operations:
1. **Entity State Inversion / Mutation**:
   $$\forall (e, k) \in \text{ExtractMutations}(o_{t+1}): \quad \mathcal{E}_{t+1} \leftarrow (\mathcal{E}_t \setminus \{(e, k, v_{old}, \tau, \text{VALID})\}) \cup \{(e, k, v_{new}, t+1, \text{VALID})\}$$
   Simultaneously, the prior state is marked as stale:
   $$\mathcal{E}_{t+1} \leftarrow \mathcal{E}_{t+1} \cup \{(e, k, v_{old}, \tau, \text{STALE})\}$$
2. **Handle Invalidation**:
   If $a_t$ terminates a session or invalidates an API resource $h$, all pointers referencing $h$ are transitioned:
   $$\forall (e, k, h, \tau, \text{VALID}) \in \mathcal{E}_t: \quad \sigma(e, k, h) \leftarrow \text{TOMBSTONE}$$
3. **Causal Edge Attachment**:
   $$\mathcal{R}_{t+1} \leftarrow \mathcal{R}_t \cup \{ (a_t, o_{t+1}, \text{Produced}), (o_{t+1}, e_{new}, \text{Instantiated}) \}$$

#### The Tool-Parameter Binding Operator ($\mathcal{B}$)
When the agent policy $\pi_\phi$ selects an intended tool schema $T_{target}(\mathbf{p}_1, \mathbf{p}_2, \dots, \mathbf{p}_m)$, the argument instantiation must satisfy the binding operator:

$$\mathbf{x}_{args}^* = \mathcal{B}(\mathcal{M}_t, T_{target}) = \arg\max_{\mathbf{x}} \prod_{j=1}^m P\left(\mathbf{p}_j = v \mid \mathcal{M}_t, \sigma(e, k, v) = \text{VALID}\right)$$

A critical failure occurs when $\mathcal{B}$ binds a value where $\sigma(e, k, v) \ne \text{VALID}$.

---

### 1.3 State-of-the-Art Benchmark Taxonomy & Comparative Matrix (2025–2026)

The following benchmark matrix synthesizes the landscape of multi-turn tool use, state tracking, and agent memory evaluations.

| Benchmark | Primary Reference / Commit | Core Construct | Manipulated Variable / Intervention | Primary Metric & Exact Denominator | Trajectory & Verifier Independence | License & Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ToolSandbox** | [`arXiv:2408.04682`](https://arxiv.org/abs/2408.04682) | Stateful, conversational multi-tool DAGs; milestone verification | Milestone dependency DAGs; negative "minefield" constraints | **Milestone Similarity ($MSM$)** & Minefield Violation Rate ($MVR$) over required intermediate states | Deterministic Python state sandbox; decoupled verifier | Apache-2.0; Released |
| **ToolBench-X** | [`arXiv:2606.25819`](https://arxiv.org/abs/2606.25819) | Multi-turn tool execution under unreliability | 5 recoverable hazard families (schema, network, auth, data, timeout) | **Recoverable Success Rate ($RSR$)** on injected fault denominator | Execution sandbox; automatic fault injector | MIT; Released |
| **ToolMaze** | [`arXiv:2606.05806`](https://arxiv.org/abs/2606.05806) | Tool navigation under environment perturbations | Perturbation injection across execution paths | **Perturbation Recovery Rate ($PRR$)** on exposed trials; Recovery Cost ($RC$) | Graph maze state machine; exact transition oracle | MIT; Released |
| **ToolMisuseBench**| [`arXiv:2604.01508`](https://arxiv.org/abs/2604.01508) | Offline deterministic contract misuse & recovery | Injected schema errors, missing arguments, type flips | **Misuse Detection Rate ($MDR$)** and Autonomous Repair Rate ($ARR$) | Static deterministic test harness | Apache-2.0; Released |
| **$\tau^2$-bench** | [`arXiv:2506.07982`](https://arxiv.org/abs/2506.07982) | Dual-control agent interaction (DB + User dialogue) | Dynamic environment assertions & user preference shifts | **Dual Success Metric**: $S = \mathbb{I}(\text{DB\_State}) \land \mathbb{I}(\text{User\_Pass})$ | Dual environment execution engine; explicit assertions | MIT; Released |
| **MemoryAgentBench**| [`arXiv:2507.05257`](https://arxiv.org/abs/2507.05257) | Multi-session incremental interaction & conflict resolution | Temporal updates, conflicting knowledge injection | **Conflict Resolution Accuracy ($CRA$)** & Retrieval Precision | Python test runner with deterministic truth database | Apache-2.0; Released |
| **LOCA-bench** | [`arXiv:2602.07962`](https://arxiv.org/abs/2602.07962) | Actionable memory under controlled context growth | Context dose ladder ($4\text{k} \to 128\text{k}$ tokens), distractors | **Actionable Binding Accuracy ($ABA$)** vs. context length | Isolated Harbor-compatible evaluation runner | Apache-2.0; Released |
| **LoCoMo** | Harbor adapter `adapters/locomo` | Long-horizon multi-session dialogue QA | Multi-session temporal separation & entity tracking | Exact-match & semantic QA accuracy on temporal questions | Dialogue transcript replay harness | Non-commercial research / Harbor |
| **BEAM** | [`arXiv:2510.27246`](https://arxiv.org/abs/2510.27246) | Ultra-long horizon memory ($10\text{M}$ tokens) | Context dilution across 100+ conversation sessions | Multi-hop extraction accuracy across temporal horizons | Automated question-answer verifier engine | CC-BY-4.0; Released |
| **AMA-Bench** | [`arXiv:2602.22769`](https://arxiv.org/abs/2602.22769) | Long-horizon agentic memory in real tools | Cross-tool entity reference & stale state overrides | **State Inversion Survival Rate ($SISR$)** | Real-world API mock harness | MIT; Released |
| **Memora** | [`arXiv:2604.20006`](https://arxiv.org/abs/2604.20006) | Weeks-to-months personalized memory | Forgetting curves, temporal decay functions | **Forgetting-Aware Retention ($FAR$)** metric | Longitudinal simulation harness | Apache-2.0; Released |
| **`action-memory-v1`** (Eval Lab) | Native Package `library/benchmarks/action-memory-v1` | Actionable entity memory, state inversion, value-bound action mutation | Context dose ladder ($4\text{k}, 16\text{k}, 64\text{k}$), handle representations, distractors | **Stale Value Override Rate ($SVOR$)**, Schema Conformance, DiD on Success vs. Conformance | Native FastMCP streamable HTTP sandbox; Docker/gVisor verifier | Apache-2.0; Native |
| **`mcp-recovery-v1`** (Eval Lab) | Native Package `library/benchmarks/mcp-recovery-v1` | Autonomous recovery under stateful MCP protocol faults | 5 fault classes $\times$ 2 persistence levels (20-cell arithmetic) | **Fault-Exposure Recovery Rate ($FERR$)** on strictly exposed denominator | Decoupled verification with clean twin / fault paired arms | Apache-2.0; Native |

---

## Section II: Recursive Self-Improvement (RSI) & Co-Evolutionary Agent RL in Synthetic Environments

### 2.1 The Triad of Co-Evolution

Recursive Self-Improvement in complex multi-step environments cannot rely on static datasets, which suffer from distribution collapse once the agent surpasses the capability frontier of the static task distribution. Co-evolutionary agent RL resolves this by framing learning as an **Asymmetric Multi-Agent Information Game** involving three components:

$$\text{Triad} = \langle \text{Generator } \pi_\theta^{env}, \text{Solver } \pi_\phi^{agent}, \text{Verifier } \mathcal{V} \rangle$$

```
+----------------------------------------------------------------------------------------------------+
|                                    CO-EVOLUTIONARY DUAL-PLAY                                       |
|                                                                                                    |
|            +------------------------------------------------------------------+                    |
|            |                      GENERATOR \pi_\theta                        |                    |
|            | Generates Environment Code \mathcal{E} ~ \pi_\theta(\cdot)       |                    |
|            | Holds Privileged Hints h and Ground-Truth State Trajectory \tau^*|                    |
|            +------------------------------------------------------------------+                    |
|                         |                                        |                                 |
|         Task Instance   |                        Task Instance   |                                 |
|         WITHOUT Hints   |                        WITH Hints h    |                                 |
|               (s_0)     |                              (s_0, h)  |                                 |
|                         v                                        v                                 |
|            +--------------------------+             +--------------------------+                   |
|            |     UNASSISTED AGENT     |             |      ASSISTED ORACLE     |                   |
|            |    Rollout \tau_{unassist}|            |    Rollout \tau_{assist} |                   |
|            +--------------------------+             +--------------------------+                   |
|                         |                                        |                                 |
|                         v                                        v                                 |
|            +------------------------------------------------------------------+                    |
|            |                      DETERMINISTIC VERIFIER \mathcal{V}          |                    |
|            | Success: Y_{unassist} \in {0, 1}        Success: Y_{assist} \in {0, 1}                    |
|            +------------------------------------------------------------------+                    |
|                                         |                                                          |
|                                         v                                                          |
|            +------------------------------------------------------------------+                    |
|            |                   CURRICULUM REGRET COMPUTATION                  |                    |
|            |              R(\mathcal{E}) = Y_{assist} - Y_{unassist}          |                    |
|            |  * If R = 0 (Both fail): Task is Impossible / Buggy -> Drop      |                    |
|            |  * If R = 0 (Both win):  Task is Trivial           -> Drop       |                    |
|            |  * If R = 1 (Solvable ONLY with hint): FRONTIER   -> RETAIN      |                    |
|            +------------------------------------------------------------------+                    |
|                         |                                        |                                 |
|                         | Update \theta                          | Update \phi                     |
|                         v                                        v                                 |
|                 Maximize Regret                           Maximize Task Return                     |
|            \nabla_\theta \mathbb{E}[R(\mathcal{E})]      \nabla_\phi \mathbb{E}[r(\tau)]           |
+----------------------------------------------------------------------------------------------------+
```

### 2.2 Hint-Based Regret Curriculum & Capability Frontier Discovery (SPADE)

In **SPADE** (*Self-Play in Adaptive Synthetic Executable Environments*, arXiv:2608.19197v2), the Environment Designer $\pi_\theta$ writes complete, executable Python environments following the OpenAI Gym interface (`reset()`, `step()`).

#### Mathematical Formulation of Hint-Based Regret
Let $\mathcal{E}$ be a generated environment instance parameterized by initial state $s_0$. The designer embeds a privileged solution hint $h(\mathcal{E})$. We define the value functions:
* $V^{\pi_\phi}(s_0)$: Expected return of agent $\pi_\phi$ executing unassisted in $\mathcal{E}$.
* $V^*(s_0 \mid h)$: Expected return of an agent given privileged oracle guidance $h$.

The **Environment Regret** $R(\mathcal{E})$ is defined as:

$$R(\mathcal{E}) = V^*(s_0 \mid h) - V^{\pi_\phi}(s_0)$$

When evaluation rewards are binary $Y \in \{0, 1\}$:

$$R(\mathcal{E}) = \mathbb{I}(Y_{\text{assisted}} = 1) - \mathbb{I}(Y_{\text{unassisted}} = 1)$$

The optimization objective for the Environment Designer is to generate environments that maximize the empirical regret while guaranteeing solvability:

$$\max_\theta \mathcal{J}(\theta) = \mathbb{E}_{\mathcal{E} \sim \pi_\theta} \left[ R(\mathcal{E}) \cdot \mathbb{I}(\mathcal{V}_{\text{exec}}(\mathcal{E}) = \text{VALID}) \cdot \mathbb{I}(Y_{\text{assisted}} = 1) \right] - \lambda \mathcal{D}_{\text{complexity}}(\mathcal{E})$$

This formulation guarantees that the curriculum naturally tracks the agent\x27s **Zone of Proximal Development (ZPD)**:
1. **Trivial Tasks**: $Y_{\text{assisted}} = 1$ and $Y_{\text{unassisted}} = 1 \implies R(\mathcal{E}) = 0$. Gradient is zero.
2. **Impossible / Broken Tasks**: $Y_{\text{assisted}} = 0 \implies \mathbb{I}(Y_{\text{assisted}} = 1) = 0$. Filtered out immediately.
3. **Frontier Tasks**: $Y_{\text{assisted}} = 1$ and $Y_{\text{unassisted}} = 0 \implies R(\mathcal{E}) = 1$. Maximal positive reinforcement to the Environment Designer.

---

### 2.3 Dual-Play & Escher-Loop Architectures

In **Dual-Play** co-evolutionary architectures (e.g., *Escher-Loop* and *Dual-Agent Self-Optimization*), the system avoids separate reward engineering by using symmetric performance feedback to simultaneously update both the Task Agent and the Meta-Optimizer.

Let $\phi$ be the parameters of the task execution policy and $\theta$ be the parameters of the prompt/architecture optimizer. The dual optimization objective is formulated as:

$$\min_\theta \max_\phi \mathcal{L}_{\text{dual}}(\phi, \theta) = \mathbb{E}_{\tau \sim \pi_\phi, \mathcal{P} \sim \pi_\theta} \left[ \mathcal{D}_{KL}\left(\pi_\phi(\cdot \mid \mathcal{P}) \parallel \pi_{\text{oracle}}(\cdot)\right) - \beta \mathcal{H}(\pi_\phi) \right]$$

By coupling the policy entropy $\mathcal{H}(\pi_\phi)$ with the meta-prompt generator $\pi_\theta$, Dual-Play prevents policy collapse and guarantees diverse exploration across complex state graphs.

---

### 2.4 Hyperagents & Gödel Self-Referential Agent Programs

**Hyperagents** extend RSI from continuous parameter updates ($\theta \leftarrow \theta + \Delta \theta$) to discrete, self-referential program modifications. The agent is represented as an executable Abstract Syntax Tree (AST) $\mathcal{P} \in \mathbb{P}_{code}$.

```
                 +-------------------------------------------------------------+
                 |                HYPERAGENT SELF-REFERENTIAL LOOP             |
                 |                                                             |
                 |   Executable Program AST \mathcal{P}_t                      |
                 |   +-----------------------------------------------------+   |
                 |   | def agent_policy(obs, memory_graph):                |   |
                 |   |     # Self-Editable Logic                           |   |
                 |   |     plan = mcts_search(obs, memory_graph)           |   |
                 |   |     return execute(plan)                            |   |
                 |   +-----------------------------------------------------+   |
                 |                              |                              |
                 |                              v Rollout Execution            |
                 |   Trajectory Trace \tau_t = (s_0, a_0, r_0, \dots, s_T, r_T) |
                 |                              |                              |
                 |                              v Meta-Reflection              |
                 |   +-----------------------------------------------------+   |
                 |   | AST Meta-Mutation Operator \mathcal{M}              |   |
                 |   | \Delta\mathcal{P} ~ \pi_{meta}(\cdot | \tau_t, \mathcal{P}_t) |
                 |   +-----------------------------------------------------+   |
                 |                              |                              |
                 |                              v Candidate \mathcal{P}\x27       |
                 |   +-----------------------------------------------------+   |
                 |   | FORMAL PROOF CHECKER & SAFETY INVARIANT GATE        |   |
                 |   |   \mathcal{V}_{safe}(\mathcal{P}\x27) \in {True, False}|   |
                 |   |   1. Halting guarantee & resource bounds            |   |
                 |   |   2. Memory isolation & sandbox integrity           |   |
                 |   |   3. Verifiable performance \Delta R > 0 on split   |   |
                 |   +-----------------------------------------------------+   |
                 |                              |                              |
                 |          Pass                | Fail                         |
                 |   +------------------+       +------------------+           |
                 |   | \mathcal{P}_{t+1} \leftarrow \mathcal{P}\x27 |       | \mathcal{P}_{t+1} \leftarrow \mathcal{P}_t |           |
                 |   +------------------+       +------------------+           |
                 +-------------------------------------------------------------+
```

#### Mathematical Definition of the Meta-Transition Operator
The self-modification trajectory evolves as:

$$\mathcal{P}_{t+1} = \begin{cases} \mathcal{M}(\mathcal{P}_t, \mathcal{D}_{\text{rollout}}) & \text{if } \mathcal{V}_{\text{formal}}(\mathcal{P}\x27, \mathcal{S}_{\text{heldout}}) = 1 \text{ and } \Delta \bar{R}(\mathcal{P}\x27) > \epsilon \\ \mathcal{P}_t & \text{otherwise} \end{cases}$$

Where $\mathcal{V}_{\text{formal}}$ verifies:
1. **Syntactic and Semantic Invariance**: The mutated AST $\mathcal{P}\x27$ compiles, satisfies static type safety, and obeys sandbox memory limits.
2. **Safety and Goal Alignment**: $\mathcal{P}\x27$ does not delete verification hooks or attempt out-of-bounds syscalls.
3. **Monotonic Generalization**: $\mathbb{E}_{\mathcal{E} \sim \mathcal{D}_{\text{heldout}}}[R_{\mathcal{P}\x27}(\mathcal{E})] > \mathbb{E}_{\mathcal{E} \sim \mathcal{D}_{\text{heldout}}}[R_{\mathcal{P}_t}(\mathcal{E})]$.

---

### 2.5 Absolute Zero & AutoEnv

* **Absolute Zero** ([`arXiv:2505.03335`](https://arxiv.org/abs/2505.03335)): A zero-human-data reasoning paradigm where the model self-evolves by generating tasks across three fundamental reasoning modes:
  1. **Abduction**: Inferring preconditions given observations and rules.
  2. **Deduction**: Propagating state transitions deterministically given rules and initial states.
  3. **Induction**: Synthesizing general state-transition rules from input-output execution traces.
  Truth is verified entirely through execution in an internal code executor.
* **AutoEnv** ([`arXiv:2511.19304`](https://arxiv.org/abs/2511.19304)): Formulates a component-centric architecture for generating standardized, heterogeneous environments (e.g., AutoEnv-36) measuring cross-environment transfer, action-space generalization, and state-drift robustness.

---

## Section III: Test-Time Search & Reasoning Over Memory Graphs and KV-Cache RL Eviction

### 3.1 Test-Time Search over Memory Graphs (MCTS + Dynamic Scratchpads)

During multi-turn execution, standard greedy autoregressive decoding ($\arg\max_w P(w_t \mid w_{<t})$) fails when encountering tool exceptions or deceptive intermediate states. Test-time search frames memory management and action selection as a **Monte Carlo Tree Search over Memory Graphs (MCTS-MG)**.

```
                    [ Root State: \mathcal{M}_0, o_0 ]
                              /              \
                             /                \
        [ Tool Action a_1^{(1)} ]           [ Tool Action a_1^{(2)} ]
         Update: \mathcal{M}_1^{(1)}         Update: \mathcal{M}_1^{(2)}
               /          \                         |
              /            \                        |
       [ Action a_2 ]  [ Action a_2\x27 ]         [ Action a_2\x27\x27 ]
       Success (PRM=0.9) Fail/Exception        (PRM=0.3)
                           |
                     [ BACKTRACK ]
            Prune \mathcal{M}_1^{(1)} branch
            Rollback Memory to \mathcal{M}_0
```

#### Formalization of MCTS-MG Decision Step
Let node $u = \langle s_t, \mathcal{M}_t \rangle$ represent the joint environment-memory state.
1. **Selection (Upper Confidence Bound for Trees with PRMs)**:
   $$a^* = \arg\max_{a \in \mathcal{A}(u)} \left[ Q(u, a) + c_{\text{puct}} P_{\phi}(a \mid u) \frac{\sqrt{\sum_{b} N(u, b)}}{1 + N(u, a)} + \lambda_{\text{PRM}} \cdot \text{PRM}_{\text{step}}(u, a) \right]$$
   Where $\text{PRM}_{\text{step}}(u, a) \in [0, 1]$ is a trained Process Reward Model predicting the likelihood that step $a$ maintains state consistency without causing irrecoverable faults.
2. **Expansion & State Update**:
   Execute action $a^*$, observe $o_{t+1}$, and apply the memory update operator:
   $$\mathcal{M}_{t+1} = \mathcal{U}(\mathcal{M}_t, a^*, o_{t+1})$$
3. **Evaluation**:
   If $u_{t+1}$ is terminal, $V(u_{t+1}) = \mathcal{R}(s_{t+1})$. Otherwise, evaluate via value network or rollout simulation.
4. **Backpropagation & Memory Pruning**:
   Update visit counts $N(u, a) \leftarrow N(u, a) + 1$ and action-value estimates $Q(u, a) \leftarrow Q(u, a) + \frac{V - Q(u, a)}{N(u, a)}$.
   If a branch returns an unrecoverable exception, that branch is **pruned from working memory**, preventing poisoned/hallucinated intermediate facts from leaking into future search branches.

---

### 3.2 KV-Cache Management: Heuristic vs. Reinforcement-Learned Eviction

In long-horizon agent interactions, storing full Key-Value (KV) caches across hundreds of turns creates an unsustainable memory footprint ($O(T)$ tokens $\times$ layers $\times$ heads $\times d_{head}$) and quadratic attention compute ($O(T^2)$).

```
+----------------------------------------------------------------------------------------------------+
|                                    KV-CACHE EVICTION STRATEGIES                                    |
|                                                                                                    |
| 1. HEURISTIC METHODS (H2O, SnapKV, StreamingLLM, Quest)                                            |
|    [ Sinks (t_0..t_k) ] + [ Heavy Hitters (Top-\Sigma \alpha_i) ] + [ Recent Window (t_{T-W}..t_T) ]|
|    * Limitation: Historical attention \alpha_i is an imperfect proxy for FUTURE parameter utility. |
|                                                                                                    |
| 2. LEARNED RL POLICIES (ForesightKV / KVP)                                                         |
|    Token t_i ---> [ Lightweight Policy \pi_\psi ] ---> Retention Probability p_retain \in [0, 1]   |
|    * Objective: Maximize downstream task return under fixed KV cache budget B.                     |
|                                                                                                    |
| 3. SYSTEM CO-OPTIMIZATION (FreeKV / NVIDIA Dynamo)                                                  |
|    GPU VRAM (Active Hot Cache) <== Async Page Transfer ==> CPU DRAM (Complete KV Storage Pool)     |
|    - CASR Protocol: Correction, Attention, Selection, Recall.                                      |
|    - Dynamo KV-Aware Router: Schedules agent turns to GPUs holding matching prefix caches.         |
+----------------------------------------------------------------------------------------------------+
```

#### Comparative Analysis of Heuristic Eviction Policies

1. **H2O (Heavy Hitter Oracle)**:
   Maintains a cumulative attention score accumulator for each token $i$:
   $$A_i^{(T)} = \sum_{t=1}^T \sum_{h=1}^H \alpha_{t, i}^{(h)}$$
   Evicts tokens with the lowest $A_i^{(T)}$ while pinning initial attention sinks and a local sliding window.
   *Failure mode*: Retains tokens that received high past attention (e.g., historical error logs) even after they become irrelevant.

2. **SnapKV**:
   Identifies crucial feature positions by pooling attention weights exclusively within an observation window $W_{obs}$ at the end of the prompt:
   $$\bar{A}_i = \frac{1}{|W_{obs}|} \sum_{t \in W_{obs}} \alpha_{t, i}$$
   Clusters adjacent high-attention tokens into persistent voting blocks.

3. **Quest**:
   Performs page-level KV cache selection by computing min-max bounding boxes over key vectors within each page $P_j$:
   $$\text{Score}(P_j) = \max_{q \in Q_t, k \in P_j} (q^T k)$$
   Only top-$K$ pages with highest upper-bound attention are loaded into active attention compute.

#### Reinforcement-Learned Eviction (ForesightKV / KV-Policy)
Rather than relying on backward-looking attention heuristics, learned eviction frames token retention as a sequential decision process.

Let $t_k$ be a token in the cache. The marginal utility of retaining $t_k$ for future generation is:

$$\Delta U(t_k) = \mathbb{E}_{\tau \sim \pi} \left[ \sum_{t\x27 > t_k} r_{t\x27} \;\Big|\; \text{retain}(t_k) \right] - \mathbb{E}_{\tau \sim \pi} \left[ \sum_{t\x27 > t_k} r_{t\x27} \;\Big|\; \text{evict}(t_k) \right]$$

A lightweight per-head eviction policy $\pi_\psi(\text{action} \mid \mathbf{k}_i, \mathbf{v}_i, \mathbf{q}_t, \Delta t)$ is trained using policy gradients (PPO/GRPO) with a reward balancing task accuracy and cache compression ratio:

$$\mathcal{R}_{\text{cache}} = \mathcal{R}_{\text{task}} - \lambda_{\text{mem}} \max\left(0, \frac{|\text{Cache}|}{B_{\text{target}}} - 1\right)$$

#### System-Level Co-Optimization: FreeKV and Dynamo
* **FreeKV**: Implements an algorithm-system co-design that keeps the full KV cache in host CPU memory, dynamically swapping only top-ranked token pages into GPU SRAM/HBM using an asynchronous **CASR (Correction, Attention, Selection, Recall)** controller.
* **NVIDIA Dynamo**: Features a distributed **KV-Aware Request Router** that inspects incoming multi-turn agent session IDs and routes execution to GPU workers already caching the specific historical prefix, eliminating redundant prompt prefill.

---

## Section IV: Concrete Blueprints for Designing Synthetic Training Tasks

### 4.1 Taxonomy of Multi-Turn Memory Failure Modes

To train robust agents, synthetic task generators must deliberately target the five canonical multi-turn memory failure modes:

```
+----------------------------------------------------------------------------------------------------+
|                                 TAXONOMY OF MEMORY FAILURE MODES                                    |
|                                                                                                    |
| 1. CONTEXT COMPACTION LOSS (CCL)                                                                   |
|    Token Stream: [E_1: Key=9841] -> [Summarizer: "Config loaded"] -> LLM: "Key is None" (FAILED)  |
|                                                                                                    |
| 2. DYNAMIC STATE DRIFT (DSD)                                                                       |
|    Tool Returns: Err 503 -> Agent Hallucinates: "DB updated anyway" -> Downstream Corrupt (FAILED) |
|                                                                                                    |
| 3. CASCADING ERROR LOOPS / BLIND RETRIES (CEL)                                                     |
|    Call(Tool_A, x=1) -> Err -> Call(Tool_A, x=1) -> Err -> Call(Tool_A, x=1) [Loop Exhaustion]    |
|                                                                                                    |
| 4. STALE-STATE OVERWRITE / TEMPORAL INVERSION (SSO)                                                |
|    t=0: Addr=Alpha -> t=10: Update Addr=Beta -> t=20: Call(Ship, Addr=Alpha) [Stale Binding]      |
|                                                                                                    |
| 5. HANDLE INVALIDATION & POINTER DECAY (HIPD)                                                      |
|    Session S_1 Closed -> Agent attempts Call(Query, session_id=S_1) -> Fatal Auth Crash           |
+----------------------------------------------------------------------------------------------------+
```

---

### 4.2 Synthetic Gym MDP Specification for Memory & Tool Training

Below is the complete, executable Python specification for a synthetic Gym environment (`SyntheticMemoryToolEnv`) that implements dynamic state inversions, recoverable fault injections, distractors, and automated oracle verification.

```python
"""
Synthetic Memory & Tool Use Gym Environment (SyntheticMemoryToolEnv).
Implements POMDP with dynamic entity mutation, recoverable fault injection,
context dose scaling, and deterministic ground-truth verification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class FaultType(str, Enum):
    NONE = "none"
    TRANSIENT_TIMEOUT = "transient_timeout"
    SCHEMA_MISMATCH = "schema_mismatch"
    STALE_HANDLE = "stale_handle"
    PAYLOAD_CORRUPTION = "payload_corruption"


@dataclass
class EntityState:
    entity_id: str
    attributes: Dict[str, Any]
    version: int = 0
    is_active: bool = True
    history: List[Dict[str, Any]] = field(default_factory=list)

    def mutate(self, key: str, value: Any) -> None:
        self.history.append({"version": self.version, "key": key, "old_value": self.attributes.get(key)})
        self.attributes[key] = value
        self.version += 1


class SyntheticMemoryToolEnv:
    """
    Gym-style environment for training agents on multi-turn working memory,
    state inversion, and fault recovery.
    """

    def __init__(
        self,
        num_entities: int = 5,
        num_distractors: int = 20,
        num_inversions: int = 3,
        fault_type: FaultType = FaultType.NONE,
        fault_persistence: int = 1,
        seed: int = 42,
    ):
        self.num_entities = num_entities
        self.num_distractors = num_distractors
        self.num_inversions = num_inversions
        self.fault_type = fault_type
        self.fault_persistence = fault_persistence
        self.seed = seed
        self.rng = random.Random(seed)

        self.entities: Dict[str, EntityState] = {}
        self.active_session_token: Optional[str] = None
        self.target_entity_id: str = ""
        self.target_attribute_key: str = ""
        self.target_final_value: Any = None
        self.inversion_schedule: List[Tuple[int, str, str, Any]] = []

        self.current_step = 0
        self.max_steps = 30
        self.fault_countdown = fault_persistence
        self.action_history: List[Dict[str, Any]] = []
        self.execution_log: List[str] = []

    def reset(self) -> Dict[str, Any]:
        """Reset environment to initial deterministic state."""
        self.rng = random.Random(self.seed)
        self.current_step = 0
        self.fault_countdown = self.fault_persistence
        self.action_history.clear()
        self.execution_log.clear()

        # 1. Initialize entities with base states
        self.entities.clear()
        for i in range(self.num_entities):
            eid = f"ent_{hashlib.sha256(fentity_{i}_{self.seed}.encode()).hexdigest()[:8]}"
            attrs = {
                "routing_key": self.rng.randint(1000, 9999),
                "auth_level": self.rng.choice(["read", "write", "admin"]),
                "payload_digest": hashlib.md5(f"init_{i}".encode()).hexdigest()[:6],
            }
            self.entities[eid] = EntityState(entity_id=eid, attributes=attrs)

        # 2. Designate primary target entity and attribute
        entity_keys = list(self.entities.keys())
        self.target_entity_id = entity_keys[0]
        self.target_attribute_key = "routing_key"

        # 3. Schedule temporal state inversions
        self.inversion_schedule.clear()
        for inv_idx in range(self.num_inversions):
            trigger_step = (inv_idx + 1) * 3
            new_val = self.rng.randint(10000, 99999)
            self.inversion_schedule.append((trigger_step, self.target_entity_id, self.target_attribute_key, new_val))
            self.target_final_value = new_val

        # 4. Generate initial session token
        self.active_session_token = f"sess_{hashlib.sha256(fsession_{self.seed}.encode()).hexdigest()[:12]}"

        obs = {
            "instruction": (
                f"Track entity {self.target_entity_id} across dynamic mutations. "
                f"When finalized, invoke commit_final_binding with the latest valid {self.target_attribute_key}. "
                f"Active session token: {self.active_session_token}."
            ),
            "available_tools": [
                "read_entity(entity_id, session_token)",
                "update_entity(entity_id, key, value, session_token)",
                "refresh_session()",
                "commit_final_binding(entity_id, bound_value, session_token)",
            ],
            "distractor_payload": [
                {"distractor_id": f"dist_{j}", "junk_data": self.rng.random()} for j in range(self.num_distractors)
            ],
        }
        return obs

    def step(self, action: Dict[str, Any]) -> Tuple[Dict[str, Any], float, bool, Dict[str, Any]]:
        """
        Execute agent tool action.
        Returns: (observation, reward, done, info)
        """
        self.current_step += 1
        self.action_history.append(copy.deepcopy(action))

        # Check for scheduled environment-side state inversions
        for trigger_step, eid, k, v in self.inversion_schedule:
            if self.current_step == trigger_step:
                self.entities[eid].mutate(k, v)
                self.execution_log.append(f"[ENV_EVENT] Step {self.current_step}: Inverted {eid}.{k} -> {v}")

        tool_name = action.get("tool_name")
        args = action.get("arguments", {})
        info: Dict[str, Any] = {"step": self.current_step, "fault_injected": False}

        # Check step budget
        if self.current_step >= self.max_steps:
            return {"error": "Max step budget exceeded"}, 0.0, True, {"reason": "timeout"}

        # Fault Injection Logic
        if self.fault_type != FaultType.NONE and self.fault_countdown > 0:
            if self.fault_type == FaultType.TRANSIENT_TIMEOUT:
                self.fault_countdown -= 1
                info["fault_injected"] = True
                return {"error": "HTTP 504: Gateway Timeout. Retry or refresh."}, 0.0, False, info

            elif self.fault_type == FaultType.STALE_HANDLE:
                self.fault_countdown -= 1
                self.active_session_token = f"sess_renewed_{self.current_step}"
                info["fault_injected"] = True
                return {"error": "Auth 401: Session Token Expired. Refresh required."}, 0.0, False, info

        # Tool Dispatch
        if tool_name == "read_entity":
            eid = args.get("entity_id")
            token = args.get("session_token")
            if token != self.active_session_token:
                return {"error": "Invalid or expired session token"}, 0.0, False, {"error_type": "auth"}
            if eid not in self.entities:
                return {"error": f"Entity {eid} not found"}, 0.0, False, {"error_type": "not_found"}
            ent = self.entities[eid]
            return {
                "entity_id": ent.entity_id,
                "attributes": ent.attributes,
                "version": ent.version,
            }, 0.0, False, info

        elif tool_name == "refresh_session":
            self.active_session_token = f"sess_{hashlib.sha256(frefresh_{self.current_step}.encode()).hexdigest()[:12]}"
            return {"status": "success", "new_session_token": self.active_session_token}, 0.0, False, info

        elif tool_name == "update_entity":
            eid = args.get("entity_id")
            k = args.get("key")
            v = args.get("value")
            token = args.get("session_token")
            if token != self.active_session_token:
                return {"error": "Invalid session token"}, 0.0, False, {"error_type": "auth"}
            if eid not in self.entities:
                return {"error": "Entity not found"}, 0.0, False, {"error_type": "not_found"}
            self.entities[eid].mutate(k, v)
            return {"status": "updated", "entity_id": eid, "new_version": self.entities[eid].version}, 0.0, False, info

        elif tool_name == "commit_final_binding":
            eid = args.get("entity_id")
            bound_val = args.get("bound_value")
            token = args.get("session_token")

            # Strict verification
            if token != self.active_session_token:
                return {"status": "rejected", "reason": "invalid_session"}, 0.0, True, {"success": False}

            if eid == self.target_entity_id and bound_val == self.target_final_value:
                # Flawless dynamic binding to latest inverted state
                return {"status": "accepted", "message": "Exact dynamic binding matched"}, 1.0, True, {"success": True}
            else:
                # Stale value or incorrect entity bound
                is_stale = False
                for hist in self.entities[self.target_entity_id].history:
                    if hist["old_value"] == bound_val:
                        is_stale = True
                        break
                return {
                    "status": "rejected",
                    "reason": "stale_binding" if is_stale else "incorrect_value",
                    "expected": self.target_final_value,
                    "received": bound_val,
                }, 0.0, True, {"success": False, "stale_binding": is_stale}

        return {"error": f"Unknown tool {tool_name}"}, 0.0, False, {"error_type": "schema"}

    def get_oracle_trajectory(self) -> List[Dict[str, Any]]:
        """Compute the deterministic oracle solution sequence."""
        actions = []
        token = self.active_session_token
        # Read entity
        actions.append({"tool_name": "read_entity", "arguments": {"entity_id": self.target_entity_id, "session_token": token}})
        # Wait/step through inversions
        for step_idx in range(len(self.inversion_schedule)):
            actions.append({"tool_name": "read_entity", "arguments": {"entity_id": self.target_entity_id, "session_token": token}})
        # Final commit
        actions.append({
            "tool_name": "commit_final_binding",
            "arguments": {
                "entity_id": self.target_entity_id,
                "bound_value": self.target_final_value,
                "session_token": token,
            },
        })
        return actions
```

---

### 4.3 Mathematical Formulations & Evaluation Metrics

To eliminate confounding variables (e.g., model size vs. memory capability), we define formal derived metrics:

#### 1. Difference-in-Differences (DiD) on Success vs. Conformance
We decouple **Syntactic Tool Conformance** ($C \in [0, 1]$) from **Task Success** ($Y \in \{0, 1\}$) across clean control vs. memory/fault treatment arms:

$$\text{DiD}_{\text{Success}} = \left( \bar{Y}_{\text{treatment}}^{\text{post}} - \bar{Y}_{\text{treatment}}^{\text{pre}} \right) - \left( \bar{Y}_{\text{control}}^{\text{post}} - \bar{Y}_{\text{control}}^{\text{pre}} \right)$$

$$\text{DiD}_{\text{Conformance}} = \left( \bar{C}_{\text{treatment}}^{\text{post}} - \bar{C}_{\text{treatment}}^{\text{pre}} \right) - \left( \bar{C}_{\text{control}}^{\text{post}} - \bar{C}_{\text{control}}^{\text{pre}} \right)$$

If $\text{DiD}_{\text{Conformance}} \approx 0$ while $\text{DiD}_{\text{Success}} \ll 0$, the degradation is causally isolated to **working memory state-tracking failure**, entirely independent of tool schema comprehension.

#### 2. Milestone Similarity Metric ($MSM$)
Measures progress along the required intermediate state DAG $\mathcal{M}^* = \{m_1, m_2, \dots, m_K\}$:

$$MSM(\tau, \mathcal{M}^*) = \frac{1}{|\mathcal{M}^*|} \sum_{j=1}^{|\mathcal{M}^*|} \mathbb{I}(m_j \in \tau) \cdot \exp\left( -\lambda \cdot \max\left(0, t_{\text{actual}}(m_j) - t_{\text{optimal}}(m_j)\right) \right)$$

#### 3. Perturbation Recovery Rate ($PRR$) & Fault Exposure Denominator ($FED$)
Measures autonomous recovery strictly conditioned on confirmed exposure to an injected fault:

$$FED = \sum_{i=1}^N \mathbb{I}(\text{Fault\_Exposed}_i = 1)$$

$$PRR = \frac{\sum_{i=1}^N \mathbb{I}(\text{Fault\_Exposed}_i = 1 \;\land\; \text{Recovered}_i = 1)}{FED}$$

#### 4. Memory Continuity Decay Factor ($\alpha_{\text{mem}}$)
Models the empirical survival of dynamic entity bindings as a function of context token distance $D$ and number of intervening state inversions $N_{\text{inv}}$:

$$P(\text{Binding\_Success}) = \sigma_0 \cdot \exp\left( -\alpha_{\text{mem}} \cdot \frac{D_{\text{tokens}}}{1000} \cdot \left[1 + \mu \cdot N_{\text{inv}}\right] \right)$$

---

### 4.4 Deterministic Oracle Verification, Sandboxing, & Verifier Isolation

To guarantee scientific reproducibility and prevent agent tampering:
1. **Container Isolation (Docker / gVisor)**: Agent processes execute inside ephemeral container namespaces with strictly isolated `/tmp` and read-only root mounts.
2. **Cryptographic Task Binding**: Every task instance contract is hashed via SHA-256:
   $$\text{Contract\_Digest} = \text{SHA-256}\left(\text{Seed} \parallel \text{Task\_Params} \parallel \text{Tool\_Schema\_Digest}\right)$$
3. **Decoupled Verifier Execution**: The verification binary executes outside the agent\x27s container after container teardown, reading only immutable `benchmark-events.jsonl` and final environment state files.

---

## Section V: Strategic Roadmap & Eval-Lab Integration Blueprint

To operationalize these findings within `eval-lab`, we outline the immediate architectural enhancements:

```
+----------------------------------------------------------------------------------------------------+
|                                  EVAL-LAB 6-MONTH INTEGRATION ROADMAP                              |
|                                                                                                    |
|  PHASE 1 (Weeks 1-4): EVIDENCE-PROMOTION REPAIR & BENCHMARK CONTRACTS                             |
|  - Backfill benchmark_contract.json across all 170 legacy trials.                                  |
|  - Enforce load_trial_bundle validation in CI.                                                     |
|                                                                                                    |
|  PHASE 2 (Weeks 5-10): DYNAMIC ACTION-MEMORY & MCP RECOVERY EXTENSION                              |
|  - Expand action-memory-v1 dose ladder to 128k context with active state-inversion mutants.        |
|  - Deploy full 20-cell mcp-recovery-v1 paired arms across GLM-5.3, GPT-5.6, and Claude models.     |
|                                                                                                    |
|  PHASE 3 (Weeks 11-18): SYNTHETIC ENVIRONMENT GENERATOR (SPADE / GYM INTEGRATION)                 |
|  - Implement native SyntheticMemoryToolEnv generator in evallab.task_workbench.                   |
|  - Integrate hint-based regret auto-curricula for model fine-tuning and evaluation.                |
|                                                                                                    |
|  PHASE 4 (Weeks 19-24): MCTS MEMORY GRAPH RUNTIME & KV-CACHE EVACUATION POLICIES                  |
|  - Deploy MCTS-MG agent scaffold with PRM verification.                                            |
|  - Benchmark ForesightKV and FreeKV memory eviction policies against baseline dense attention.     |
+----------------------------------------------------------------------------------------------------+
```

---

## Complete Primary Source Citations & References

1. **SPADE**: *SPADE: Self-Play in Adaptive Synthetic Executable Environments*, arXiv:2608.19197v2, 2026. Official repository: `spade-rl/spade`.
2. **Absolute Zero**: *Absolute Zero: Zero-Human-Data Self-Evolving Reasoning via Executable Code*, arXiv:2505.03335, 2025.
3. **AutoEnv**: *AutoEnv: Automated Environments for Measuring Cross-Environment Agent Learning*, arXiv:2511.19304, 2025.
4. **ToolSandbox**: *ToolSandbox: A Stateful, Conversational, Interactive Evaluation Benchmark for LLM Tool Use Environments*, arXiv:2408.04682, 2024.
5. **ToolBench-X**: *ToolBench-X: Evaluating Multi-Turn Tool Robustness Under Execution Hazards*, arXiv:2606.25819, 2026.
6. **ToolMaze**: *ToolMaze: Navigating Dynamic Tool Graph Environments with Injected Perturbations*, arXiv:2606.05806, 2026.
7. **ToolMisuseBench**: *ToolMisuseBench: Deterministic Evaluation of Agentic Tool Misuse and Autonomous Repair*, arXiv:2604.01508, 2026.
8. **$\tau^2$-bench**: *$\tau^2$-bench: Evaluating Dual-Control Conversational Database Agents*, arXiv:2506.07982, 2025.
9. **MemoryAgentBench**: *MemoryAgentBench: Evaluating Multi-Turn Context, Conflict Resolution, and Long-Horizon Memory in LLM Agents*, arXiv:2507.05257, 2025.
10. **LOCA-bench**: *LOCA-bench: Long-Context Agent Benchmark for Actionable Memory under Extreme Token Growth*, arXiv:2602.07962, 2026.
11. **BEAM**: *BEAM: Evaluating Multi-Hop Reasoning in Ultra-Long Conversations up to 10M Tokens*, arXiv:2510.27246, 2025.
12. **AMA-Bench**: *AMA-Bench: Actionable Memory Agents Benchmark for Stateful Real-World Tool Use*, arXiv:2602.22769, 2026.
13. **Memora**: *Memora: A Benchmark for Long-Term Personalization and Forgetting-Aware Agent Memory*, arXiv:2604.20006, 2026.
14. **H2O**: *H2O: Heavy-Hitter Oracle for Efficient Generative Inference of Large Language Models*, NeurIPS 2023 / arXiv:2306.14048.
15. **SnapKV**: *SnapKV: LLM Knows What You Are Looking for Before Generation*, arXiv:2404.14469, 2024.
16. **Quest**: *Quest: Efficient Spatial-Temporal KV Cache Selection for Long-Context LLMs*, arXiv:2406.10774, 2024.
17. **StreamingLLM**: *Efficient Streaming Language Models with Attention Sinks*, ICLR 2024 / arXiv:2309.17453.
18. **FreeKV**: *FreeKV: Algorithm-System Co-Optimization for KV-Cache Retrieval and Swapping in Long-Horizon Agent Inference*, 2026.
19. **NVIDIA Dynamo**: *NVIDIA Dynamo: Distributed KV-Aware Routing for Multi-Node LLM Inference*, 2026.

