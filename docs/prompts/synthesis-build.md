---
status: living
audience:
  - builder
---

# Synthesis build directive (SG missions)

Verdict first: **no restructuring.** The architecture (platform-architecture
v2) already has the seams; every paper idea lands additively in the
authoring + calibration planes. Changes are file-scoped below. Peter reads
nothing further; each mission's implementer reads exactly one required
source.

## Code change map (what happens to what you have)

| Existing file | Change | Mission |
|---|---|---|
| src/evallab/authoring.py | +meta-task packaging, +seed classes, +spec sampler hooks | SG-1..3 |
| src/evallab/schemas.py | additive: ProposalSpec axes fields, seed_class enum +inversion | SG-2/3 |
| src/evallab/calibrate.py | generalize: verifier-agreement-vs-execution metric | SG-4 |
| src/evallab/queue.py, runner.py | **unchanged** (SG-1 rides the existing executor) | — |
| task_workbench.py, registry.py | **unchanged** (battery + human gate stay the gates) | — |
| new: library/meta/ | meta-task template package (skeleton, exemplar, checker tests) | SG-1 |
| new: authoring/templates/*.yaml | category/scenario/difficulty axes data | SG-2 |
| new dep | `llm-verifier` (pip), SG-4 only, isolated extra group | SG-4 |

## SG-1 — META-LOOP: generation as a Harbor task (Meta-Task pattern)

Required reading: research/papers/meta-task-2607-27929.md (in-repo note).
Build `library/meta/synthesize-task@1/`: a Terminal-Bench-format task whose
instruction directs an agent to author ONE task package; environment =
skeleton dirs + one exemplar sampled from registered tasks + output
templates; tests/ = the completeness checker (structure, oracle-runs,
tests-pass, no answer leakage in env image). `evallab author propose
--via-harbor` assembles this package with the sampled spec injected,
submits it through the queue (purpose=craft, GATE applies), and harvests
the generated task from the job's artifacts into `_proposed/`. Generation
trajectories land in Z1/Z3 like any trial — CRAFT can study the generator.
Acceptance: one end-to-end run produces a `_proposed/` package that then
passes the existing battery; generation trajectory visible in Phoenix;
zero changes to queue.py/runner.py.

## SG-2 — SPEC-SAMPLER: dimension-decoupled specs, coverage-first

Axes as data files: category (seed from craft facet vocabulary + the local
TB corpus domains), scenario (8–10 instruction styles), difficulty (levels
with anti-pattern lists). Sampling order: (1) craft-gap query (facet
combinations with zero registered coverage) — primary; (2) random
axis product — secondary; (3) multi-phase novel-spec mode: a lightweight
meta-task designs a new (category, scenario) pair from topic seeds before
SG-1 runs it. Spec fields recorded on the Proposal for lineage.
Acceptance: sampler emits 20 specs with zero duplicates against the
qualification ledger; ≥1/3 originate from craft-gap queries.

## SG-3 — INVERSION: TOFFEE-style answer-first tasks

Required reading: github.com/wang0702/toffee (implementer only).
`seed_class=inversion`: pick a real data asset (start with data files
already inside library/ environments; later a pinned public-dataset pool
via fetch), agent probes it inside the meta-loop, computes a target value
by executing analysis code, then writes instruction backwards from the
verified answer; verifier checks the computed key. Battery unchanged.
Acceptance: 3 inversion proposals reach the human gate; each answer key
reproducible by re-executing the reference analysis.

## SG-4 — SELECTOR: best-of-N + verifier calibration (llm-as-a-verifier)

Required reading: github.com/llm-as-a-verifier/llm-as-a-verifier.
Two measurements on OUR data (execution ground truth makes both cheap):
(1) selection lift — for suite tasks with k≥3 trials, use `llm-verifier`
to pick best-of-k; report pass@1 vs selected@k vs oracle ceiling with
TRUTH intervals; (2) verifier agreement — score every trial, measure
agreement with execution reward; record as CalibrationRecords (verifier
flavor). Boundary: LLM verifiers NEVER replace execution verifiers in the
battery or registry gates — this is an instrument experiment
(purpose=calibration/elicitation), dep isolated in an extras group.
Acceptance: one eval card with both numbers; agreement metric visible in
calibration history.

## Order

SG-1 → SG-2 (sampler feeds the loop) ; SG-3 and SG-4 parallel, anytime.

## Bounded further reading (delegate, don't read)

Drone-note tier for research/papers/: SWE-smith (repo-mined task synthesis),
Meta-Task Table-1 competitors (TerminalTraj, TermiGen, Nemotron-Terminal,
CLI-Gym, SkillSynth). Peter: nothing.
