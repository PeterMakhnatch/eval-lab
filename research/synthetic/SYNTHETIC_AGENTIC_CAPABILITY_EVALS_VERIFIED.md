---
topic: synthetic-agentic-capability-evals
created_at: 2026-08-25T15:25:00Z
updated_at: 2026-08-25T23:15:00Z
repository_state_commit: e7fd00899d6105e2185bf319bafd36ac1d87a17c
author: "OpenAI Codex GPT-5.6-Sol, synthetic-evals research orchestrator — requested by Peter"
source_type: "prior OMP/Exa reports reconciled against primary papers, official repositories, released datasets, licenses, and current Eval Lab code"
status: reviewed
scope: "Synthetic generation of executable agentic-capability evaluations; excludes generic QA, domain-only coding data, and training-only claims unless they contribute a reusable generation or verification method"
sibling_policy: "Independent verified synthesis. Earlier OMP/Exa reports are inputs, not authorities; conflicts are resolved here against primary sources."
---

# Synthetic agentic capability evaluations: verified landscape and Eval Lab program

## Executive decision

Build a **verifier-first evaluation generator**, not a generic synthetic-data factory.
The useful unit is not a prompt or a successful trajectory. It is a reproducible package:

\[
(task, s_0, tools, hidden\ target, verifier, lineage, factor\ controls)
\]

that can be executed under a clean reset, rejects no-op and plausible wrong solutions, and supports a matched comparison against a baseline task.

The reviewed literature separates into three materially different groups:

1. **Evaluation-first task generators.** FuncBenchGen and TASTE directly generate executable evaluation tasks. FuncBenchGen is public and BSD-3-Clause; TASTE's current code/artifact license is review-only. AgentAbstain publishes a generated paired benchmark, but the AbstainGen generator is intentionally withheld and evaluation combines deterministic commit checks with an LLM response judge.
2. **Benchmark-specific perturbation systems.** ToolMaze and ToolBench-X construct recovery/unreliability evaluations. Their taxonomies and controlled hazards are reusable; neither is a general capability-eval generator.
3. **Training-first generators.** TRACE, AReaL-SEA, AWM, SPADE, RandomWorld, DIVE, TOUCAN, APIGen-MT, ASTRA, EnvFactory, and SyntheticAgentTraceQA primarily generate RL environments, SFT trajectories, or supervision. Execution during data generation does **not** make their output an evaluation suite.

Immediate priority: finish the evidence migration enabled by the now-fail-closed certification gate. The repository contains three experimental Function-DAG packages and sequence-analysis machinery, but it does not contain the complete 12-task campaign, a model cohort, or durable execution records for the checked-in experimental certificates.

## Operational definition: evaluation-grade synthetic task

This is a proposed Eval Lab admission standard, not a claim of field consensus.
A generated task is evaluation-grade only when all of the following are preserved as evidence:

1. **Executable state machine:** tools operate on a resettable environment, not an LLM-simulated world unless simulation quality is itself the variable under test.
2. **Reachable hidden target:** a privileged construction path establishes a reachable target state or exact result without exposing it to the evaluated agent.
3. **Independent verifier:** success follows from hidden tests, state differences, or exact executable predicates. An LLM judge may provide secondary semantic analysis, not sole ground truth.
4. **Negative controls:** a no-op agent and plausible wrong implementations are actually executed and rejected.
5. **Construct control:** a task states the capability opportunity and changes one primary factor relative to a baseline or paired variant.
6. **Provenance and regeneration:** source task, generator version, parameters, seed, partition, licenses, and content digests are recorded; the same inputs regenerate the same package.
7. **Coverage and novelty:** acceptance records procedural diversity—tool sequences, DAG motifs, state transitions, or perturbation cells—not only paraphrase diversity.
8. **Difficulty evidence:** a held-out model cohort estimates discrimination and saturation separately from validity. Training-oriented pass bands are not validity gates.

## Landscape: what each system actually emits

| System | Primary artifact | Primary role | Grading or selection signal | Public generator / reuse state | Eval Lab decision |
|---|---|---|---|---|---|
| **FuncBenchGen** | On-the-fly hidden function-dependency DAG tasks | Evaluation | Exact target-number match after deterministic tool execution | Public; BSD-3-Clause | **Implement and run first.** Best released controllable eval generator in this review. |
| **TASTE** | Generated tau2-style tasks and $\tau^c$-Bench | Evaluation | Gold-action replay induces final state; rule checks plus hint-assisted solver during construction | Code and artifacts visible but current license is review/reproducibility-only | **Borrow method only:** sequence-first coverage, medoid selection, validity before difficulty, decoy-state evolution. |
| **AgentAbstain / AbstainGen** | 263 should-act/should-abstain pairs in 42 MCP sandboxes | Evaluation | Critical-action commit check plus LLM terminal-response judge | Benchmark code MIT and data CC BY 4.0; generator intentionally withheld | **Borrow paired single-$\delta$ design and critical-action evidence.** Do not claim a public regenerating pipeline. |
| **ToolMaze** | DAG tool tasks with explicit/implicit and transient/permanent faults | Evaluation | Expected-tool/count coverage plus perturbation-specific stop/retry rules; optional LLM judge | Runtime and data public; repository code license not clear | **Borrow 2×2 fault cells and recovery-path accounting.** Strengthen verifier to state/result evidence. |
| **ToolBench-X** | Executable tasks under five reliability hazards | Evaluation | Deterministic tools and canonical final answers | Paper/data public; code/data terms require file-level audit | **Borrow hazard taxonomy.** Do not use the placeholder citation or unverified license claims in the current audit. |
| **RandomWorld** | Type-guided synthetic tools, goal states, and trajectories | SFT/RL plus a held-out synthetic test | Exact goal-state submission | Code public but no root license found | Borrow type system, trajectory skeletons, distractor tools, and length curriculum; methodology only pending license. |
| **APIGen-MT** | Executable blueprints followed by 5,000 simulated multi-turn trajectories | Training | Policy unit tests, state/output checks, LLM committee, then trajectory reward | Dataset CC BY-NC 4.0; generator not released | Borrow blueprint-first generation and reverse recombination; do not treat as an eval generator. |
| **DIVE** | Evidence-first tasks and SFT/RL data from executed real tools | Training plus in-distribution DIVE-Eval | Released verifier is Claude judgment against a reference answer | Repo lacks clear license | Borrow execute-before-question grounding; unsuitable as objective Harbor grading. |
| **TOUCAN** | 1.5M trajectories over real MCP servers | Training | Rule and LLM quality filters during collection | Code MIT; remote MCP dependencies | Borrow MCP schema ingestion and deliberately irrelevant/unsolvable query cases, not graders. |
| **SyntheticAgentTraceQA** | Execution-first traces, then natural-language tasks and reference answers | Training supervision | Runtime validation during generation; downstream overlap/numeric metrics | Code/data unreleased as of review | Borrow operational-DAG → bind → execute → verbalize order; no implementation reuse. |
| **AWM** | SQLite-backed MCP environments, tasks, and generated verifiers | RL training | Recommended mode combines SQL probes with LLM judgment; optional code-only verifier exists | Public pipeline/data; no clear repository license | Borrow scenario → DB → MCP → verifier staging and reset contract. Require code-only verification for Eval Lab admission. |
| **TRACE** | Capability-targeted synthetic training environments | Training/self-improvement | LLM-derived `NA/PRESENT/LACKING` labels; contrastive failure gap; deterministic environment rewards | Public MIT pipeline | Borrow deficit prioritization as a hypothesis generator, not as capability ground truth. |
| **AReaL-SEA** | Multi-turn training trajectories, DB snapshots, per-instance checkers | SFT/RL | LLM task/trajectory validation; final-state binary reward for RL | Checkers/data public; synthesis engine not released in cited example path | Borrow DB snapshot + per-instance checker contract; do not claim a public self-evolving generator. |
| **SPADE** | Programmatic Python MDPs generated through self-play | Training/self-play | Executable environment reward; hint-based regret and target win-rate band | Public MIT training code | Borrow difficulty/frontier controller after Eval Lab has valid tasks; not an eval validity method. |
| **E-Bench** | 323 state-changing tasks across three synthetic products | Evaluation | Deterministic pre/post database-state differences | Paper only; authors withhold environments/tasks | Clean pattern: privileged generator writes state, solver gets reduced tools/information, verifier checks state diff. Reimplement ideas only. |
| **ScaleEnv** | Stateful RL environments and hidden-user-intent tasks | Training | Executed seed chain produces ground-truth state; rule-based column matcher | Paper only; no official code found | Borrow seed-chain-as-code and typed state matcher only. |

### Directly reusable method stack

Use these methods together, but preserve their different epistemic roles:

1. **Task topology:** FuncBenchGen hidden DAGs and connected/disconnected distractors.
2. **Coverage:** TASTE action-sequence n-grams, weighted edit distance, medoid selection, and validity-before-difficulty staging.
3. **Paired causal control:** AgentAbstain should-act/should-abstain pairs differing by one perturbation to instruction, tool, or environment state.
4. **Recovery cells:** ToolMaze explicit/implicit × transient/permanent faults; ToolBench-X hazard classes.
5. **State oracle:** E-Bench/ScaleEnv privileged execution to produce a ground-truth state; AWM's database-backed environment and reset pattern.
6. **Failure prioritization:** TRACE's contrastive gap between successful and failed trajectories. Treat LLM-produced capability labels as reviewable hypotheses.
7. **Difficulty targeting:** SPADE hint-regret or a model pass-rate band, applied only after validity and construct alignment pass.

Do not combine all seven in one first system. The first campaign should exercise the existing Function-DAG generator, then add one perturbation family at a time.

## Source and identity corrections applied to the local audit

`research/synthetic/LEGAL_AND_METHODOLOGY_AUDIT.md` now records the corrections identified during this review:

1. **FuncBenchGen and ToolMaze have distinct arXiv identifiers.**
   - FuncBenchGen: `arXiv:2509.26553`, first released in 2025, revised 2026, accepted at ICLR 2026.
   - ToolMaze / *When Tools Fail*: `arXiv:2606.05806`.
2. **ToolBench-X is `arXiv:2606.25819`.** The paper is *Beyond Function Calling: Benchmarking Tool-Using Agents under Tool-Environment Unreliability*.
3. **Paper and repository licenses differ.** TASTE's arXiv paper is CC BY 4.0; the repository code/data license is review-only and forbids redistribution. Paper-level ideas may be cited; code/artifacts must not be copied.
4. **"Execution-based" must name executed evidence.** Static metadata, required-evidence declarations, or expected-behavior text are not oracle runs, no-op trials, or mutation tests.
5. **Training artifacts are not evaluation artifacts.** APIGen-MT, DIVE, TOUCAN, AWM, TRACE, AReaL-SEA, SPADE, ASTRA, and EnvFactory need independent held-out task construction and graders before Eval Lab can call their outputs evaluations.

## Current Eval Lab state: implemented versus evidenced

### Implemented

- `synthetic_contracts.py`: `SyntheticEvalSpec`, lineage, paired lineage, certificate, and behavior-episode contracts.
- `synthetic_funcdag.py`: seeded typed DAG generation, deterministic execution, difficulty parameters, distractors, and Harbor package materialization.
- `synthetic_transform.py`: seeded tool-fault, epistemic-restraint, and context-pressure transforms over base task directories.
- `synthetic_cert.py`: an eight-check audit API that now fails when reset, oracle, no-op, mutant, or regeneration runners/records are absent.
- `synthetic_projections.py` and `synthetic_report.py`: typed projections and aggregate reporting.
- `trajectory_sequence.py`: trial-isolated chronological action sequences, transition/motif extraction, strict PyArrow schemas, and atomic deterministic Parquet projections.
- Three experimental Function-DAG packages—easy, medium, and hard—with exact-output verifiers and focused pass/fail tests.

### Not yet evidenced in the repository

- The specified 12-task `SG-FDAG-001` campaign across three seeds and four matched conditions.
- Persisted execution records and content digests supporting every claim in the three checked-in experimental certificate sidecars. Each sidecar is explicitly `experimental`; its named `output/result.json` is not tracked with the package.
- A paired model cohort establishing discrimination, avoiding flat floors/ceilings, or isolating a capability effect.
- Sequence/motif projections computed from the synthetic campaign's actual ATIF trajectories.
- A closed-loop generator update supported by held-out evidence.

### Certification gate status

The fail-open code paths identified in the original review have been removed:

- `check_clean_reset()` requires a reset function and executes it twice.
- `check_oracle_3x()` requires an oracle runner or at least three oracle execution records.
- `check_nop_failed()` requires a no-op runner or no-op execution records.
- `check_mutants()` requires at least three supplied runners or records and rejects every mutant.
- `check_regeneration_idempotency()` requires a regenerator and compares two runs with the spec digests.

This repairs the admission logic, not historical evidence. Existing experimental certificates must be regenerated from persisted execution records before they are treated as execution-backed.

## Build program

### M0 — fail-closed gate landed; migrate artifacts

**Goal:** make checked-in claims and certificates traceable to durable evidence.

Completed:

- Corrected the source-identity issues above and recorded paper licenses separately from code/data licenses.
- Made missing oracle, no-op, reset, regeneration, and mutant evidence fail certification.
- Added focused tests proving metadata-only inputs are rejected.

Remaining:

- Persist execution records and content digests for each certified control.
- Regenerate the experimental Function-DAG certificates from those records rather than relying on untracked `output/result.json` paths.
- Re-run the packages under a clean reset and preserve the resulting evidence.

### M1 — complete the Function-DAG evaluation campaign

**Goal:** move from three materialized experimental packages to a controlled, evidenced campaign.

Status: easy, medium, and hard packages exist and their exact-output verifiers have focused pass/fail coverage. The matched 12-task design and model cohort below are not yet present as repository evidence.

Use `synthetic_funcdag.py`; do not add a generic `SyntheticEngine` abstraction yet. Existing generation paths have not demonstrated a repeated orchestration need.

Campaign `SG-FDAG-001`:

- Fix `width=1` and vary `depth` over `{2, 4, 8}` with no distractors.
- Add one matched condition at `depth=4`, `width=1` with 10 connected distractors.
- Interpret the first contrast as required-chain length, not a pure depth effect: the current generator has `depth × width` functional nodes and cannot hold node count fixed while changing depth.
- Generate three seeds per condition: 12 tasks.
- Run real oracle/no-op/mutant controls locally.
- After control admission and explicit model-run approval, pilot two economical models with repeated trials.

Primary measurements:

- task/control admission rate;
- success by dependency depth and connected-distractor condition;
- exact target correctness;
- wrong/stale argument propagation;
- redundant or disconnected calls;
- minimum path length versus observed tool calls;
- regeneration digest equality.

Acceptance for moving beyond the pilot:

- every admitted task has executed evidence;
- at least two depth conditions differ enough to avoid a flat floor or ceiling in the pilot cohort;
- failures are attributable from tool traces rather than only final answers;
- results reproduce on a second seed batch.

### M2 — add paired recovery generation over real stateful tasks

**Goal:** move from abstract DAG reasoning to controlled capability perturbations.

Campaign `SG-REC-001`:

- Select three existing Harbor tasks with deterministic final-state verifiers and clean resets.
- For one fixed critical tool per task, generate clean baseline plus ToolMaze's four cells: explicit/transient, explicit/permanent, implicit/transient, implicit/permanent.
- Keep task instruction, target state, and non-fault tools fixed.
- Record whether a valid retry, fallback, verification, or alternate path exists.

Likely files:

- `src/evallab/synthetic_transform.py`
- `src/evallab/synthetic_contracts.py`
- `src/evallab/synthetic_projections.py`
- existing Harbor workbench/runner integration

Primary measurements:

- paired success delta relative to clean baseline;
- fault detection latency;
- retry count and repeated-failure loops;
- alternate-path use;
- corrupted-output verification before commit;
- final-state correctness and collateral mutations.

Do not add context pressure or abstention in this campaign. One capability family per experiment preserves attribution.

### M3 — coverage, difficulty, and generator proposals

**Goal:** choose what to generate next from evidence without creating an uncontrolled self-modifying benchmark loop.

Use the merged `trajectory_sequence.py` extraction and projection contracts, then add campaign-specific reporting rather than another sequence representation:

- action-sequence n-gram coverage and weighted edit distance;
- DAG depth/width/motif and distractor coverage;
- perturbation-cell coverage;
- acceptance, oracle, no-op, mutation, leakage, and regeneration rates;
- model pass-rate bands and paired effect estimates;
- success/failure contrastive capability labels with cited trajectories and human review status.

The analysis agent may propose a new operator or new parameter weights. A human must approve the proposal; generated candidates must repeat M0 controls. Never train the generator directly on the held-out test partition.

## Research questions for the Eval Lab

1. **Coverage:** Does sequence/DAG-first generation increase procedural coverage relative to the seed task bank without lowering runtime validity?
2. **Construct isolation:** Do paired perturbations change the intended behavior while preserving all non-target task semantics?
3. **Verifier quality:** Which plausible wrong strategies escape the verifier, and does automated mutation testing reduce those false positives?
4. **Difficulty:** Which structural knobs produce a useful capability gradient rather than model-specific brittleness?
5. **Transfer:** Do failure categories observed on abstract Function-DAG tasks predict failures on stateful Harbor tasks? This requires measurement; it is not established by FuncBenchGen.
6. **Freshness:** Does eval-time generation reduce instance memorization while retaining stable aggregate difficulty? Structural generator leakage remains possible and must be tested separately.
7. **Trajectory value:** Which trajectory facts—state transitions, argument flow, recovery latency, verification actions—predict paired task success better than generic tool-call counts?

## Recommended paper/repository reading order

1. **FuncBenchGen** — direct released predecessor for controllable synthetic evaluation:  
   Paper: https://arxiv.org/abs/2509.26553  
   Repo: https://github.com/megagonlabs/FuncBenchGen
2. **TASTE** — sequence-first coverage and validity-before-difficulty:  
   Paper: https://arxiv.org/abs/2605.28556  
   Repo, methodology only under current terms: https://github.com/tomerkeren42/TASTE-task-synthesis-from-tool-sequence-evolution
3. **AgentAbstain / AbstainGen** — paired causal design and critical-action evidence:  
   Paper: https://arxiv.org/abs/2607.10059  
   Repo: https://github.com/AntiQuality/agentabstain
4. **ToolMaze** — recovery perturbation cells and replanning metrics:  
   Paper: https://arxiv.org/abs/2606.05806  
   Repo: https://github.com/Zhudongsheng75/ToolMaze
5. **ToolBench-X** — structured tool-environment hazards:  
   Paper: https://arxiv.org/abs/2606.25819
6. **TRACE** — failure/success contrast and difficulty calibration, training-first:  
   Paper: https://arxiv.org/abs/2604.05336  
   Repo: https://github.com/ScalingIntelligence/TRACE
7. **Agent World Model** — database-backed MCP environment/verifier synthesis, training-first:  
   Paper: https://arxiv.org/abs/2602.10090  
   Repo: https://github.com/Snowflake-Labs/agent-world-model
8. **SPADE** — hint-regret difficulty frontier, training-first:  
   Paper: https://arxiv.org/abs/2608.19197  
   Repo: https://github.com/spade-rl/spade
9. **AReaL-SEA** — DB snapshots and per-instance executable checkers, training-first:  
   Paper: https://arxiv.org/abs/2601.22607  
   Released trainer path: https://github.com/inclusionAI/AReaL/tree/main/examples/tau2
10. **Execution-first data methods** — useful ordering, not Eval Lab graders:  
    DIVE: https://arxiv.org/abs/2603.11076  
    RandomWorld: https://arxiv.org/abs/2506.11045  
    SyntheticAgentTraceQA: https://arxiv.org/abs/2607.29175  
    TOUCAN: https://arxiv.org/abs/2510.01179

## Position

The underbuilt part of the field is not synthetic agent data. It is **refreshable executable evaluations with hardened, provenance-bearing verifiers and controlled capability factors**. That conclusion is bounded to the reviewed systems: most generate training environments or trajectories; the smaller evaluation-first set is either abstract (FuncBenchGen), restrictively licensed (TASTE), generator-withheld/hybrid-judged (AgentAbstain), or benchmark-specific (ToolMaze and ToolBench-X).

Eval Lab now has a fail-closed certification gate, three experimental Function-DAG packages, and trial-isolated sequence/motif projections. The next action is to attach durable control records to those packages, complete the 12-task Function-DAG campaign, run an approved model cohort, and project its actual trajectories through `trajectory_sequence.py`. Only that evidence should determine the next perturbation family.
