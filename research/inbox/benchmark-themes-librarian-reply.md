---
type: benchmark-portfolio-recommendation
topic: thematic-benchmark-program
reviewed: 2026-08-31
status: distilled
source_url: https://export.arxiv.org/api/query
source_type: paper
retrieved: 2026-08-31
license_note: Benchmark metadata retrieved via the public arXiv API and Exa search for verification; each benchmark's own code and data licences remain authoritative and are not restated here.
feeds:
  - parked
---

# Thematic benchmark portfolio — recommendation

Answers the six questions in `benchmark-themes-brief.md`. Every arXiv ID below was
resolved against the arXiv API in this session: **23 requested, 23 resolved, 0
unresolved**. Harbor adapter facts come from the local checkout at `d13260df`
(86 adapters).

## 0. Missing input, stated up front

`research/analysis/agentic-benchmark-feature-inventory-2026-08-31.json` **does not
exist** anywhere in the repository or the estate. I searched `research/analysis/`,
`research/inbox/`, and `/private/tmp`. This recommendation therefore does not rest on
it. If that inventory carries feature-level coverage decisions, treat §2 as
provisional until it is reconciled.

The two RSI pilot evidence files were located and read at
`/private/tmp/rsi-game2048-calibration-evidence.json` and
`/private/tmp/rsi-bbo-calibration-evidence.json` (schema
`evallab-rsi-calibration-evidence/v1`).

## 1. The three themes

The brief asks which themes match Eval Lab's actual strengths. Those strengths are
trajectory capture, deterministic derived features, opportunity denominators, and
causal grading — not score collection. Themes are chosen so that **the trajectory is
the product**, and the leaderboard number is a by-product.

| Theme | Question it answers | Why it fits Eval Lab specifically |
|---|---|---|
| **T1 — Long-horizon research improvement** | Does the agent improve a working method over many iterations, and does the improvement generalise off the visible split? | Already piloted. Sealed-split design maps directly onto the C0/C1/C2 causal ladder, and the iteration sequence is exactly what alignment and change-point features consume. |
| **T2 — Stateful tool use under unreliability** | Given tools with state and dependencies, does the agent compose them correctly, and recover when the environment misbehaves? | The only theme where a **fault-exposure denominator** already exists in published form, so recovery is measurable rather than narrated. |
| **T3 — Memory and context under controlled growth** | Does information written earlier survive dilution, compaction, and session boundaries, and get used as a tool argument rather than recited? | Directly consumes the `state-journal` and ATIF token/compaction facts we already emit. |

These three are deliberately not "coding", "web", and "science". Each is defined by a
**construct plus a controllable variable**, which is what makes a derived feature
programme possible.

## 2. Theme detail

### T1 — Long-horizon research improvement

| Role | Benchmark | Verified source | What it actually reports |
|---|---|---|---|
| **Anchor** | **RSI-Exam** | `aiming-lab/RSI-Exam`; HF dataset per pilot evidence | Weak-but-working start, agent iterates on a visible split for up to 12 h, submission replayed **once** on a sealed split. Reported score is sealed-split performance. |
| Support | **RE-Bench** | [`arXiv:2411.15114`](https://arxiv.org/abs/2411.15114) | 7 open-ended ML research engineering environments; per-environment scoring function tracked **over time**; human baseline from 71 eight-hour attempts by 61 experts. |
| Support | **MLE-bench** | [`arXiv:2410.07095`](https://arxiv.org/abs/2410.07095) | 75 Kaggle competitions; headline metric **Any Medal (%)** with mean and SEM, split by Low/Medium/High. |
| Adjacent, newer | **AI4AI-Bench** | [`arXiv:2608.20318`](https://arxiv.org/abs/2608.20318) | Rewrite a training algorithm in a fixed window, rerun to completion, score by a **fixed hidden evaluator**; 10 algorithm families across 10 frozen repositories. |

**Shared constructs:** iterative improvement over a budget; visible-versus-sealed
generalisation; artifact selection under uncertainty; anytime-versus-final score.

**Minimum comparable features** — all four report a time or iteration budget and a
held-out score, so these are the fields that must exist for any cross-benchmark
comparison:

- budget declared in the run's own unit (seconds, iterations, or attempts) **and**
  consumed budget;
- `baseline_visible_score` and `best_visible_score`;
- sealed or held-out score, with an explicit null when unscored;
- `best_improvement_iteration`;
- artifact lineage — which version was submitted, and whether it is the one the log
  claims;
- `anytime_final_score_gap`.

The last three come straight out of our own pilots, not from the papers.

### T2 — Stateful tool use under unreliability

| Role | Benchmark | Verified source | What it actually reports |
|---|---|---|---|
| **Anchor** | **ToolSandbox** | [`arXiv:2408.04682`](https://arxiv.org/abs/2408.04682) | Stateful, on-policy, conversational tool use. **Milestone similarity** along a DAG of required intermediate states, plus **minefields** as negative constraints. Explicitly trajectory-centric, not final-answer. |
| Support | **ToolBench-X** | [`arXiv:2606.25819`](https://arxiv.org/abs/2606.25819) | Five hazard families over executable tasks; a construction constraint that injected failures must be **recoverable**. |
| Support | **ToolMaze** | [`arXiv:2606.05806`](https://arxiv.org/abs/2606.05806) | Defines **Perturbation Recovery Rate** on a fault-exposure denominator and **Recovery Cost**. See §4. |
| Support | **ToolMisuseBench** | [`arXiv:2604.01508`](https://arxiv.org/abs/2604.01508) | Offline, deterministic, tool misuse **and recovery**. |
| Adjacent | **τ²-bench** | [`arXiv:2506.07982`](https://arxiv.org/abs/2506.07982) | Dual-control environment; database and communicate assertions. Substrate we already pin. |

**Shared constructs:** tool-graph dependency order, state carried between calls,
injected-fault exposure, recovery versus blind retry, exposure-conditioned rates.

**Minimum comparable features:**

- per-call schema conformance against the declared tool definitions;
- fault exposure indicator per trial, and the **count** of exposed trials as an
  explicit denominator;
- recovery indicator with its own definition recorded, never inferred from a later
  pass;
- blind-retry count — same tool, same argument hash, immediately after a non-zero
  exit;
- milestone or intermediate-state hit sequence where the benchmark supplies one;
- cost of the failed prefix, separated from total cost.

ToolSandbox is the anchor rather than ToolBench-X because **milestones give per-step
ground truth**, which is the scarce resource. Hazards are cheap to add on top of a
milestone environment; milestones cannot be retrofitted onto a hazard suite.

### T3 — Memory and context under controlled growth

| Role | Benchmark | Verified source | What it actually reports |
|---|---|---|---|
| **Anchor** | **LOCA-bench** | [`arXiv:2602.07962`](https://arxiv.org/abs/2602.07962) | Controllable and extreme context growth. Controllability is the reason it anchors: growth is the manipulated variable. |
| Support | **MemoryAgentBench** | [`arXiv:2507.05257`](https://arxiv.org/abs/2507.05257) | Incremental multi-turn interactions; capability axes including accurate retrieval and conflict resolution. |
| Support | **LoCoMo** | Harbor adapter `adapters/locomo` | Multi-session dialogue QA. **Already a native Harbor adapter.** |
| Support | **BEAM** | [`arXiv:2510.27246`](https://arxiv.org/abs/2510.27246) | Conversations up to 10M tokens; 100 conversations, 2,000 validated questions. |
| Adjacent, newer | **AMA-Bench** | [`arXiv:2602.22769`](https://arxiv.org/abs/2602.22769) | Long-horizon memory for agentic applications. |
| Adjacent, newer | **Memora** | [`arXiv:2604.20006`](https://arxiv.org/abs/2604.20006) | Weeks-to-months personalised memory; reports a **forgetting-aware** metric, and separates remembering, reasoning, recommending. |
| Method reference | Memory substrate harness | [`arXiv:2608.15008`](https://arxiv.org/abs/2608.15008) | Holistic comparison across LoCoMo, MemoryAgentBench and others — useful as a **harness design precedent**, not a benchmark to run. |
| Method reference | Modular memory survey | [`arXiv:2604.01707`](https://arxiv.org/abs/2604.01707) | Reports token cost, retrieval latency, context scalability, **position sensitivity**, backbone dependence. |

**Shared constructs:** retention across a boundary, conflicting-update resolution,
retrieval-versus-use, position sensitivity, token cost of remembering.

**Minimum comparable features:**

- cumulative prompt tokens at the point the probed fact is needed;
- boundary events crossed — compaction, summarisation, session restart — as counted
  events, not inferred;
- effective cache ratio as `cached / prompt` (cached is a subset of prompt);
- whether the recalled entity was **used as a tool argument** or only restated;
- stale-versus-updated value selection on conflicting writes;
- position of the probed fact in the assembled context.

The retrieval-versus-use distinction is the one most benchmarks collapse and the one
our ATIF data can actually separate.

## 3. Keep / defer table

| Benchmark | Theme | Decision | Reason |
|---|---|---|---|
| RSI-Exam | T1 | **Keep — anchor** | Already piloted twice with real evidence; sealed split is the strongest generalisation control in the candidate set |
| RE-Bench | T1 | **Keep — support** | Human baseline at 71×8 h attempts is the only genuine human reference among candidates |
| MLE-bench | T1 | **Keep — support, import-only first** | Any-Medal with SEM is a clean estimand, but 75 Kaggle environments is the heaviest environment cost in the set |
| AI4AI-Bench | T1 | **Keep — adjacent** | Frozen repositories plus hidden evaluator materially strengthen the sealed-split construct |
| ToolSandbox | T2 | **Keep — anchor** | Milestone DAG plus minefields is per-step ground truth; nothing else in the candidate list offers it |
| ToolBench-X | T2 | **Keep — support** | Recoverability is a stated construction constraint, not an afterthought |
| ToolMaze | T2 | **Keep — support** | Supplies the exposure-denominator formula the programme needs |
| ToolMisuseBench | T2 | **Keep — support** | Offline and deterministic, so it costs almost nothing to run |
| τ²-bench | T2 | **Keep — adjacent** | Already pinned; note **Harbor has `tau3-bench`, not `tau2`** |
| LOCA-bench | T3 | **Keep — anchor** | Growth is controllable, which is what makes it a cause rather than a correlate |
| MemoryAgentBench | T3 | **Keep — support** | Conflict resolution axis is not covered elsewhere |
| LoCoMo | T3 | **Keep — support** | Native Harbor adapter already exists; lowest-friction entry point in the entire portfolio |
| BEAM | T3 | **Keep — support** | Extends the token axis far past our current corpus |
| AMA-Bench, Memora | T3 | **Keep — adjacent, newer** | Forgetting-aware metric and multi-week horizon are genuinely absent from the original candidate list |
| **PaperBench** | — | **Defer** | Measures replication of 20 ICML papers against **8,316 rubric subtasks**. The construct is rubric grading, not agent improvement; the grading cost dominates and the signal is about paper comprehension. Different capability. |
| **CORE-Bench** | — | **Defer** | Computational reproducibility of published results. Overlaps MLE-bench and PaperBench on environment cost while measuring dependency installation and figure reading. Redundant against T1 once RE-Bench and RSI-Exam are in. |
| **AgentBoard** | — | **Defer as a benchmark, adopt as a design precedent** | Its progress-rate and subgoal-matching design is genuinely aligned with our aims ([`arXiv:2401.13178`](https://arxiv.org/abs/2401.13178)), but its nine environments are 2024-era and its value to us is the **metric design**, not the tasks. Borrow subgoal progress; do not run the suite. |
| GAIA / GAIA2 | — | **Defer** | Native Harbor adapters exist (`gaia`, `gaia2`), but the construct is general assistant QA. No controllable variable, so no causal arm. |
| OSWorld | — | **Defer** | Native adapter exists; GUI control is a fourth construct and would breach the three-theme constraint |
| SWE-bench | — | **Defer for theming** | `swebench`, `swebench_multilingual`, `swebenchpro` adapters exist and remain useful as a regression corpus, but the construct is patch correctness, which none of the three themes needs |

Newer benchmarks added only where they strengthen a chosen theme: **AI4AI-Bench**
(T1), **AMA-Bench** and **Memora** (T3), **ToolMaze** and **ToolMisuseBench** (T2).
Deliberately noted but **not** recommended: **ReliabilityBench**
([`arXiv:2601.06112`](https://arxiv.org/abs/2601.06112)), **OrchestraBench**
([`arXiv:2608.05263`](https://arxiv.org/abs/2608.05263)), and **Retry, Switch, or
Abstain?** ([`arXiv:2608.11977`](https://arxiv.org/abs/2608.11977)) — all three are
on-theme for T2 but would take the theme past four supporting benchmarks. Revisit if
a T2 supporting slot frees up.

## 4. What is measured elsewhere that we do not yet compute

The brief asks what is typically measured. Four patterns recur across the verified
set, and each is a concrete derived-feature target:

1. **Exposure-conditioned rates.** ToolMaze reports
   $\mathrm{PRR} = \frac{\sum_\tau I_{\text{recov}}(\tau) I_{\text{pert}}(\tau)}{\sum_\tau I_{\text{pert}}(\tau)}$
   — the denominator is trials **actually exposed** to the injected fault, not all
   trials. Zero exposure leaves the rate undefined, which matches our null-on-zero
   rule. Note the asymmetry: Recovery Cost in the same paper normalises by $|T_m|$,
   so it is **not** exposure-conditioned.
2. **Intermediate-state ground truth.** ToolSandbox milestones and AgentBoard
   subgoal progress both score the path, not the endpoint. We have alignment
   machinery and no labelled milestones; this is the cheapest high-value acquisition.
3. **Anytime versus final.** RE-Bench tracks score over time; our own BBO pilot
   already found final `0.3122` against anytime `0.1587`, and ranked
   `anytime_final_score_gap` third among next features. This one is validated
   internally before it is adopted externally.
4. **Sealed-split transfer.** RSI-Exam and AI4AI-Bench both replay onto a hidden
   evaluator. Our pilot correctly holds transfer gap at `null` because raw visible
   score and normalised sealed reward are not arithmetically comparable — that
   guard should become a schema invariant, not a note.

## 5. Execution lanes

| Lane | Definition | Members |
|---|---|---|
| **Native Harbor** | Adapter exists in the local checkout `d13260df` | `locomo` (T3), `tau3-bench` (T2 adjacent), plus deferred `gaia`, `gaia2`, `osworld`, `swebench*`, `cooperbench`, `mmau`, `crmarena`, `kumo` |
| **Native Inspect** | Implemented in Inspect Evals, run there | Several T1 and deferred candidates; the Inspect Evals registry is the authority and should be enumerated before any parity claim |
| **Inspect–Harbor parity** | Run both, compare on identical task digests | Reserve for exactly one benchmark per theme; parity is expensive and only pays where a scoring dispute is plausible |
| **Import-only evidence** | Consume published results; never claim we reproduced them | MLE-bench, PaperBench, CORE-Bench, and **both current RSI pilots** |

**RSI-Exam is not on any Harbor adapter.** Neither `rsi`, `re-bench`, `paperbench`,
`mle-bench`, `core-bench`, `agentboard`, nor `toolsandbox` has an adapter at
`d13260df`. T2's anchor and T1's anchor both require adapter work; T3's `locomo`
does not. That ordering should drive sequencing.

## 6. Initial corpus and expansion rule

**Initial corpus — three benchmarks, one per theme, chosen for lowest cost to first
signal:**

| Theme | Start with | Why this one first |
|---|---|---|
| T3 | **LoCoMo** | Native adapter already exists. Zero adapter cost, immediate trajectory data. |
| T2 | **ToolMisuseBench** | Offline and deterministic — no live tool environment, no network, cheapest possible T2 entry. |
| T1 | **RSI-Exam**, calibration-only | Two pilots already run. Continue as calibration, not as a score. |

Anchors that need adapters (ToolSandbox, LOCA-bench) come second, once the feature
schema is proven against the cheap entries.

**Expansion rule — information gain, not popularity.** Admit a new benchmark only if
it clears all five:

1. it manipulates a **variable we can control**, so a C2 arm is constructible;
2. it exposes **per-step or intermediate** evidence, not only a final score;
3. it contributes at least one **construct not already covered** by that theme's
   existing members;
4. its **denominator is definable** — we can say what the rate is over, and what a
   zero-opportunity trial yields;
5. environment cost is justified by (1)–(4), assessed **after** the cheap member of
   the same theme has produced data.

A benchmark failing (2) or (4) is import-only evidence at best. Popularity, leaderboard
presence, and recency are explicitly not admission criteria.

Hard cap: **one anchor plus three supports per theme.** At the cap, admitting a new
benchmark requires demoting an existing one — which is what keeps this a programme
rather than a collection.

## 7. Claim boundaries

- ~~The referenced feature inventory does not exist.~~ **Superseded — see §8.** The
  inventory exists on fast-forwarded main; §2 is reconciled against it in §8.
- **Neither RSI pilot is leaderboard-comparable.** Still stands. Both evidence files
  record `official_leaderboard_comparable: false`. For game2048 the stated reasons
  are: network mode changed from none to public because Harbor Docker on Darwin
  rejects no-network; agent timeout cut from the official 12 h to 2,160 s; initial
  verifier timeout multiplier reduced to 0.2 and that window expired without an
  outcome. Evidence class is `calibration_only_darwin_public_egress`. A
  verifier-only regrade does not repair agent-phase deviations, so this is unchanged
  by §8.
- ~~No sealed RSI score exists yet.~~ **Superseded — see §8.** The game2048
  verifier-only regrade has completed.
- **Transfer gap on the normalised scale remains undefined**, but a raw-scale
  comparison is now possible. See §8.
- **Benchmark descriptions are source-verified for identity, not for method depth.**
  All 23 arXiv IDs resolved to the titles cited. What each benchmark *reports* comes
  from abstracts and official pages; only ToolMaze and AgentCheck have been read at
  body-text level in prior work. Everything else is `BINDING-VERIFIED,
  METHOD-UNQUOTED` and should not be treated as method evidence until quoted.
- **Harbor adapter presence is a name match** against `adapters/` at `d13260df`. It
  says an adapter exists, not that it is current, passing, or parity-checked.
- **Inspect Evals coverage was not enumerated.** I did not list its registry, so the
  Native Inspect lane in §5 names no specific members and should be filled before
  any parity work is scheduled.

## 8. Correction — 2026-08-31, after main fast-forward

Two §7 claims are withdrawn. The three-theme recommendation in §1–§3 stands
unchanged; §5 sequencing is revised.

### 8.1 The feature inventory exists

`research/analysis/agentic-benchmark-feature-inventory-2026-08-31.json` is present on
main at `58c9b592`. Schema `agentic-benchmark-feature-inventory/v1`, generated
2026-08-31. The §0 missing-input caveat is withdrawn, and §2 is no longer provisional.

What it contains, and how it lands against this recommendation:

| Inventory fact | Effect on §1–§3 |
|---|---|
| One registered family, `autonomous-research-v1`, producer `evallab.autonomous_research`, source table `autonomous_research_runs`, **74 features** across 7 constructs | **Confirms T1.** Its constructs are Autonomous Research & Method Improvement, Score-Time Dynamics & Budget Scaling, Milestone & Rubric Progression, Selection & Generalization, Reproducibility & Replay, Environment Reconstruction & Dependency Repair, Data Integrity & Contamination Prevention. |
| `supported_benchmarks` = RSI-Exam, RE-Bench, PaperBench, MLE-bench, CORE-Bench, AgentBoard | T1's anchor and both supports already have a producer. **PaperBench, CORE-Bench and AgentBoard are also supported** — see §8.3. |
| `benchmarks` array also covers ToolSandbox, Tau2, GAIA, OSWorld, but **none is in a registered family** | **T2 and T3 have no producer family yet.** This is the sequencing change. |
| Fields `scale_binding_digest`, `score_scale_compatible` (BOOLEAN), `visible_hidden_transfer_gap` (C2), `hidden_score` (C3) | §4 item 4 is **already implemented**. My recommendation that the scale guard "become a schema invariant, not a note" was already satisfied; withdraw it as a recommendation. |
| `required_milestones`, `completed_milestones`, `milestone_completion_rate` | The AgentBoard metric design I recommended borrowing is **already adopted**. §3's "defer the suite, adopt the design" is confirmed as done, not pending. |
| `priority_feature_tiers` with `tier_1_deterministic` (10), `tier_2_efficiency` (7), `tier_3_semantic_hypotheses` (5), `not_decision_ready` (4) | Independent of my §2 lists and consistent with them. `not_decision_ready` explicitly includes "visible score without hidden-transfer context", which is why §8.2 matters. |

§2's minimum-comparable-feature lists for T1 are a strict subset of the 74 registered
features, so nothing there needs adding. T2 and T3 lists remain proposals, because no
producer family covers them.

### 8.2 Game2048 sealed score exists

The verifier-only regrade completed. Reported values:

| Quantity | Value |
|---|---|
| Sealed reward | **0.37800819** |
| Mean raw | **23167** |
| `valid_fraction` | **1.0** |

The "no sealed RSI score" claim is withdrawn, as is the reliance on
`scores.sealed.status = "awaiting_verifier_regrade"`. The
`/private/tmp/rsi-game2048-calibration-evidence.json` snapshot I read still shows
`reward: null`; it is stale and should not be cited.

Three consequences worth stating precisely:

1. **`hidden_score` is now populated** for this run, and it is a C3-grade field in the
   inventory. That is the highest causal grade in the schema.
2. **A raw-scale transfer comparison is now available.** Mean raw 23167 is on the same
   scale as the visible raw means — `baseline_raw_mean` 2060.0 and
   `best_comparable_raw_mean` 17233.5. The sealed replay therefore scored **above**
   the best comparable visible full-suite result on raw merge score, with every seed
   valid. The normalised reward 0.37800819 is still not comparable to a raw mean, so
   `visible_hidden_transfer_gap` stays undefined **on the normalised scale** until
   `score_scale_compatible` is set. The null is now a scale-binding question, not a
   missing-measurement question.
3. **Leaderboard comparability is unchanged.** The regrade was verifier-only, so the
   agent phase was not re-run and the three recorded deviations — forced public
   egress, 2,160 s instead of 12 h, verifier multiplier 0.2 — all still apply.
   Evidence class remains `calibration_only_darwin_public_egress`.

### 8.3 Revised sequencing

§5's initial corpus was ordered by adapter cost, which put LoCoMo first. The inventory
adds a second axis, producer coverage, and the two axes disagree:

| Theme | Adapter at harbor `d13260df` | Registered producer family | End-to-end today |
|---|---|---|---|
| **T1** | absent | **yes — 74 features** | **Furthest along.** Two pilots run, one now with a sealed C3 score. |
| T2 | absent for ToolSandbox; `tau3-bench` only | none | Needs both |
| T3 | **`locomo` exists** | none | Can produce trajectories immediately, cannot yet produce comparable features |

**Producer coverage wins**, because a benchmark that runs but yields no registered
features produces trajectories we cannot compare. Revised order:

1. **T1 first.** It is the only theme that is instrumented end-to-end. Continue
   RSI-Exam as calibration and land the raw-scale transfer comparison from §8.2.
2. **T3 second, split.** Run `locomo` for trajectory volume now, but treat a T3
   producer family as the blocking deliverable before any T3 feature claim.
3. **T2 third.** Needs both an adapter and a producer family; ToolMisuseBench remains
   the cheapest entry once one exists.

### 8.4 Deferrals revisited

- **PaperBench** — the inventory shows `total_rubric_subtasks`,
  `completed_rubric_subtasks` and `rubric_completion_rate`, so rubric progression is
  already a modelled construct and my "different capability" reason was wrong. The
  deferral holds on **cost** alone: 8,316 gradable subtasks across 20 papers is the
  most expensive grading load in the candidate set.
- **CORE-Bench** — deferral unchanged; still redundant against T1 at high environment
  cost, now with the added note that its producer support already exists, so admitting
  it later is cheap.
- **AgentBoard** — confirmed. Its metric design is already in the schema; the suite
  itself stays deferred.

No change to the keep/defer decisions in §3.
