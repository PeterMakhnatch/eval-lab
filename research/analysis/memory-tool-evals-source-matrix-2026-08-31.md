# Modern agent memory and tool-use evaluations — source/licence/code/data matrix

Answers `research/inbox/librarian-modern-memory-evals.md`. Base `93d2e7c1`.

**Verification method.** Every arXiv identifier was resolved against the arXiv Atom API
in this session: **39 resolved, 0 unresolved**. The report cites **36** of them; all 36
are within the resolved set, verified programmatically after writing. Every repository
fact — SPDX licence, head SHA, head date, and the first line of the actual `LICENSE`
file — came from the GitHub API, not from a README badge or a search snippet. Titles
below are the API-resolved titles, not search-result titles.

One correction that method caught: a search snippet titled `arXiv:2606.01435` *"Don't
Ask the LLM to Track Freshness"*, but the API resolves it to *"Reliable Post-Retrieval
Assembly for Agent Memory: Separating Evidence Extraction…"*. The resolved title is used.

**Layering.** Sections 1–4 are observed facts and source claims, labelled per row.
Section 5 is inference. Section 6 reads the `ContextOperationFact` contract off the
schema and classifies each source against it. Section 7 is proposed hypothesis. Nothing
in 1–4 depends on 5–7.

**Scope, per the five-lane contract review §68.** Source review only. This report
creates no adapters, task packages, registry records or schemas, and makes **no adoption
or program priority decisions** — §7 candidates are unranked and each is mapped to the
lane and contract that would own the decision. Sources that cannot activate
`ContextOperationFact` write/read/use semantics without instrumentation we would have to
add are flagged explicitly in §6.

---

## 0. Two findings that correct prior reports

Both were produced by reading the actual licence file rather than the SPDX summary.

1. **`apple/ToolSandbox` is not open-source-licensed.** GitHub reports
   `NOASSERTION`; the `LICENSE` file begins *"Copyright (C) 2024 Apple Inc. All Rights
   Reserved. IMPORTANT: This Apple software is supplied to you by Apple Inc. in
   consideration of your agreement to the following terms…"*. This is an Apple custom
   licence, not a standard permissive grant. **This is a licence fact bearing on
   `research/inbox/benchmark-themes-librarian-reply.md`, which had named ToolSandbox
   the T2 anchor.** That earlier report's disposition was written before the `LICENSE`
   body was read and needs revisiting by whichever lane owns T2; the milestone design
   itself is unaffected. This report records the licence, not a replacement disposition.
2. **`snap-research/locomo` is CC BY-NC 4.0.** The `LICENSE.txt` opens with
   *"Attribution-NonCommercial 4.0 International"*. Harbor already ships a `locomo`
   adapter, so any use must stay non-commercial, and redistribution of derived data
   inherits the NC term. Head is `3eb6f2c585f5` dated 2024-08-13 — unmaintained for two
   years.

---

## 1. Matrix — memory constructs

Columns follow the brief. `Traj.` is trajectory/artifact availability. `Oracle indep.`
is whether the scoring authority is separable from the generator or the agent.

| Source | Paper / repo / commit | Licence | Task unit + harness | Construct | Intervention vs comparator | Metric + denominator | Traj. | Oracle indep. | Lane / contract mapping · CoF activation | Evidence limitation |
|---|---|---|---|---|---|---|---|---|---|---|
| **LoCoMo** | `arXiv:2402.17753` *Evaluating Very Long-Term Conversational Memory of LLM Agents*; `snap-research/locomo` @ `3eb6f2c585f5` (2024-08-13) | **CC BY-NC 4.0** | Multi-session dialogue QA; **native Harbor adapter `adapters/locomo`** | Recall only, across sessions | None internal; comparator is model/memory-system swap | QA accuracy over annotated questions | Dialogues released; no agent trajectories | Gold answers ship with data — independent of any agent | Agent Data ingestion lane (`memory_continuity.py`); package/registry owned by Eval Runner. **CoF:no(structural)** — QA accuracy only; a chat turn is not a `memory_write` and a question is not a `memory_read`. | Recall-only. Answering a question is not using the fact as an action parameter. Two years stale. |
| **LongMemEval** | `arXiv:2410.10813`; `xiaowu0162/LongMemEval` @ `9e0b455f4ef0` (2026-05-11) | **MIT** | Long interactive chat sessions | Recall + temporal reasoning + knowledge update | Session-length and distractor scaling | Per-ability accuracy | Session logs | Gold answers released | Agent Data ingestion lane, same generic normalization path. **CoF:no(structural)** — per-ability accuracy; no operation event. | Chat-shaped; no tool surface, so no parameter binding. |
| **MemoryAgentBench** | `arXiv:2507.05257`; `HUST-AI-HYZ/MemoryAgentBench` @ `fe1735de8cf8` (2026-08-20) | **MIT** | Incremental multi-turn interaction | Four competencies incl. **conflict resolution** (source claim) | Incremental chunk feeding vs full context | Per-competency accuracy | Interaction sequences | Task-defined truth | Agent Data ingestion lane; conflict competency touches the Data Engineer stale-override construct. **CoF:no(structural)** — per-competency accuracy; conflict resolution is scored by answer, not by a logged update. | Conflict resolution is the load-bearing axis for us and is one competency among four; per-axis n unread. |
| **BEAM** | `arXiv:2510.27246` *Beyond a Million Tokens* | Paper (repo not verified here) | 100 conversations, 2,000 validated questions, up to 10M tokens (source claim) | Long-term memory at extreme length | Length scaling | Question accuracy | Conversations | Validated question set | Agent Data ingestion lane. **CoF:no(structural)** — question accuracy; length is the only manipulated variable. | Length is confounded with content unless padding is controlled; see LOCA. |
| **MemGym** | `arXiv:2605.20833` *MemGym: a Long-Horizon Memory Environment for LLM Agents* | Paper | Environment, not static QA | Long-horizon memory in an env | Env-native episodes | Env reward | Env rollouts | Env verifier | Agent Data ingestion lane. **CoF:undetermined** — env-native episodes could carry operation events; body unread. | Env fidelity and oracle strength unread at body level. |
| **MemoryArena** | `arXiv:2602.16313` *Benchmarking Agent Memory in Interdependent Multi-Session Agentic Tasks* | Paper | **Interdependent** multi-session agentic tasks | Cross-session action, not recall | Session interdependence | Task success | Not verified | Task verifier | Data Engineer lane; candidate for `SessionDependencyFact` only if the package exposes dependency edges as ground truth (review §22). **CoF:undetermined**. | "Interdependent" is exactly the cross-session-action construct; needs body read to confirm the dependency is genuine. |
| **STALE** | `arXiv:2605.06527` *STALE: Can LLM Agents Know When Their Memories Are No Longer Valid?* | Paper | Probing framework | **Stale-state detection**; three-dimensional probe incl. State Resolution (source claim) | Validity-invalidating updates | Per-dimension probe scores | Not verified | Probe-defined | Data Engineer lane, stale-override construct (`action_memory.py`, `PairedConditionFact`). **CoF:undetermined** — probe scores may not expose the update as an operation. | Closest published match to our stale-override construct; body unread. |
| **Supersede** | `arXiv:2606.27472` *Supersede: Diagnosing and Training the Memory-Update Gap in LLM Agents* | Paper | Multi-session, **bounded memory** (notes capped at B chars), sessions not re-fed (source claim) | **State inversion / supersession** under a hard budget | Value superseded mid-interaction vs not | Whether superseded value is remembered or forgotten | Not verified | Final-query truth | Data Engineer lane, state-inversion construct; bounded-notes cap is the same forcing device as the LOCA padding control on hold at `39022d6`. **CoF:undetermined** — a hard cap must evict, so this is the likeliest source of a typed `evict`/`memory_write` event. | The bounded-notes design is a genuine forced-forgetting control, which most benchmarks lack. |
| **TEPA** | `arXiv:2608.07429` *TEPA: Revoking Stale Memories for Conflict-Robust Language Agents* | Paper | Unified suite spanning memory state and tool outcomes (source claim) | Stale revocation + drift | Conflict injection | Memory-state and tool-outcome metrics | Not verified | Suite-defined | Data Engineer lane, stale-override construct. **CoF:undetermined** — method paper; no benchmark package verified. | Method paper with an evaluation attached; not a clean benchmark package. |
| **Memora** | `arXiv:2604.20006` *From Recall to Forgetting* | Paper | Weeks-to-months personalised sessions | Remember / reason / recommend, **forgetting-aware** | Evolving knowledge over time | Forgetting-aware memory metric (source claim) | Not verified | Automated grounding + human eval | Agent Data ingestion lane. **CoF:no(structural)** — forgetting-aware aggregate metric over sessions. | Personalisation domain is off our axis; the forgetting-aware metric is the transferable part. |
| **LOCA-bench** | `arXiv:2602.07962` *Benchmarking Language Agents Under Controllable and Extreme Context Growth* | Paper | Controllable growth | Context growth as a **manipulated variable** | Growth level is the intervention | Task success vs growth | Not verified | Task verifier | Data Engineer lane; context-growth control, already on hold at `39022d6` for a padding confound. **CoF:no(structural)** — task success vs growth level. | Our own readiness audit already flagged a padding confound; see §4 duplication. |
| **ContextBench** | `arXiv:2602.05892` *A Benchmark for Context Retrieval in Coding Agents* | Paper | Coding-agent context retrieval | Retrieval infrastructure, not memory | Retrieval variants | Retrieval accuracy | Not verified | Task verifier | No memory lane. **CoF:no(construct)** — measures retrieval infrastructure; `RetrievalFact` is the contract it would touch, not `ContextOperationFact`. | Measures retrieval plumbing; keep out of the memory theme to avoid construct drift. |
| **AMA-Bench** | `arXiv:2602.22769` *Evaluating Long-Horizon Memory for Agentic Applications* | Paper | Agentic applications | Long-horizon memory | Horizon scaling | Not verified | Not verified | Not verified | Agent Data ingestion lane. **CoF:no(structural)** — nothing verified beyond identity; horizon scaling only. | Nothing verified beyond identity. |
| **Memory substrate harness** | `arXiv:2608.15008` *Harness the Memory* | Paper | Meta-harness over LoCoMo, MemoryAgentBench and others (source claim) | Substrate comparison | Substrate swap | Cross-benchmark aggregate | n/a | Inherits each benchmark | No lane; multi-benchmark harness precedent. **CoF:no(structural)** — cross-benchmark aggregate. | Not a benchmark. Valuable as a design precedent for our own multi-benchmark harness. |
| **Modular memory survey** | `arXiv:2604.01707` *Memory in the LLM Era* | Paper | Survey | Reports token cost, retrieval latency, context scalability, **position sensitivity**, backbone dependence (source claim) | n/a | n/a | n/a | n/a | Data Engineer feature lane would own any position-sensitivity feature. **n/a** — survey, not a benchmark. | Position sensitivity is a measurable we do not currently compute. |
| **Agent-native memory** | `arXiv:2606.24775` *Are We Ready For An Agent-Native Memory System?* | Paper | Three end-to-end workloads incl. LoCoMo (source claim) | End-to-end memory-system effect | Memory system swap | End-to-end task success | n/a | Inherits workloads | No lane. **CoF:no(structural)** — end-to-end task success, inherits its workloads. | Position paper shape. |
| **agent-memory-eval** | `verifiedstate/agent-memory-eval` @ `6c82208f7638` (2026-04-03) | **NONE DECLARED — no LICENSE file** | 50 fixtures, composite scoring (source claim) | Temporal state, provenance, abstention, conflict | Fixture-based | Composite score across dimensions | Fixtures | Fixture-defined | Data Engineer lane if ever licensed. **CoF:undetermined** — fixture composite score; unlicensed, so unreadable as a package. | Unlicensed code and fixtures cannot be vendored. Abstention-plus-provenance dimension is interesting; reimplement clean if wanted. |

## 2. Matrix — memory × tool interaction

This is the brief's question 6 and the most consequential section: several sources
measure memory and tool use **jointly within one task**, not in separate arms.

| Source | Paper / repo / commit | Licence | Task unit | Construct | Intervention vs comparator | Metric + denominator | Traj. | Oracle indep. | Lane / contract mapping · CoF activation | Evidence limitation |
|---|---|---|---|---|---|---|---|---|---|---|
| **Mem2ActBench** | `arXiv:2601.19935` *A Benchmark for Evaluating Long-Term Memory Utilization in Task-Oriented…*; `Cantaloupe-M/Mem2ActBench` @ `b00726940b5a` (2026-01-13) | **NONE DECLARED — no LICENSE file** | Multi-turn conversation → tool call | **Memory-driven tool calling / memory-grounded parameter binding**, evaluated in a single task not separate arms (source claim) | Underspecified request requiring a remembered value | Memory-grounded verification with exact and soft matching; strict schema validation (source claim) | Pipeline released | Schema + memory-grounded verifier, separable from the agent | Data Engineer lane, memory-grounded parameter binding. **CoF:undetermined** — verification is on the tool argument, which is `RetrievalFact.utilized_status` / `SessionDependencyFact.observed_memory_reference` shaped, not a `ContextOperationFact` operation. | Unlicensed. Head is 2026-01-13, thin activity. Body unread, so metric denominators unconfirmed. |
| **Entity Binding Failures** | `arXiv:2606.30531` *Entity Binding Failures in Tool-Augmented Agents*; `R-Suresh/EntityBindingFailures` @ `af311d10f526` (2026-06-30) | **MIT** | 60 diagnostic tasks, 5 enterprise domains: email, calendar, documents, customer records, issue tracking (source claim) | **Right-tool / wrong-entity** — tool-parameter binding as a first-class failure mode | Ambiguous, underspecified, or confusable target entity; 6 tool-use methods × 5 backends | Binding correctness on the real-world target entity | Diagnostic tasks released | Entity truth is task-defined | Data Engineer lane, binding construct. **CoF:no(structural)** — binding correctness is an argument-level verdict; no memory operation is logged. Fits `PairedConditionFact` and `RetrievalFact` instead. | Diagnostic scale is small (60 tasks) and enterprise-shaped; a source page reported "0 diagnostic tasks" in one summary, so the count needs a body check. |
| **When Does Memory Help…** | `arXiv:2605.28224` *When Does Memory Help Multi-Trajectory Inference for Tool-Use LLM Agents?* | Paper | Multi-trajectory inference | **The interaction question stated directly** | Memory on/off across trajectories | Not verified | Not verified | Not verified | Data Engineer lane, interaction question. **CoF:undetermined** — nothing beyond identity verified. | Title promises exactly the conditional we need; nothing beyond identity is verified. |
| **MemToolAgent** | `arXiv:2606.07909` | Paper | Memory-augmented tool use | Joint memory retrieval + reflection + tool execution in one workflow (source claim) | Memory entries from past tool trajectories + user feedback | Tool accuracy plus memory-informed measures (source claim) | Not verified | Not verified | No lane; method, not a controlled evaluation. **CoF:undetermined**. | It is a *method* that improves tool use with memory, not a controlled evaluation of the interaction. |
| **MemTool** | `arXiv:2507.21428` *Optimizing Short-Term Memory Management for Dynamic Tool Calling* | Paper | Multi-turn conversations | Short-term context management for tool calling; joint, not separate arms (source claim) | Removal/search tool policies | Removal Ratio, Avg Residual 3T (source claim) | Not verified | Not verified | Data Engineer lane; short-term context hygiene overlaps our compaction facts. **CoF:undetermined** — removal-ratio metrics imply a removal event, which is `evict`-shaped; body unread. | Measures context hygiene during tool calling, adjacent to but not identical with long-term memory. |
| **H-EPM** | `arXiv:2512.07287` *Experience-Evolving Multi-Turn Tool-Use Agent with Hybrid Episodic-Procedural Memory* | Paper | Multi-turn tool use | State-annotated **tool-transition graph** as memory (source claim) | Episodic + procedural memory vs neither | Not verified | Not verified | Not verified | Data Engineer lane; tool-transition graph maps onto existing `trajectory_sequence` edges. **CoF:undetermined**. | The tool-transition-graph representation maps onto our existing `trajectory_sequence` edges. |
| **ToolMem** | `arXiv:2510.06664` *Enhancing Multimodal Agents with Learnable Tool Capability Memory* | Paper | Multimodal tool selection | Memory **of tools**, not memory used **by** tools | Capability memory vs none | Tool-proficiency tiers (source claim) | Not verified | Not verified | No lane. **CoF:no(construct)** — remembers tool quality, inverting the construct. | Inverts the construct: it remembers tool quality rather than binding remembered facts into calls. |
| **ReMe / ToolMemory** | `agentscope-ai/ReMe` @ `65cb4ebdd643` (2026-08-31) | **Apache-2.0** | Framework component | Tool usage/performance memory | n/a | Separate components, no single joint metric (source claim) | n/a | n/a | No lane; framework, not an evaluation. **CoF:no(construct)**. | Actively maintained and permissively licensed, but a framework, not a benchmark. |

## 3. Matrix — tool-side and trajectory→task generation

Included only where they contribute a construct the memory sources do not.

| Source | Paper / repo / commit | Licence | Construct | Metric + denominator | Oracle indep. | Lane / contract mapping · CoF activation | Evidence limitation |
|---|---|---|---|---|---|---|---|
| **ToolSandbox** | `arXiv:2408.04682`; `apple/ToolSandbox` @ `165848b9a78c` (2025-11-07) | **Apple custom, all rights reserved** | Stateful tool use; milestone DAG + minefields | Milestone similarity along a required-state DAG (source claim) | Milestones are task-defined, independent of the agent | Researcher lane, milestone ground truth. **CoF:no(construct)** — per-step state milestones, not memory operations. Licence unresolved. | Best available per-step ground truth in the tool space, but not redistributable under a standard grant. |
| **ToolMaze** | `arXiv:2606.05806` *When Tools Fail* | Paper | Fault recovery on a fault-exposure denominator | **`PRR = Σ I_recov·I_pert / Σ I_pert`** — body-quoted in prior work | Deterministic verifier | Researcher lane, recovery denominator; PRR is body-quoted. **CoF:no(construct)** — fault recovery, not memory. | Companion Recovery Cost normalises by ` | T_m | `, so RC is *not* exposure-conditioned. |
| **AgentCheck** | `arXiv:2607.11098`; `aritra741/AgentCheck` @ `2b89d2c5782f` (2026-07-11) | **MIT** | Clean-run/faulted-run pair over a cached MCP prefix; **memory-recovery interaction via the response cache** | First-divergence comparison | *"holds every tool response constant except one, so the divergence is attributable to the injected fault"* — body-quoted | Researcher lane (`mcp_recovery.py`); clean-twin arm is body-quoted. **CoF:no(construct)** — the response cache is retained state but the source logs no memory operation over it. | Bundled 120 scenarios include source-derived cases whose upstream terms are not preserved; engine is clean, scenarios are not. |
| **ToolMisuseBench** | `arXiv:2604.01508` | Paper | Offline deterministic misuse + recovery | Not verified | Deterministic | Researcher lane. **CoF:no(construct)**. | Cheapest tool-side entry; offline and deterministic. |
| **FuncBenchGen** | `arXiv:2509.26553`; `megagonlabs/FuncBenchGen` @ `0729e2567dfa` (2026-02-10) | **BSD-3-Clause** | Hidden typed function DAG; **intermediate value propagation** = synthetic parameter binding | Exact target oracle | Generator and oracle are both deterministic and separable | Researcher lane (`mcp_funcdag.py`); value propagation is synthetic parameter binding. **CoF:no(construct)** — deterministic DAG oracle, no memory operation. | Topology/identifier leakage and stale-value shortcuts require partitions and mutants. |
| **FACET** | `arXiv:2608.18580` *Preserving Source Intent and Executable State in Terminal Task Synthesis* | Paper | Generates instruction, solution **and verifier**, grounding all three in the same realised env state; compares generation orders including Forward I→S→V (source claim) | n/a | **Directly addresses generator/oracle independence** — the brief's question 4 | Synthetic Data lane; generation-order result bears on the funnel materialiser. **n/a** — task synthesis, not memory. | Highest-value unread paper for our synthetic funnel; ordering result could change our materialiser design. |
| **Anchor** | `arXiv:2605.26321` *Mitigating Artifact Drift in Agent Benchmark Generation* | Paper | Artifact drift in generated benchmarks | Multi-objective reward with per-task checks (source claim) | Verifier framework | Synthetic Data lane, artifact drift. **n/a**. | Drift is the failure mode our task-digest pinning already guards. |
| **TRACE** | `arXiv:2510.00415` *Towards Self-Evolving Benchmarks: Synthesizing Agent Trajectories via Test-…* | Paper | Evolution proposer → exploration executor → validatable trajectory audit (source claim) | n/a | Trajectory audited, not self-scored | Synthetic Data lane, trajectory audit. **n/a** — distinct from the capability-training TRACE in our corpus; do not conflate. | Note: distinct from the capability-training TRACE cited elsewhere in our corpus; do not conflate. |
| **BenchAgents** | `arXiv:2410.22584` *Multi-Agent Systems for Structured Benchmark Creation* | Paper | Planning/Generation/Verification/Evaluation agent split | n/a | Verification agent is a separate role | Synthetic Data lane, role separation. **n/a** — a separate agent is not an independent oracle. | Role separation is organisational, not cryptographic; a separate agent is not an independent oracle. |
| **Self-Challenging (CaT)** | `arXiv:2506.01716` | Paper | Challenger generates task + verification function; executor solves | n/a | **Same model can be both** — weak independence | Synthetic Data lane as a negative control on oracle independence. **n/a** — same model may author task and verifier. | Self-generated verifier is precisely the circularity our funnel gates prohibit. |
| **TASTE** | `arXiv:2605.28556`; `tomerkeren42/TASTE-…` @ `d53da23956d6` (2026-05-31) | **NOASSERTION, `LICENSE` reads "Copyright (c) 2026"** | Tool-sequence evolution → task synthesis | n/a | LLM filter and GT agent are not an independent oracle | Synthetic Data lane, cleanroom method only. **n/a** — all rights reserved. | Confirms prior finding: no redistribution or derivatives. |
| **SPADE** | `arXiv:2608.19197` *Self-Play in Adaptive Synthetic Executable Environments*; `spade-rl/spade` @ `ebd40ec872fc` (2026-08-26) | **MIT** | Designer/agent self-play in executable envs | n/a | Runtime validation | Synthetic Data lane, training method. **n/a** — memory ablation concerns the designer's task buffer, not agent memory; no interaction claim is carried here. | **Boundary honoured:** its memory ablation concerns the *designer's* historical regret/task buffer in games, and its tool-use gains are a separate self-play experiment. Nothing here supports agent-memory→tool-use causality, and this report makes no such claim. |
| **tau2-bench** | `arXiv:2506.07982`; `sierra-research/tau2-bench` @ `a2c024725189` (2026-08-18) | **MIT** | Dual-control env; DB + COMMUNICATE assertions | Task × trial | State oracle independent of agent | Researcher lane substrate; Harbor pins `tau3-bench`. **n/a**. | Harbor ships `tau3-bench`, not `tau2`. |
| **recovery-bench** | `letta-ai/recovery-bench` @ `c5f83f2ba4f8` (2026-04-20) | **NONE — licence endpoint 404, no root LICENSE** | Inherited-failure-state recovery | Selection conditioned on `reward == 0` | **No clean-twin arm** (source read in prior work) | Researcher lane as a negative control. **n/a** — selection on `reward == 0` with no clean twin is a structural confound. | Structural confound, not a reporting gap. |

---

## 4. Answers to the six questions

**Q1 — recall only vs write/read/use, cross-session action, stale override, state
inversion, tool-parameter binding.**

| Construct | Best-evidenced sources |
|---|---|
| Recall only | LoCoMo, BEAM, LongMemEval (partly) |
| Write/read/use across sessions | MemoryArena (interdependent multi-session), MemGym |
| Stale-state override | **STALE**, **TEPA**, MemoryAgentBench conflict-resolution competency |
| State inversion / supersession under budget | **Supersede** — bounded notes, sessions not re-fed |
| Tool-parameter binding | **Mem2ActBench** (memory-grounded), **Entity Binding Failures** (entity-level), **FuncBenchGen** (synthetic value propagation) |

**Q2 — real trajectories, explicit memory operations, task truth, denominators,
reproducible packages.** Only three sources clear all of licence, released code, and a
separable oracle: **LongMemEval** (MIT), **MemoryAgentBench** (MIT), **Entity Binding
Failures** (MIT). FuncBenchGen (BSD-3) clears them for the synthetic arm. Everything
else fails on licence (LoCoMo NC, ToolSandbox Apple, Mem2ActBench and agent-memory-eval
unlicensed, TASTE all-rights-reserved) or on unverified artifact availability.

Explicit *memory operations* — a log of write/update/evict rather than inferred state —
are not confirmed released by any source I verified. That is a gap, not a finding.

**Q3 — interventions that isolate memory from context length, reasoning, tool
familiarity, or retrieval infrastructure.**
- From context length: **LOCA-bench** makes growth the manipulated variable;
  **Supersede** caps memory to B characters and stops re-feeding sessions, which forces
  memory use rather than re-reading.
- From general reasoning: **STALE**'s per-dimension probes separate detection from
  downstream action (source claim).
- From tool familiarity: **Entity Binding Failures** holds the tool constant and varies
  only entity ambiguity — right tool, wrong entity.
- From retrieval infrastructure: nothing clean. **ContextBench** measures the
  infrastructure itself, which is why it is excluded from the memory theme.

**Q4 — trajectory→task generation with independent oracle ownership.** **FACET** is the
most directly relevant unread source: it generates instruction, solution and verifier
grounded in one realised environment state and compares generation orders. **TRACE**
audits a validatable trajectory rather than self-scoring. **BenchAgents** separates a
verification agent by role only, which is organisational rather than structural.
**Self-Challenging (CaT)** permits the same model to author task and verifier and is
therefore excluded. **TASTE** and **AgentCheck** both keep a deterministic check outside
the generator, but TASTE's licence blocks reuse.

**Q5 — licences, commits, files, versions constraining adoption.**

| Blocker | Sources | Effect |
|---|---|---|
| No licence at all | Mem2ActBench, agent-memory-eval, recovery-bench | Cannot vendor. Reimplement clean or exclude. |
| Non-commercial | LoCoMo (CC BY-NC 4.0) | Usable non-commercially; derived data inherits NC. |
| Vendor custom | ToolSandbox (Apple) | Needs Peter's read before any adoption. |
| All rights reserved | TASTE | Cleanroom method only. |
| Clean permissive | LongMemEval, MemoryAgentBench, EntityBindingFailures, FuncBenchGen, AgentCheck engine, tau2-bench, SPADE, ReMe | No licence blocker. Construct fit is judged per lane, not here. |

**Q6 — empirical memory×tool interaction vs separate arms.** Three sources claim joint
measurement in a single task: **Mem2ActBench** (memory-grounded tool-call generation),
**MemToolAgent** (retrieval + reflection + execution in one workflow), and **MemTool**
(context management measured through tool-calling metrics). **`arXiv:2605.28224`** poses
the conditional directly in its title. **Entity Binding Failures** is joint by
construction, since the binding target *is* the tool argument.

Sources that keep them in separate arms, and must not be cited for interaction:
**SPADE** — its memory ablation is about the designer's task buffer, its tool-use gains
are a different experiment — plus **ToolMem**, which inverts the construct by
remembering tool quality rather than using memory inside a call.

---

## 5. Inference

Labelled inference, not observation.

- The field has moved from *recall* to *use* within roughly the last two quarters.
  Mem2ActBench, Entity Binding Failures, Supersede and STALE all post-date the LoCoMo
  generation and all measure whether a remembered value changes an action. Eval Lab's
  memory theme should be built on that newer axis, not on QA accuracy.
- **Entity Binding Failures is the cleanest available separation of memory-use from
  tool-familiarity**, because it isolates parameter binding while holding the tool
  fixed. Whether that separation is worth acquiring is a lane decision, not this
  report's.
- The absence of released memory-operation logs across every source I checked suggests
  our `state-journal` plus ATIF token/compaction facts may already give us a
  finer-grained memory-operation record than the benchmarks we would adopt.
- Licence quality correlates poorly with construct quality here. The two best-designed
  artifacts, ToolSandbox and Mem2ActBench, are the two least usable.

## 6. `ContextOperationFact` activation

Required by the five-lane review §68. The test is read off the schema in
`src/evallab/semantic_facts.py`, not invented here.

`ContextOperationFact` carries `trial_id`, `operation_id`,
`operation ∈ {compaction, clear, evict, memory_read, memory_write}`, four size/token
fields, and an optional `content_digest` constrained to `sha256:<64 hex>`. `FactRow`
adds `source_ref`, `source_digest`, `provenance_kind`.

A source activates the contract only if it emits, natively:

| Prong | Contract field | What the source must expose |
|---|---|---|
| A1 discrete operation | `operation_id` (min_length 1) | An addressable event per memory operation, not an episode aggregate |
| A2 typed kind | `operation` literal | The event maps to one of the five enum members |
| A3 content identity | `content_digest` | The written or read payload is addressable, so a digest is computable |
| A4 step order | **no field exists** | See the shared-contract delta below |

### Two structural findings about the contract itself

**1. `operation` has no `memory_use` member.** Verified mechanically: the literal is
exactly `compaction, clear, evict, memory_read, memory_write`. The write and read legs
of write/read/use are expressible; **the use leg is not a `ContextOperationFact` at
all.** It routes to either `RetrievalFact.utilized_status` or
`SessionDependencyFact.observed_memory_reference`.

This matters because `RetrievalFact` already refuses the inference the review §20
prohibits. Its validator raises on `utilized_status` without `cited_evidence_ref`, and
again without an exposed document, file, block or line ID. So "the answer overlapped the
remembered fact" cannot populate the use leg — the schema blocks it mechanically, ahead
of any reviewer. §22's restriction on `SessionDependencyFact` closes the other route.

**2. `ContextOperationFact` carries no step-order field.** The review §20 requires
emission only from instrumentation with "content identity **and step order**". Content
identity has a field. Step order does not: no `step`, `start_step`, `end_step`, `index`
or ordinal appears on the row. Step order exists on `CapabilityOpportunity`
(`start_step`/`end_step`) and `ProcessStepFact` (`source_step_id`), not here.

Reported as a proposed shared-contract delta per §30 and §90, **not** edited:
`semantic_facts.py` is on the shared-mutation list and needs one integration owner.

- **Missing invariant:** a `ContextOperationFact` cannot currently be ordered within its
  trial, so write-then-supersede-then-read sequencing is unverifiable from the row set.
  Two operations on the same `trial_id` are unordered.
- **Call sites:** `interpretation/trajectory_context.py` lines ~1062 and ~1076 construct
  from `source="ContextOperationFact"`; `SEMANTIC_FACT_SCHEMAS["context_operation_facts"]`
  and `NormalizedFactBundle.context_operation_facts` carry the table.
- **Not proposed here:** the field name, type, or whether ordering should instead be
  derived from an existing join. That is the integration owner's call.

### Per-source activation

Verdicts are in the mapped column of §1–§3. Summarised:

| Verdict | Sources | Meaning |
|---|---|---|
| **CoF:no (structural)** | LoCoMo, LongMemEval, MemoryAgentBench, BEAM, Memora, LOCA-bench, AMA-Bench, Agent-native memory, Harness-the-Memory, Entity Binding Failures | Recall-only or aggregate-scored. Even with full artifacts released, no per-operation event exists to emit. **These cannot activate `ContextOperationFact` or write/read/use metrics without instrumentation we would have to add ourselves.** |
| **CoF:no (construct)** | ContextBench, ToolMem, ReMe, ToolSandbox, ToolMaze, AgentCheck, ToolMisuseBench, FuncBenchGen | Measures something other than agent memory operations. |
| **CoF:undetermined** | STALE, Supersede, TEPA, MemoryArena, MemGym, Mem2ActBench, MemTool, H-EPM, When-Does-Memory-Help, agent-memory-eval | Body unread. `IDENTITY-VERIFIED, METHOD-UNQUOTED`. Activation is unknown, not denied. |

**The honest first result for every source in the first two rows is typed
unavailable / zero opportunities**, per review §20: `EvidenceCoverage(exposed=False)`
plus `CapabilityOpportunity.missing_evidence`. Task outcome and trajectory volume may
still be analysis-ready for those sources; the memory-operation record is not.

Note what this does to the headline count: of 39 verified sources, **zero** are
confirmed to activate the contract, and ten are merely undetermined pending a body
read. No source is confirmed to emit an explicit memory operation.

## 7. Source-backed candidates

Unranked, per review §68. Each is a proposed hypothesis mapped to the lane and contract
that would own it. None is a result, and none is an adoption, packaging or priority
decision — those belong to the named lane.

| Candidate | Source basis | Owning lane / contract | CoF activation | Duplication |
|---|---|---|---|---|
| **Memory-bound tool-parameter family.** Fact written in session 1, superseded in session 2, required as a tool argument in session 3. Score the argument, not the prose. | Mem2ActBench + Entity Binding Failures + Supersede | Data Engineer lane; `action_memory.py`, `PairedConditionFact`, `CampaignAnalysisCell` | Would require our own instrumentation; no source supplies it | No lane owns memory×tool jointly. Not the AgentAbstain single-delta work, which is restraint. |
| **Entity Binding Failures as a licensed binding source** (MIT, `af311d10f526`). | `arXiv:2606.30531` | Data Engineer lane | **CoF:no(structural)** — argument-level verdict, no operation log | Verify the 60-task count against the body; one summary reported 0. |
| **Stale-override arm on existing sessions**: inject a contradicting update, measure which value reaches the tool call. | STALE + TEPA + MemoryAgentBench conflict axis | Data Engineer lane; scored from ATIF arguments, no judge | Our own arm could emit typed operations; the sources do not | Overlaps MemoryAgentBench's conflict competency — take the task shape rather than inventing one. |
| **Bounded-memory forcing control.** Cap the notes budget, stop re-feeding sessions, so recall cannot be replaced by re-reading. | `arXiv:2606.27472` | Data Engineer lane | A hard cap forces eviction, so this is the likeliest route to a genuine typed `evict` event | **The fix for the LOCA padding confound on hold at `39022d6`**, not a parallel effort. Review §62 gates concrete lineage on upstream digests. |
| **FuncBenchGen synthetic binding arm** (BSD-3) as a known-answer control beside real-memory tasks. | `arXiv:2509.26553` | Researcher lane; `mcp_funcdag.py`, `CampaignDefinition`/`CampaignManifest` | **CoF:no(construct)** — deterministic DAG oracle | An adapter already exists in prior work; reuse rather than duplicate. |
| **FACET read before any trajectory→task materialiser is finalised.** | `arXiv:2608.18580` | Synthetic Data lane | n/a | Gates synthetic funnel families A and C. |
| **Memory-recovery bridge via AgentCheck's cache** — the cache is retained state, so the clean/faulted pair is a memory-adjacent intervention. | `arXiv:2607.11098`, MIT engine, arm construction body-quoted | Researcher lane; `mcp_recovery.py` | **CoF:no(construct)** — no operation logged over the cache | Engine only. The 120 bundled scenarios have unpreserved upstream terms. |
| **PRR as the exposure-conditioned recovery estimator**, with RC marked non-exposure-conditioned. | `arXiv:2606.05806`, body-quoted | Researcher lane | n/a | Already established in prior work; carried here for completeness, not as new scope. |
| **Position-sensitivity feature**: where in the assembled context the probed fact sat. | `arXiv:2604.01707` | Data Engineer feature lane; computable from our own packs | Independent of CoF; a pack-derived feature | New. Not this lane's to build. |
| **LongMemEval as a licensed recall source** (MIT) where LoCoMo's NC terms are awkward. | `arXiv:2410.10813` | Agent Data ingestion lane | **CoF:no(structural)** | Harbor's `locomo` adapter exists and stays; this is a licence observation, not a replacement decision. |

### Duplication warnings against work underway

- **`benchmark-themes-librarian-reply.md` named ToolSandbox as the T2 anchor for
  adoption. That is now wrong on licence grounds** and must be amended to
  inspiration-only. This report supersedes it on that point.
- **Harbor `adapters/locomo` already exists.** Any memory lane must start from it rather
  than adding a second conversational-memory adapter, and must respect CC BY-NC.
- **`autonomous-research-v1` (74 features) covers T1 only.** No registered producer
  family covers memory or tool constructs, so the memory×tool, stale-override and
  bounded-memory candidates each need a producer before they can yield comparable
  features.
- **LOCA readiness is on HOLD at `39022d6`** for a padding confound. The bounded-memory
  forcing candidate is the fix for that confound, not a parallel effort.
- **AgentAbstain single-delta admission is restraint, not memory.** Different construct;
  do not merge.

## 8. Gaps and blockers

**Blockers.** Mem2ActBench and `agent-memory-eval` are unlicensed. ToolSandbox needs
Peter's licence decision. LoCoMo is NC. TASTE is all-rights-reserved.

**Gaps.** No verified source releases explicit memory-operation logs, so **no source is
confirmed to activate `ContextOperationFact`** and ten are undetermined pending a body
read (§6). Body-level reads are missing for STALE, Supersede, Mem2ActBench and FACET, so
every construct claim about them in §1–§3 is `IDENTITY-VERIFIED, METHOD-UNQUOTED` and
must not be treated as method evidence. Only ToolMaze's PRR and AgentCheck's arm
construction carry verbatim body quotes, both from prior work.

**Proposed shared-contract delta.** `ContextOperationFact` carries no step-order field
while review §20 requires content identity *and* step order for emission. Reported in §6
for a single integration owner per §30 and §90; `semantic_facts.py` is not edited here.
