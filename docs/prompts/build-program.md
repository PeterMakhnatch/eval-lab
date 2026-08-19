---
status: living
audience:
  - builder
  - runner
  - operator
---

# Build program — data truth first, then absorb, then generate

The forward program, superseding ad-hoc mission picking. Direction from
Peter (2026-08-19): build NEW capability, not more convergence polish —
specifically (1) analysis of trajectories and solid data ingestion BEFORE
more eval-building, (2) absorb existing best practices of generating evals
with agents, then iterate on them, (3) one OMP orchestrator dispatches
subagents; Peter reads surfaces.

## State at writing (verified, not asserted)

Night-loops M015–M019: complete and merged — audit ledger live
(`research/audits/`), `docs/STATUS.md` generating, five real cards in
`research/cards/`, lessons gated, property tests landed. M020–M024 merged
(parallel dispatch leases, CLI registry, analyst-memory in LanceDB, craft
batching, tidy). Open PRs: none. Worktrees: none. **Nothing blocks this
program.** The only ordering constraints are internal and listed per phase.

## Orchestration model

ONE orchestrator (the standing OMP session). It registers each loop as an
M-mission on the board, spawns one subagent per mission (OMP-native
subagents or herdr lanes — orchestrator's choice), owns integration and
merge, and keeps `ACTIVE.md` honest. Subagent quota note: prefer codex/
OMP-native lanes while the antigravity lane recovers. Peter's interface:
this doc in, `docs/STATUS.md` + cards + digest out.

Standing rules unchanged: cycle protocol from `docs/prompts/night-loops.md`;
subscriptions only; quota metering (#64); GATE (#65) for billable-class;
registry promotion human-only; leases disjoint or handshake via board-note.

---

## Phase 1 — DATA TRUTH (dispatch all three now)

Rationale: every later claim stands on trials being fully captured and
honestly analyzable. This phase turns "we run Harbor tasks" into "we run
Harbor tasks, nothing is lost, and every trajectory is analyzable within a
day."

### LOOP-INGEST — completeness as an invariant

**Lease:** `src/evallab/ingest_verify.py` (new; or extend `facts.py` if the
integrator prefers — declare in the board row), `tests/test_ingest_verify*`,
`sql/ingest_views.sql`.

**Cycles:**
1. `evallab ingest verify` — counts and reconciles: trial dirs on disk
   (runs/, queue/researchers/passes/, canary outputs, harbor-practice
   migrated evidence) vs. catalog rows vs. Parquet rows vs. ATIF index.
   Output: a gap table (missing where, counts, examples), exit nonzero on
   gaps. This command is the phase's centerpiece.
2. Backfill the gaps it finds, through existing ingest paths only (no
   parallel ingest logic); re-run verify to zero.
3. Idempotence property test: re-ingesting an already-ingested trial dir
   changes nothing (row counts + digests stable).
4. Wire verify into nightly + STATUS ("data completeness: N trials, 0
   gaps"); storm-alarm if gaps appear.
5. Contract doc: one page in `docs/` stating what "fully ingested" means
   (the four stores and their join keys), generated numbers cited.

**Acceptance:** verify runs clean on the whole history; nightly carries it;
a deliberately-hidden fixture gap is caught in tests.

### LOOP-TRAJ — trajectory analysis as a capability

Purpose: Peter's direction verbatim — analysis first. Make every
trajectory condensable, feature-extracted, and labelable, with the human
protocol staying the calibration source.

**Lease:** `src/evallab/traj.py` (new), `atif.py` (extend, additive),
`tests/test_traj*`, `sql/traj_views.sql`, `derived/parquet/traj_features/`.
Lessons join goes via board-note to the LESSONS owner, not by editing
`lessons.py`.

**Cycles:**
1. `evallab traj outline <trial>` — deterministic condensation of ATIF to
   a readable step outline (phase markers, tool calls, errors, tokens/step).
   This is also what Peter's daily reading consumes.
2. Mechanical features to Parquet per trial: steps, tool mix, error count,
   recovery-after-error count, loop-suspicion heuristic (repeated
   near-identical commands), token/cost curve shape, time-to-first-edit.
   DuckDB view + golden test.
3. `evallab traj queue` — emits the day's 3-trajectory reading list for
   Peter (prioritize: real agent trials, unlabeled, family-diverse) and
   records his labels (`traj label <trial> <taxonomy> --note`) into the
   catalog next to any machine labels. Human labels are ground truth.
4. Heuristic label proposals (deterministic only in this phase): rule-based
   taxonomy suggestions (e.g., zero-edit run → likely planning/harness;
   loop-suspicion → tool_use). Stored as `proposed_by=heuristic`, never
   overwriting human labels. Precision measured against Peter's existing
   labels in the calibration corpus.
5. Model-assisted labeling — DEFERRED behind LOOP-SEAM's adapter; specify
   the callable signature now, wire when SEAM cycle 2 lands.

**Acceptance:** outline + features exist for 100% of ingested real trials
(joins LOOP-INGEST); reading queue works; heuristic precision report
against the human-labeled corpus committed.

### LOOP-SEAM — as specified in `docs/prompts/context-loops.md`

Unchanged. Phase-1 resident because TRAJ cycle 5 and all model-assisted
analysis depend on it. Cycles 1–2 are the priority (adapter + analyst
wiring, one real trial analyzed end-to-end through the codex lane).

---

## Phase 2 — ABSORB (start when any Phase-1 mission reaches cycle 3)

### LOOP-STANDARDS — as specified in `context-loops.md`, plus one cycle

Add cycle: **EX-SMITH** — SWE-smith (github.com/SWE-bench/SWE-smith):
extract their task-generation pipeline stages and validation gates from the
CODE into `library/curated/standards/swe-smith/pipeline.md`; produce a
one-page gate-comparison table (their gates vs. our battery) ending in
adopt/skip verdicts per gate. This is the "absorb existing best practices
of generating evals with agents" item, made concrete alongside Meta-Task
(EX-MT), Terminal-Bench (EX-TB), METR (EX-METR), TOFFEE (EX-TOFFEE).

**Iterate-and-improve mechanism (the standing rule, not a cycle):** every
adapted template in `_proposed_templates/` carries a version and a
changelog; every battery/A-B result that used it appends one evidence line
(template@v, arm, battery pass, review score) to
`research/registration/qualification-ledger.parquet` notes. Template
version N+1 must cite the evidence rows that motivated the change. That is
how "absorb → iterate → improve" stays measurable instead of vibes.

---

## Phase 3 — GENERATE & MEASURE (start when STANDARDS EX-MT + TRAJ c1–2 landed)

### LOOP-EXPERIENCE — as specified in `context-loops.md`

Now strengthened by Phase 1: the experience packs' trajectory halves come
from `traj outline` (deterministic, tested) instead of ad-hoc condensation,
and family selection can use `traj_features` (e.g., pick families with both
passes and instructive failures on record).

**Program KPI (orchestrator maintains in STATUS):** battery pass-rate per
seed_class per template version, with raw counts. The program is working
iff that table gains rows and later template versions don't regress. This
is the number "are we getting better at generating evals" reduces to.

---

## Consolidated lease map (active program)

| Mission | Writes |
|---|---|
| INGEST | ingest_verify.py, sql/ingest_views.sql, its tests |
| TRAJ | traj.py, atif.py (additive), sql/traj_views.sql, traj_features/, its tests |
| SEAM | modeladapter.py, analyst/analysis_worker injection points, its tests |
| STANDARDS | library/curated/standards/**, contextpack audience wiring, its tests |
| EXPERIENCE | research/experience/**, experience.py, research/cards/experience-*.md, its tests |
| (continuing, low priority) AUDIT weekly; FUZZ as capacity | research/audits/**; tests/test_*_properties.py |

Shared-file handshakes (board-note, never direct): authoring.py (SG),
lessons.py (LESSONS owner), digest/status sections (SURFACE owner),
cli.py registry entries (append-only per M021's registry pattern).

## Dispatch directive (paste to the orchestrator verbatim)

> Read docs/prompts/build-program.md. Register Phase-1 missions (INGEST,
> TRAJ, SEAM) on the board now with the leases as written and dispatch one
> subagent each; register STANDARDS and EXPERIENCE as ready, gated on the
> phase conditions in the doc. Night-loop continuations (AUDIT weekly,
> FUZZ) run at low priority. Keep ACTIVE.md and STATUS.md honest; quota
> note: codex/OMP-native lanes until the antigravity lane recovers.

## Changelog

- 2026-08-19 — v1: three-phase program (DATA TRUTH → ABSORB → GENERATE),
  written with the board empty after M015–M024 all merged.
