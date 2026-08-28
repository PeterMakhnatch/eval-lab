---
status: historical
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Build plan (spec)

Status: living. Six workstreams. Each entry: files, data model, CLI, data
flow, dependencies, acceptance (runnable). Strategy background:
path-forward-2026-08.md. Consumers: the Integrator board (mission briefs
should copy the relevant workstream section verbatim) and Peter.

Target restated as a system requirement: the platform analyzes agent
performance on Harbor-format evals AND analyzes the evals themselves as
structured data, so builder agents consume that data to author non-trivial
tasks; the whole loop runs unattended under policy.

---

## WS-A — CRAFT: task-corpus analyzer

Purpose: make eval design patterns queryable. Input corpus already on disk:
76 TB3 tasks (~/Developer/agent-evals/terminal-bench/tasks), library/
(registered, curated, benchmarks), frontier-bench.

- **Files:** `src/evallab/craft.py`, `tests/test_craft.py`,
  `sql/craft_views.sql`
- **Data model:** `CraftRecord` (pydantic → Parquet at
  `derived/parquet/craft/`):
  `task_ref, source_repo, version, task_digest, instruction_chars,
  instruction_style: imperative|narrative|spec, env_n_files,
  env_languages[], env_services_n, env_multi_container: bool,
  verifier_type: pytest|diff|golden_file|judge|hybrid,
  anti_cheat[]: {hidden_tests, answer_outside_image, digest_check,
  process_check}, answer_hiding: str, difficulty_mechanism:
  conceptual|clerical|volume|mixed, human_minutes: int|null,
  pinned_deps: bool, facets_schema_version`
- **CLI:**
  - `evallab craft scan <dir>|--all-local|--tb3` — deterministic
    extraction only (file inventory, verifier detection by AST/heuristics,
    instruction metrics). No model calls.
  - `evallab craft classify <task_ref>` — LLM facet pass for fields
    heuristics can't fill; submits via queue, `purpose=craft`,
    GATE-authorized, cheap model.
  - `evallab craft patterns` — regenerates
    `research/craft/{verifier-cookbook,instruction-guide,anticheat-catalog}.md`
    from Parquet; every entry cites ≥1 real task file path.
- **Flow:** task dirs → scan → Parquet ⟶ classify (queue) → Parquet update
  ⟶ patterns → generated markdown. All generated files carry
  `generated-by: craft vN` headers; hand-editing them is a CI failure.
- **Deps:** queue, GATE (#65), Parquet layout.
- **Acceptance:** `evallab craft scan --tb3` emits ≥76 records;
  `SELECT verifier_type, count(*) FROM craft GROUP BY 1` returns in DuckDB;
  three pattern files exist with ≥10 cited exemplars each; re-scan is
  idempotent (same digests → no row churn).

## WS-B — CONTEXT: pack compiler

Purpose: context engineering as code. Agents get a compiled, deterministic
context bundle per mission type instead of an unbounded docs/ crawl.

- **Files:** `src/evallab/contextpack.py`, front-matter added to docs/,
  `tests/test_contextpack.py`
- **Data model:** doc front-matter
  `{status: living|historical, audience: [builder|analyst|runner|operator]}`
  (status field already mandated by the doc lifecycle).
- **CLI:** `evallab context build <mission_type> [--task REF] [-o out.md]`
- **Flow:** select docs where `status=living AND mission_type ∈ audience`
  → append WS-A patterns filtered by the target task's facets → append
  WS-D lessons matching those facets → append mission brief → emit single
  file + content hash. Deterministic: same repo state ⇒ same hash.
- **Deps:** WS-A (patterns), WS-D (lessons; optional at first), doc
  front-matter sweep.
- **Acceptance:** two consecutive `context build builder` runs produce
  identical hashes; two board briefs reference packs instead of doc lists;
  pack for `--task <registered>` includes only facet-relevant patterns.

## WS-C — BUILDER: authoring pipeline

Purpose: agent-authored evals as a measured pipeline with a human-only
promotion gate. Extends the existing workbench + `registry` CLI.

- **Files:** `src/evallab/authoring.py`, `library/tasks/_proposed/`
  (quarantine), `research/registration/qualification-ledger.parquet`
- **State machine per proposal:**
  `proposed → battery_passed → craft_reviewed → registered | rejected`
  (registered only via existing `evallab registry` — human-only,
  automation-refusing).
- **CLI:**
  - `evallab author propose --seed mutation|scenario|craft-gap
    [--ref TASK]` — mutation: new version of a registered task, never
    in-place; scenario: from research/ scenario material; craft-gap: WS-A
    query finds an unexercised facet combination and seeds a spec for it.
  - `evallab author battery <proposal>` — oracle=1.0, nop=0.0 (n≤2),
    fair-oracle (fresh agent, instruction+env only), adversarial
    (cheat-instructed agent scores 0). Free/gated runs via queue.
  - `evallab author review <proposal>` — rubric scorer generated from
    WS-A patterns; writes score + reasons to the ledger.
- **Ledger columns:** proposal_id, seed_class, ref_task, battery results
  (4 bools + evidence paths), review_score, outcome, timestamps.
- **Deps:** WS-A (rubric), WS-B (builder pack), workbench, registry, GATE.
- **Acceptance:** a 5-proposal batch runs propose→battery→review
  unattended and halts at the human gate; pass-rate per seed_class is one
  DuckDB query; ≥1 proposal registered by Peter; ≥1 flagged TB4-candidate.

## WS-D — LESSONS: aggregation views

Purpose: convert per-trial observations + failure taxonomy + craft facets
into aggregate, statistically-gated findings.

- **Files:** `sql/lessons.sql`, digest section hook, generated
  `research/lessons.md`
- **Views:** `v_failure_by_facet` (craft ⋈ trials ⋈ analysis sidecars),
  `v_loop_rate_by_env` (observation records ⋈ craft), 
  `v_outcome_by_verifier_type`.
- **Rules:** every emitted lesson row carries n and a cohort.py interval;
  rows under the power threshold render as "insufficient n", never as
  findings. lessons.md is generated; hand-edits fail CI.
- **Deps:** WS-A, observation records (27 and growing), TRUTH.
- **Acceptance:** three views return rows on current data; nightly digest
  gains a lessons section; regeneration from a clean clone is identical.

## WS-E — SPINE: finish autonomy (consolidation, no new scope)

Ordered backlog, each item one small PR:

1. `ExperimentSpec.purpose: baseline|comparison|elicitation|drift|
   calibration|craft|practice` (required) + dispatch-time rejection of
   purposeless specs.
2. `evallab preflight` — prints per-provider remaining quota (#64 data),
   queue grouped by purpose, and power warnings; runs at tick start;
   embedded in digest.
3. LADDER generator — `evallab ladder generate` expands a declared grid
   (task × agent × preamble × k) into specs under per-provider quotas.
4. Parquet compaction in nightly — closed days →
   `derived/parquet/compact/dt=YYYY-MM-DD/`, per-trial retained 7 days;
   scan-latency wired to perf budgets.
5. Storm alarm — >N same `reason_code` events/hour ⇒ digest banner +
   STATUS flag.
6. `STATUS.md` generator (CI post-merge + nightly): merged today, suite
   health, quota spend, open decisions.
7. `docs/INDEX.md` generator + archive sweep (front-matter driven).

Acceptance: three consecutive zero-intervention nights; STATUS.md answers
"what happened yesterday" with no terminal.

## WS-F — QUALITY: mechanical engineering bar

- `tests/test_queue_properties.py` — hypothesis-based fuzz of the queue
  state machine (10k random op sequences; invariants: no spec lost, no
  double-dispatch, quota never exceeded). **Only new dependency in this
  plan: `hypothesis`.**
- Golden-file tests for digest and STATUS rendering.
- Repo-local Claude Code skills in `.claude/skills/`: `lab-status`
  (three-surface summary), `mission-launch` (brief + pack + board entry),
  `review` (PR + handoff + checks in one view).
- Acceptance: fuzz green in CI; three skills invocable; golden files stable
  across two regenerations.

## Dependency graph and order

```
WS-E (independent, mostly landed) ──────────────┐
WS-A ──→ WS-B ──→ WS-C                          │ nightly substrate
   └───→ WS-D ──→ (feeds WS-B packs, WS-C rubric)
WS-F: continuous, applies to all
```

Execution order: E items 1–2 immediately (they gate next week's runs),
then A (pure local analysis, cheap), B ∥ D, C last, F threaded. E items
3–7 interleave as board capacity allows.

Post-training runway (out of scope, for orientation only): WS-A/C/D outputs
(facet-annotated corpus, qualified tasks, verified-reward trajectories) are
the direct inputs of a later slime/verl-class stack. No work item above
changes when that day comes.

## Changelog

- 2026-08-16 — v2: rewritten as an engineering spec (files/models/CLI/flow/
  acceptance per workstream) at Peter's direction.
- 2026-08-16 — v1: initial six-area prose version.
