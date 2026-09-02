---
status: proposed
reviewed: 2026-08-31
audience:
  - research
  - operator
sources:
  - research/inbox/benchmark-themes-librarian-reply.md
  - research/inbox/feature-analysis-meta-analyst-reply.md
  - research/analysis/agentic-benchmark-feature-inventory-2026-08-31.json
  - research/analysis/memory-tool-evals-source-matrix-2026-08-31.md
---

# Thematic benchmark portfolio

## Decision

Use **three research themes**, not a benchmark zoo. A benchmark enters the active portfolio only when it strengthens one of these questions:

1. **Autonomous research and improvement** — can the agent improve a method under budget, select an artifact, and generalize off the visible split?
2. **Stateful tool use and recovery** — can the agent compose stateful tools, respect dependencies, and recover from controlled failures?
3. **Memory, context and continuity** — can information survive context growth, compaction and session boundaries and still guide action?

These themes match Eval Lab's actual strengths: full trajectory capture, deterministic fact projection, opportunity denominators, artifact lineage and causal grading.

## Theme 1 — Autonomous research and improvement

**Research question:** Does iterative work produce a real improvement, and does the selected artifact survive sealed evaluation?

| Role | Benchmark | Contribution | Lane |
|---|---|---|---|
| **Anchor** | [RSI-Exam](https://github.com/aiming-lab/RSI-Exam) / [dataset](https://huggingface.co/datasets/RSI-Exam/RSI-Exam) | Visible experimentation, artifact selection, sealed replay, long budget | Native Harbor task; canonical for RSI |
| Support | [RE-Bench](https://arxiv.org/abs/2411.15114) | Score-over-time and horizon scaling with human comparison | Import first; native execution after task audit |
| Support | [MLE-bench](https://arxiv.org/abs/2410.07095) | ML experimentation, selection and contamination pressure | Native Inspect or import first |
| Support | [CORE-Bench](https://arxiv.org/abs/2409.11363) | Reproducibility, environment reconstruction and dependency repair | Import/limited slice first |
| Metric precedent | [PaperBench](https://arxiv.org/abs/2504.01848) | Hierarchical rubric and milestone design | Defer full execution; borrow rubric structure |

**Core comparable features:** budget declared/consumed, experiment count and validity, selected artifact digest, best/selected visible score, sealed score, scale-binding status, selection reconstructibility, anytime/final score, replay validity.

**Current evidence:** BBO and Game2048 are calibration-only RSI runs. Game2048's verifier regrade produced reward `0.37800819`; neither run is leaderboard-comparable because the Darwin task copy used public egress and reduced agent timeouts.

## Theme 2 — Stateful tool use and recovery

**Research question:** When the environment exposes state, dependencies and failures, does the agent diagnose and recover rather than blindly retry?

| Role | Benchmark | Contribution | Lane |
|---|---|---|---|
| **Anchor — method/milestone precedent** | [ToolSandbox](https://arxiv.org/abs/2408.04682) | Stateful tool interactions, intermediate milestones and negative constraints | Milestone design usable as precedent read from the paper; code/data adoption blocked pending licence decision (Apple custom, all rights reserved — see source matrix) |
| Cheap controlled corpus | Eval Lab `mcp-recovery-v1`, tool-composition and FuncDAG families | Deterministic injected faults, handle order, dependency ground truth | Native Harbor/synthetic |
| Support | [τ²-bench](https://arxiv.org/abs/2506.07982) | Stateful dual-control interaction and policy assertions | Native Inspect where supported |
| Metric precedent | [AgentBoard](https://arxiv.org/abs/2401.13178) | Subgoal progress rather than endpoint-only success | Borrow progress metric; defer full suite |
| Candidate | [ToolMaze](https://arxiv.org/abs/2606.05806) | Exposure-conditioned recovery and recovery-cost framing | Watchlist pending method-level audit |

**Core comparable features:** fault exposure count, recovery opportunity count, certified recovery, blind retries, post-error overhead, tool schema conformance, dependency/milestone sequence, failed-prefix cost, zero-exposure null semantics.

The exposure denominator is load-bearing. Recovery rate is undefined when no fault was encountered; it is not zero.

## Theme 3 — Memory, context and continuity

**Research question:** Does information written earlier remain available and get used correctly after controlled context growth or a boundary event?

| Role | Benchmark | Contribution | Lane |
|---|---|---|---|
| **Initial anchor** | LoCoMo / RSI `locomo_longterm_memory` | Multi-session memory with the lowest current adapter cost | Native Harbor; CC BY-NC 4.0 — non-commercial use and derivation only, derived data inherits NC (see source matrix) |
| Support | [MemoryAgentBench](https://arxiv.org/abs/2507.05257) | Incremental interactions, retrieval and conflict resolution | Import/native task audit |
| Controlled-growth candidate | [LOCA-bench](https://arxiv.org/abs/2602.07962) | Context growth as a manipulated variable | Watchlist pending method-level audit |
| Internal controlled corpus | Eval Lab Action Memory and context-operation families | Write/read/use edges, compaction and session dependency | Native synthetic |

**Core comparable features:** fact write/read/use edges, context position, prompt and cached tokens at use, compaction/session boundaries, stale-versus-updated selection, retrieval latency, and whether recalled information changed a tool argument or only appeared in text.

Retrieval and use must stay distinct. Repeating a remembered value is weaker evidence than using it in a correct action.

## Active, deferred and precedent-only choices

| Benchmark/family | Decision | Reason |
|---|---|---|
| RSI-Exam | **Active anchor** | Already produces real long-horizon trajectory and sealed evidence |
| RE-Bench | **Keep, support** | Strong time-budget contrast inside Theme 1 |
| MLE-bench | **Keep, support; import first** | Activates selection and contamination constructs but has high environment cost |
| CORE-Bench | **Keep, limited support** | Activates dormant reproducibility and repair features |
| ToolSandbox | **Keep, Theme 2 anchor as method precedent only** | Intermediate-state ground truth is more valuable than another endpoint score; Apple custom, all rights reserved licence — method/milestone precedent usable, code/data adoption blocked pending licence decision |
| Existing MCP recovery / FuncDAG / tool composition | **Active cheap corpus** | Controlled opportunities already exist in Eval Lab |
| LoCoMo | **Active next pilot under CC BY-NC 4.0** | Lowest-friction memory benchmark and direct Theme 3 coverage; non-commercial use only, derived data inherits NC, and Harbor's existing `adapters/locomo` is already bound by those terms |
| MemoryAgentBench | **Keep, support** | Adds update/conflict behavior absent from simple QA |
| PaperBench | **Precedent first** | Rubric design is useful; full replication suite is expensive and overlaps Theme 1 |
| AgentBoard | **Metric precedent** | Borrow subgoal-progress design instead of adding nine environments immediately |
| GAIA | **Defer** | Broad assistant QA lacks a controlled variable for the three-theme programme |
| OSWorld | **Defer** | GUI control is a separate fourth construct |
| SWE-bench variants | **Regression corpus, not active theme** | Patch correctness does not by itself answer the three research questions |
| Tau2 | **Adjacent Theme 2** | Add only after ToolSandbox/internal recovery analyses are working |

## Initial corpus

Start with the cheapest source of one informative contrast per theme:

| Theme | Initial corpus | First analysis |
|---|---|---|
| T1 | Existing RSI BBO + Game2048 evidence; one additional clean-completion RSI slice | Composite outcome, artifact/log conformance, selected/sealed binding |
| T2 | Existing MCP recovery and tool-composition tasks | Exposure-conditioned recovery, blind retry, milestone progress |
| T3 | LoCoMo or RSI `locomo_longterm_memory` | Write/read/use graph across session and context boundaries |

Do not schedule a large new benchmark integration until the relevant analysis view works on the cheap corpus.

## Expansion rule

A benchmark is admitted only if it clears all five gates:

1. It strengthens one of the three themes.
2. It creates a controllable variable or a stronger contrast.
3. It exposes trajectory/intermediate evidence, not only a final score.
4. Its opportunity denominator and zero-opportunity behavior are definable.
5. The information gain justifies environment and verifier cost.

Hard cap: one anchor plus at most three active supports per theme. Adding a fourth support requires demoting an existing one.

Recency and leaderboard popularity are not admission criteria.

## Execution-lane rules

| Lane | Authority |
|---|---|
| Native Harbor | Canonical for Harbor tasks and RSI |
| Native Inspect | Authoritative only for Inspect source facts and explicitly bound deterministic outcomes |
| Inspect-Harbor parity | Paired evidence only; exact task, trial, attempt, harness and verifier binding required |
| Import-only | Descriptive evidence; never claimed as locally reproduced performance |

Only one benchmark per theme should receive cross-runner parity work, and only when a genuine scoring or harness-equivalence question justifies the cost.

## Watchlist claim boundary

The established anchor/support descriptions above use primary project or paper links. The 2026 ToolMaze and LOCA-bench entries are watchlist candidates whose identities were source-checked by the Librarian, but their methods have not been independently audited in Eval Lab. They should not drive implementation until that method-level review is complete.
