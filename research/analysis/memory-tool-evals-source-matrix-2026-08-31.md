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
Section 5 is inference. Section 6 is proposed hypothesis. Nothing in 1–4 depends on 5–6.

---

## 0. Two findings that change prior recommendations

Both were produced by reading the actual licence file rather than the SPDX summary.

1. **`apple/ToolSandbox` is not open-source-licensed.** GitHub reports
   `NOASSERTION`; the `LICENSE` file begins *"Copyright (C) 2024 Apple Inc. All Rights
   Reserved. IMPORTANT: This Apple software is supplied to you by Apple Inc. in
   consideration of your agreement to the following terms…"*. This is an Apple custom
   licence, not a standard permissive grant. **This corrects
   `research/inbox/benchmark-themes-librarian-reply.md`, which named ToolSandbox as the
   T2 anchor for direct adoption.** Its milestone design remains the best available and
   is still worth borrowing, but the recommendation must drop from *adopt* to
   *inspiration-only pending a licence read by Peter*.
2. **`snap-research/locomo` is CC BY-NC 4.0.** The `LICENSE.txt` opens with
   *"Attribution-NonCommercial 4.0 International"*. Harbor already ships a `locomo`
   adapter, so any use must stay non-commercial, and redistribution of derived data
   inherits the NC term. Head is `3eb6f2c585f5` dated 2024-08-13 — unmaintained for two
   years.

---

## 1. Matrix — memory constructs

Columns follow the brief. `Traj.` is trajectory/artifact availability. `Oracle indep.`
is whether the scoring authority is separable from the generator or the agent.

| Source | Paper / repo / commit | Licence | Task unit + harness | Construct | Intervention vs comparator | Metric + denominator | Traj. | Oracle indep. | Recommendation | Evidence limitation |
|---|---|---|---|---|---|---|---|---|---|---|
| **LoCoMo** | `arXiv:2402.17753` *Evaluating Very Long-Term Conversational Memory of LLM Agents*; `snap-research/locomo` @ `3eb6f2c585f5` (2024-08-13) | **CC BY-NC 4.0** | Multi-session dialogue QA; **native Harbor adapter `adapters/locomo`** | Recall only, across sessions | None internal; comparator is model/memory-system swap | QA accuracy over annotated questions | Dialogues released; no agent trajectories | Gold answers ship with data — independent of any agent | **Adapter, NC-constrained** | Recall-only. Answering a question is not using the fact as an action parameter. Two years stale. |
| **LongMemEval** | `arXiv:2410.10813`; `xiaowu0162/LongMemEval` @ `9e0b455f4ef0` (2026-05-11) | **MIT** | Long interactive chat sessions | Recall + temporal reasoning + knowledge update | Session-length and distractor scaling | Per-ability accuracy | Session logs | Gold answers released | **Direct adoption candidate** | Chat-shaped; no tool surface, so no parameter binding. |
| **MemoryAgentBench** | `arXiv:2507.05257`; `HUST-AI-HYZ/MemoryAgentBench` @ `fe1735de8cf8` (2026-08-20) | **MIT** | Incremental multi-turn interaction | Four competencies incl. **conflict resolution** (source claim) | Incremental chunk feeding vs full context | Per-competency accuracy | Interaction sequences | Task-defined truth | **Direct adoption candidate** | Conflict resolution is the load-bearing axis for us and is one competency among four; per-axis n unread. |
| **BEAM** | `arXiv:2510.27246` *Beyond a Million Tokens* | Paper (repo not verified here) | 100 conversations, 2,000 validated questions, up to 10M tokens (source claim) | Long-term memory at extreme length | Length scaling | Question accuracy | Conversations | Validated question set | **Adapter** | Length is confounded with content unless padding is controlled; see LOCA. |
| **MemGym** | `arXiv:2605.20833` *MemGym: a Long-Horizon Memory Environment for LLM Agents* | Paper | Environment, not static QA | Long-horizon memory in an env | Env-native episodes | Env reward | Env rollouts | Env verifier | **Adapter** | Env fidelity and oracle strength unread at body level. |
| **MemoryArena** | `arXiv:2602.16313` *Benchmarking Agent Memory in Interdependent Multi-Session Agentic Tasks* | Paper | **Interdependent** multi-session agentic tasks | Cross-session action, not recall | Session interdependence | Task success | Not verified | Task verifier | **Adapter — high priority** | "Interdependent" is exactly the cross-session-action construct; needs body read to confirm the dependency is genuine. |
| **STALE** | `arXiv:2605.06527` *STALE: Can LLM Agents Know When Their Memories Are No Longer Valid?* | Paper | Probing framework | **Stale-state detection**; three-dimensional probe incl. State Resolution (source claim) | Validity-invalidating updates | Per-dimension probe scores | Not verified | Probe-defined | **Adapter — highest memory priority** | Closest published match to our stale-override construct; body unread. |
| **Supersede** | `arXiv:2606.27472` *Supersede: Diagnosing and Training the Memory-Update Gap in LLM Agents* | Paper | Multi-session, **bounded memory** (notes capped at B chars), sessions not re-fed (source claim) | **State inversion / supersession** under a hard budget | Value superseded mid-interaction vs not | Whether superseded value is remembered or forgotten | Not verified | Final-query truth | **Adapter — highest memory priority** | The bounded-notes design is a genuine forced-forgetting control, which most benchmarks lack. |
| **TEPA** | `arXiv:2608.07429` *TEPA: Revoking Stale Memories for Conflict-Robust Language Agents* | Paper | Unified suite spanning memory state and tool outcomes (source claim) | Stale revocation + drift | Conflict injection | Memory-state and tool-outcome metrics | Not verified | Suite-defined | **Inspiration-only** | Method paper with an evaluation attached; not a clean benchmark package. |
| **Memora** | `arXiv:2604.20006` *From Recall to Forgetting* | Paper | Weeks-to-months personalised sessions | Remember / reason / recommend, **forgetting-aware** | Evolving knowledge over time | Forgetting-aware memory metric (source claim) | Not verified | Automated grounding + human eval | **Inspiration-only** | Personalisation domain is off our axis; the forgetting-aware metric is the transferable part. |
| **LOCA-bench** | `arXiv:2602.07962` *Benchmarking Language Agents Under Controllable and Extreme Context Growth* | Paper | Controllable growth | Context growth as a **manipulated variable** | Growth level is the intervention | Task success vs growth | Not verified | Task verifier | **Adapter** | Our own readiness audit already flagged a padding confound; see §4 duplication. |
| **ContextBench** | `arXiv:2602.05892` *A Benchmark for Context Retrieval in Coding Agents* | Paper | Coding-agent context retrieval | Retrieval infrastructure, not memory | Retrieval variants | Retrieval accuracy | Not verified | Task verifier | **Exclusion for memory** | Measures retrieval plumbing; keep out of the memory theme to avoid construct drift. |
| **AMA-Bench** | `arXiv:2602.22769` *Evaluating Long-Horizon Memory for Agentic Applications* | Paper | Agentic applications | Long-horizon memory | Horizon scaling | Not verified | Not verified | Not verified | **Adapter, low priority** | Nothing verified beyond identity. |
| **Memory substrate harness** | `arXiv:2608.15008` *Harness the Memory* | Paper | Meta-harness over LoCoMo, MemoryAgentBench and others (source claim) | Substrate comparison | Substrate swap | Cross-benchmark aggregate | n/a | Inherits each benchmark | **Inspiration-only — harness precedent** | Not a benchmark. Valuable as a design precedent for our own multi-benchmark harness. |
| **Modular memory survey** | `arXiv:2604.01707` *Memory in the LLM Era* | Paper | Survey | Reports token cost, retrieval latency, context scalability, **position sensitivity**, backbone dependence (source claim) | n/a | n/a | n/a | n/a | **Inspiration-only** | Position sensitivity is a measurable we do not currently compute. |
| **Agent-native memory** | `arXiv:2606.24775` *Are We Ready For An Agent-Native Memory System?* | Paper | Three end-to-end workloads incl. LoCoMo (source claim) | End-to-end memory-system effect | Memory system swap | End-to-end task success | n/a | Inherits workloads | **Inspiration-only** | Position paper shape. |
| **agent-memory-eval** | `verifiedstate/agent-memory-eval` @ `6c82208f7638` (2026-04-03) | **NONE DECLARED — no LICENSE file** | 50 fixtures, composite scoring (source claim) | Temporal state, provenance, abstention, conflict | Fixture-based | Composite score across dimensions | Fixtures | Fixture-defined | **Exclusion until licensed** | Unlicensed code and fixtures cannot be vendored. Abstention-plus-provenance dimension is interesting; reimplement clean if wanted. |

## 2. Matrix — memory × tool interaction

This is the brief's question 6 and the most consequential section: several sources
measure memory and tool use **jointly within one task**, not in separate arms.

| Source | Paper / repo / commit | Licence | Task unit | Construct | Intervention vs comparator | Metric + denominator | Traj. | Oracle indep. | Recommendation | Evidence limitation |
|---|---|---|---|---|---|---|---|---|---|---|
| **Mem2ActBench** | `arXiv:2601.19935` *A Benchmark for Evaluating Long-Term Memory Utilization in Task-Oriented…*; `Cantaloupe-M/Mem2ActBench` @ `b00726940b5a` (2026-01-13) | **NONE DECLARED — no LICENSE file** | Multi-turn conversation → tool call | **Memory-driven tool calling / memory-grounded parameter binding**, evaluated in a single task not separate arms (source claim) | Underspecified request requiring a remembered value | Memory-grounded verification with exact and soft matching; strict schema validation (source claim) | Pipeline released | Schema + memory-grounded verifier, separable from the agent | **Adapter after licence resolution — the single closest published match to our construct** | Unlicensed. Head is 2026-01-13, thin activity. Body unread, so metric denominators unconfirmed. |
| **Entity Binding Failures** | `arXiv:2606.30531` *Entity Binding Failures in Tool-Augmented Agents*; `R-Suresh/EntityBindingFailures` @ `af311d10f526` (2026-06-30) | **MIT** | 60 diagnostic tasks, 5 enterprise domains: email, calendar, documents, customer records, issue tracking (source claim) | **Right-tool / wrong-entity** — tool-parameter binding as a first-class failure mode | Ambiguous, underspecified, or confusable target entity; 6 tool-use methods × 5 backends | Binding correctness on the real-world target entity | Diagnostic tasks released | Entity truth is task-defined | **Direct adoption candidate — best licensed fit** | Diagnostic scale is small (60 tasks) and enterprise-shaped; a source page reported "0 diagnostic tasks" in one summary, so the count needs a body check. |
| **When Does Memory Help…** | `arXiv:2605.28224` *When Does Memory Help Multi-Trajectory Inference for Tool-Use LLM Agents?* | Paper | Multi-trajectory inference | **The interaction question stated directly** | Memory on/off across trajectories | Not verified | Not verified | Not verified | **Inspiration-only, read first** | Title promises exactly the conditional we need; nothing beyond identity is verified. |
| **MemToolAgent** | `arXiv:2606.07909` | Paper | Memory-augmented tool use | Joint memory retrieval + reflection + tool execution in one workflow (source claim) | Memory entries from past tool trajectories + user feedback | Tool accuracy plus memory-informed measures (source claim) | Not verified | Not verified | **Inspiration-only** | It is a *method* that improves tool use with memory, not a controlled evaluation of the interaction. |
| **MemTool** | `arXiv:2507.21428` *Optimizing Short-Term Memory Management for Dynamic Tool Calling* | Paper | Multi-turn conversations | Short-term context management for tool calling; joint, not separate arms (source claim) | Removal/search tool policies | Removal Ratio, Avg Residual 3T (source claim) | Not verified | Not verified | **Inspiration-only** | Measures context hygiene during tool calling, adjacent to but not identical with long-term memory. |
| **H-EPM** | `arXiv:2512.07287` *Experience-Evolving Multi-Turn Tool-Use Agent with Hybrid Episodic-Procedural Memory* | Paper | Multi-turn tool use | State-annotated **tool-transition graph** as memory (source claim) | Episodic + procedural memory vs neither | Not verified | Not verified | Not verified | **Inspiration-only** | The tool-transition-graph representation maps onto our existing `trajectory_sequence` edges. |
| **ToolMem** | `arXiv:2510.06664` *Enhancing Multimodal Agents with Learnable Tool Capability Memory* | Paper | Multimodal tool selection | Memory **of tools**, not memory used **by** tools | Capability memory vs none | Tool-proficiency tiers (source claim) | Not verified | Not verified | **Exclusion** | Inverts the construct: it remembers tool quality rather than binding remembered facts into calls. |
| **ReMe / ToolMemory** | `agentscope-ai/ReMe` @ `65cb4ebdd643` (2026-08-31) | **Apache-2.0** | Framework component | Tool usage/performance memory | n/a | Separate components, no single joint metric (source claim) | n/a | n/a | **Exclusion as an eval; note as tooling** | Actively maintained and permissively licensed, but a framework, not a benchmark. |

## 3. Matrix — tool-side and trajectory→task generation

Included only where they contribute a construct the memory sources do not.

| Source | Paper / repo / commit | Licence | Construct | Metric + denominator | Oracle indep. | Recommendation | Evidence limitation |
|---|---|---|---|---|---|---|---|
| **ToolSandbox** | `arXiv:2408.04682`; `apple/ToolSandbox` @ `165848b9a78c` (2025-11-07) | **Apple custom, all rights reserved** | Stateful tool use; milestone DAG + minefields | Milestone similarity along a required-state DAG (source claim) | Milestones are task-defined, independent of the agent | **Inspiration-only pending Peter's licence read** — downgraded from prior *adopt* | Best available per-step ground truth in the tool space, but not redistributable under a standard grant. |
| **ToolMaze** | `arXiv:2606.05806` *When Tools Fail* | Paper | Fault recovery on a fault-exposure denominator | **`PRR = Σ I_recov·I_pert / Σ I_pert`** — body-quoted in prior work | Deterministic verifier | **Direct adoption of the estimator** | Companion Recovery Cost normalises by `|T_m|`, so RC is *not* exposure-conditioned. |
| **AgentCheck** | `arXiv:2607.11098`; `aritra741/AgentCheck` @ `2b89d2c5782f` (2026-07-11) | **MIT** | Clean-run/faulted-run pair over a cached MCP prefix; **memory-recovery interaction via the response cache** | First-divergence comparison | *"holds every tool response constant except one, so the divergence is attributable to the injected fault"* — body-quoted | **Adapter — the memory×recovery bridge** | Bundled 120 scenarios include source-derived cases whose upstream terms are not preserved; engine is clean, scenarios are not. |
| **ToolMisuseBench** | `arXiv:2604.01508` | Paper | Offline deterministic misuse + recovery | Not verified | Deterministic | **Adapter** | Cheapest tool-side entry; offline and deterministic. |
| **FuncBenchGen** | `arXiv:2509.26553`; `megagonlabs/FuncBenchGen` @ `0729e2567dfa` (2026-02-10) | **BSD-3-Clause** | Hidden typed function DAG; **intermediate value propagation** = synthetic parameter binding | Exact target oracle | Generator and oracle are both deterministic and separable | **Direct adoption — synthetic control arm** | Topology/identifier leakage and stale-value shortcuts require partitions and mutants. |
| **FACET** | `arXiv:2608.18580` *Preserving Source Intent and Executable State in Terminal Task Synthesis* | Paper | Generates instruction, solution **and verifier**, grounding all three in the same realised env state; compares generation orders including Forward I→S→V (source claim) | n/a | **Directly addresses generator/oracle independence** — the brief's question 4 | **Inspiration-only, read first** | Highest-value unread paper for our synthetic funnel; ordering result could change our materialiser design. |
| **Anchor** | `arXiv:2605.26321` *Mitigating Artifact Drift in Agent Benchmark Generation* | Paper | Artifact drift in generated benchmarks | Multi-objective reward with per-task checks (source claim) | Verifier framework | **Inspiration-only** | Drift is the failure mode our task-digest pinning already guards. |
| **TRACE** | `arXiv:2510.00415` *Towards Self-Evolving Benchmarks: Synthesizing Agent Trajectories via Test-…* | Paper | Evolution proposer → exploration executor → validatable trajectory audit (source claim) | n/a | Trajectory audited, not self-scored | **Inspiration-only** | Note: distinct from the capability-training TRACE cited elsewhere in our corpus; do not conflate. |
| **BenchAgents** | `arXiv:2410.22584` *Multi-Agent Systems for Structured Benchmark Creation* | Paper | Planning/Generation/Verification/Evaluation agent split | n/a | Verification agent is a separate role | **Inspiration-only** | Role separation is organisational, not cryptographic; a separate agent is not an independent oracle. |
| **Self-Challenging (CaT)** | `arXiv:2506.01716` | Paper | Challenger generates task + verification function; executor solves | n/a | **Same model can be both** — weak independence | **Exclusion for evaluation** | Self-generated verifier is precisely the circularity our funnel gates prohibit. |
| **TASTE** | `arXiv:2605.28556`; `tomerkeren42/TASTE-…` @ `d53da23956d6` (2026-05-31) | **NOASSERTION, `LICENSE` reads "Copyright (c) 2026"** | Tool-sequence evolution → task synthesis | n/a | LLM filter and GT agent are not an independent oracle | **Inspiration-only, cleanroom method** | Confirms prior finding: no redistribution or derivatives. |
| **SPADE** | `arXiv:2608.19197` *Self-Play in Adaptive Synthetic Executable Environments*; `spade-rl/spade` @ `ebd40ec872fc` (2026-08-26) | **MIT** | Designer/agent self-play in executable envs | n/a | Runtime validation | **Inspiration-only, training method** | **Boundary honoured:** its memory ablation concerns the *designer's* historical regret/task buffer in games, and its tool-use gains are a separate self-play experiment. Nothing here supports agent-memory→tool-use causality, and this report makes no such claim. |
| **tau2-bench** | `arXiv:2506.07982`; `sierra-research/tau2-bench` @ `a2c024725189` (2026-08-18) | **MIT** | Dual-control env; DB + COMMUNICATE assertions | Task × trial | State oracle independent of agent | **Substrate, already pinned** | Harbor ships `tau3-bench`, not `tau2`. |
| **recovery-bench** | `letta-ai/recovery-bench` @ `c5f83f2ba4f8` (2026-04-20) | **NONE — licence endpoint 404, no root LICENSE** | Inherited-failure-state recovery | Selection conditioned on `reward == 0` | **No clean-twin arm** (source read in prior work) | **Exclusion for causal recovery claims** | Structural confound, not a reporting gap. |

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
| Clean permissive | LongMemEval, MemoryAgentBench, EntityBindingFailures, FuncBenchGen, AgentCheck engine, tau2-bench, SPADE, ReMe | Adoptable subject to construct fit. |

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
- **Entity Binding Failures is the highest-value licensed acquisition**, because it
  isolates parameter binding while holding the tool fixed. That is the cleanest
  available separation of memory-use from tool-familiarity.
- The absence of released memory-operation logs across every source I checked suggests
  our `state-journal` plus ATIF token/compaction facts may already give us a
  finer-grained memory-operation record than the benchmarks we would adopt.
- Licence quality correlates poorly with construct quality here. The two best-designed
  artifacts, ToolSandbox and Mem2ActBench, are the two least usable.

## 6. Ranked Eval Lab opportunities

Proposed hypotheses with the intervention that would test each. None is a result.

| # | Opportunity | Source basis | Why it ranks here | Duplication warning |
|---:|---|---|---|---|
| 1 | **Memory-bound tool-parameter task family.** Fact written in session 1, superseded in session 2, required as a tool argument in session 3. Score the *argument*, not the prose. | Mem2ActBench + Entity Binding Failures + Supersede | Hits write/read/use, supersession and binding in one unit; all three constructs are otherwise measured separately | New. No lane owns memory×tool jointly. Do **not** fold into the AgentAbstain single-delta work, which is restraint. |
| 2 | **Adopt Entity Binding Failures directly** (MIT, `af311d10f526`) as the licensed entry point for binding. | `arXiv:2606.30531` | Only clean-licensed source that isolates binding with tool held constant | None. Verify the 60-task count against the body; one summary said 0. |
| 3 | **Stale-override arm on existing sessions**: inject a contradicting update, measure which value reaches the tool call. | STALE + TEPA + MemoryAgentBench conflict axis | Deterministic to score from ATIF arguments; no judge needed | Overlaps MemoryAgentBench's conflict competency — adopt their task shape rather than inventing one. |
| 4 | **Bounded-memory forcing control.** Cap the notes budget and stop re-feeding sessions, per Supersede, so recall cannot be replaced by re-reading. | `arXiv:2606.27472` | The single cheapest way to separate memory from context length | **Directly relevant to the LOCA padding confound already flagged at `39022d6`.** Coordinate; do not build a second padding scheme. |
| 5 | **FuncBenchGen synthetic binding arm** (BSD-3) as the known-answer control beside the real-memory tasks. | `arXiv:2509.26553` | Deterministic oracle, generated on demand, no licence risk | Prior work already recommended FuncBenchGen for tool-graph work — reuse that adapter, do not duplicate it. |
| 6 | **Read FACET before finalising any trajectory→task materialiser.** | `arXiv:2608.18580` | Its instruction/solution/verifier generation-order result could invalidate our current ordering assumption | **Blocks, or at least gates, the synthetic funnel families A and C.** Cheap to read, expensive to ignore. |
| 7 | **Memory-recovery bridge via AgentCheck's cache.** Its clean/faulted pair over a cached prefix is a memory-adjacent intervention: the cache *is* the retained state. | `arXiv:2607.11098`, MIT engine | Reuses an already-verified clean-twin design | Engine only. Do **not** vendor the 120 bundled scenarios; upstream terms are unpreserved. |
| 8 | **Adopt PRR as the exposure-conditioned recovery estimator**, with RC explicitly marked non-exposure-conditioned. | `arXiv:2606.05806`, body-quoted | Already verified at body level; nothing left to research | Already recommended in prior work — this is a reminder, not new scope. |
| 9 | **Position-sensitivity feature** from the modular-memory survey: where in the assembled context the probed fact sat. | `arXiv:2604.01707` | Computable from our own packs; no external dependency | New. Belongs to the Data Engineer feature lane, not here. |
| 10 | **LongMemEval as the licensed recall baseline** (MIT), replacing LoCoMo where NC terms are awkward. | `arXiv:2410.10813` | Clean licence, maintained to 2026-05 | Harbor already has a `locomo` adapter; keep it for continuity but stop treating it as the default. |

### Duplication warnings against work underway

- **`benchmark-themes-librarian-reply.md` named ToolSandbox as the T2 anchor for
  adoption. That is now wrong on licence grounds** and must be amended to
  inspiration-only. This report supersedes it on that point.
- **Harbor `adapters/locomo` already exists.** Any memory lane must start from it rather
  than adding a second conversational-memory adapter, and must respect CC BY-NC.
- **`autonomous-research-v1` (74 features) covers T1 only.** No registered producer
  family covers memory or tool constructs, so opportunities 1–4 need a producer before
  they can yield comparable features.
- **LOCA readiness is on HOLD at `39022d6`** for a padding confound. Opportunity 4 is
  the fix for that confound, not a parallel effort.
- **AgentAbstain single-delta admission is restraint, not memory.** Different construct;
  do not merge.

## 7. Gaps and blockers

**Blockers.** Mem2ActBench and `agent-memory-eval` are unlicensed. ToolSandbox needs
Peter's licence decision. LoCoMo is NC. TASTE is all-rights-reserved.

**Gaps.** No verified source releases explicit memory-operation logs. Body-level reads
are missing for the four highest-priority sources — STALE, Supersede, Mem2ActBench and
FACET — so every construct claim about them in §1–§3 is `IDENTITY-VERIFIED,
METHOD-UNQUOTED` and must not be treated as method evidence. Only ToolMaze's PRR and
AgentCheck's arm construction carry verbatim body quotes, both from prior work.
