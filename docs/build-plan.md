# Build plan — the living "what to build next"

Status: living (per the doc lifecycle in architecture-review-2026-08-16).
Strategy context lives in path-forward-2026-08.md; this doc is the concrete
build areas, how to build them, with what tools, and what each improves.
Target restated: a research platform where agents' performance on
Harbor-format evals is analyzed so deeply that **builder agents learn eval
craft from the data** — best practices, common failures, the implementation
patterns of best-in-class evals — and produce non-trivial evals of their
own, on a platform capable of autonomous operation, engineered to a high
bar. Post-training (slime/verl-class tooling) is explicitly downstream; this
plan builds the foundation those tools would consume.

As of 2026-08-16: quota metering (#64), paid-run authorization gate (#65),
registry CLI, 27 observation records, 76 TB3 tasks cloned locally, TRUTH
statistics, analysis worker, board-driven Integrator. The six areas below
build on exactly that.

## A. CRAFT — the eval-pattern corpus (the new centerpiece)

**What.** Today the lab studies *agent runs*. The target requires studying
*evals themselves* as data. CRAFT decomposes every best-in-class task —
76 TB3 tasks locally, Hub datasets, frontier-bench, own registered tasks —
into structured facets: instruction style and length; environment complexity
(files, services, languages, multi-container); verifier type (pytest / diff
/ golden-file / judge / hybrid) and its anti-cheat measures; how answers are
hidden; difficulty mechanism (per the TB3 rubric taxonomy: conceptual vs
clerical vs volume); pinning discipline; human-time anchors where stated.

**How.** `src/evallab/craft.py` + a `CraftRecord` pydantic model → Parquet
alongside existing facts; deterministic extraction first (file inventories,
verifier AST/type detection, instruction token counts), LLM-assisted facet
classification second — through the queue, purpose=`craft`, cheap models,
GATE-authorized. Outputs: `research/craft/` cards per task family, plus
**generated pattern digests**: "verifier techniques cookbook,"
"instruction style guide," "anti-cheat catalogue" — each entry citing real
task files as exemplars.

**Tools.** Nothing new: Python, DuckDB, the queue. **Improves.** Turns
FOUNDRY/BUILDER from "generate and hope" into "generate from measured
patterns"; gives Peter the eval-craft education he wants without
hand-reading 76 tasks; directly raises TB4 submission quality. **Done
when.** Every registered + TB3-local task has a CraftRecord; the three
pattern digests exist and cite ≥10 exemplars each; one facet distribution
("what do TB3 verifiers actually do") is queryable in DuckDB.

## B. CONTEXT — the context-pack compiler

**What.** Context engineering as a subsystem, not a habit. Agents currently
read an ever-growing docs/ pile; quality varies with what they happened to
read. A context pack is a *compiled, mission-scoped bundle*: the relevant
living docs slice + the CRAFT patterns for the task at hand + prior
failures/verdicts that match (from observations and DISCOVERIES) + the
mission brief — one file, generated.

**How.** Front-matter tags on docs (`status: living|historical`,
`audience: builder|runner|analyst|operator`) — the lifecycle policy already
requires the status half. `evallab context <mission-type> [--task <t>]`
assembles the pack deterministically (tag selectors now; retrieval later
only if selectors fail). Board briefs reference packs instead of raw doc
lists. **Tools.** None new. **Improves.** Kills context pollution
structurally; makes agent quality reproducible; cuts every mission's
startup tokens. **Done when.** Two mission types (builder, analyst) launch
from packs only, and a pack regenerates identically from a clean clone.

## C. BUILDER — eval authoring as a measured pipeline

**What.** The FOUNDRY sketch productized, now powered by A and B: propose →
draft in `_proposed/` quarantine → battery (oracle 1.0 / nop 0.0 /
fair-oracle / adversarial — the workbench, widened) → **craft-review**
(rubric derived from CRAFT patterns, scored) → human `registry` action.
Three seed classes, all owned in unlimited supply: mutation of registered
tasks (new version, never in-place), scenario-derived tasks (the
calibration-corpus method), and **craft-gap seeds** ("no registered task
exercises verifier pattern X") — the last one is what makes extensions
non-trivial rather than cosmetic.

**How.** Extends existing workbench + registry; adds the craft-review
scorer and a qualification ledger. The pipeline's own metric — battery
pass-rate and reviewer grade per generation batch — is the platform's S1
progress number. Harbor features RECON already demoed (multi-step tasks,
separate verifier envs, network policies) are the non-trivial extension
surface. **Tools.** None new. **Improves.** Converts "agents write evals"
from aspiration to an instrumented production line with a quality dial.
**Done when.** A batch of 5 proposals runs the full pipeline unattended up
to the human gate, with scores; ≥1 task Peter registers; ≥1 TB4 candidate.

## D. LESSONS — the analysis→learning feedback loop

**What.** The step that makes "agents learn from the data" literal: join
CRAFT facets × failure taxonomy × observation records into aggregate
lessons — "agents loop most on tasks with interactive environments,"
"fair-oracle failures cluster in judge-verified tasks." Anecdotes
(DISCOVERIES) become distributions.

**How.** DuckDB views over existing tables + observation front-matter; a
nightly `lessons` section in the digest; `research/lessons.md` generated,
never hand-written; lessons feed B's context packs and C's craft-review
rubric — closing the loop. **Tools.** None new. **Improves.** DISCOVERIES
verdicts get evidence at aggregate level; elicitation studies get
hypotheses from data instead of hunches. **Done when.** Three lessons exist
with n and intervals (TRUTH's rules apply to lessons too), each traceable
to queryable views.

## E. SPINE — autonomy consolidation (finish, don't extend)

Remaining from the reviews, unchanged in priority: purpose taxonomy +
`preflight` gate; LADDER standing backlog; Parquet nightly compaction
(clock: 1–2 weeks after real nightlies); storm alarms; generated STATUS.md;
doc INDEX + archive sweep; per-provider quotas now measurable via #64.
**Done when** three consecutive unattended nights need zero intervention
and STATUS.md answers "what happened" without opening a terminal.

## F. QUALITY — the engineering bar, made mechanical

Property-based tests (`hypothesis`) for the queue state machine — the one
component whose failure modes are combinatorial; golden-file tests for
digest/STATUS rendering; perf budgets already ratcheted (keep); and the
orchestration best practice: encode Peter's routines as Claude Code skills
(lab-status, mission-launcher, review) so operating knowledge lives in
tooling, not memory. **Tools:** `hypothesis` (the one new dependency in
this entire plan). **Done when** the state machine survives a hypothesis
fuzz of 10k operation sequences and skills replace the three most-typed
routines.

## Sequencing and the post-training runway

Order: **E finishes first** (it's mostly landed; boring nights are the
substrate), then **A** (corpus exists on disk today; pure analysis, cheap),
**B and D in parallel** (both consume A), then **C** (needs A's rubric and
B's packs), **F** threaded throughout. Every area feeds the S1–S3 stages in
path-forward: A+C are S1 (foundry at scale), D is S2's difficulty-targeting
brain, and the artifacts A–D produce — qualified tasks, verified-reward
trajectories, facet-annotated corpora — are byte-for-byte the inputs a
slime/verl-class post-training stack consumes later. Nothing here is
throwaway on the road Peter named; nothing here starts that road early.

## Changelog

- 2026-08-16 — created (Claude, at Peter's direction): six areas — CRAFT,
  CONTEXT, BUILDER, LESSONS, SPINE, QUALITY.
