---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Eval R&D Platform — System Architecture (v1)

Status: living. This is the capital design document: the full architecture
of the platform, specified so a large team of engineers or coding agents can
build against it in parallel for days without collisions. It supersedes
`architecture.md` (retained as historical v0) and absorbs the workstreams of
`build-plan.md` into a complete system decomposition. Harbor remains the
execution engine; everything here is the laboratory around it.

## 0. Design tenets (binding)

T1  Evidence is immutable; every store above raw evidence is rebuildable.
T2  Contract-first: every component publishes typed interfaces (pydantic
    models, CLI signatures, file schemas) before implementation; parallel
    work binds to contracts, never to internals.
T3  Agents propose; one executor disposes; money/quota authority lives in
    committed policy files only.
T4  Every automated claim carries provenance (digests) and uncertainty
    (n, interval) or is labeled a draft.
T5  Generated > hand-written for any surface describing system state;
    hand-editing generated files is a CI failure.
T6  Extensibility by seams (Section 9), not by rewrites.
T7  Every role cleans up after itself: janitorial duties are part of each
    component's contract, not a separate chore.

## 1. System context

```
                         ┌────────────────────────────────────────────┐
  Peter (decisions) ───▶ │  COORDINATION PLANE (board, briefs, packs) │
                         └───────┬────────────────────────────────────┘
                                 ▼
  ┌──────────┐   specs   ┌──────────────┐  jobs  ┌─────────────────┐
  │ KNOWLEDGE │◀──feeds──│ EXPERIMENT-  │──────▶ │ EXECUTION PLANE │──▶ Harbor/Docker
  │ PLANE     │          │ ATION PLANE  │        │ (queue+executor)│
  └────▲─────┘           └──────▲───────┘        └────────┬────────┘
       │ lessons/craft          │ cards/power             │ evidence
  ┌────┴──────────────┐  ┌──────┴────────┐        ┌───────▼────────┐
  │ ANALYSIS PLANE    │◀─│ DATA PLANE    │◀───────│ EVIDENCE ZONE  │
  │ (stats, calib,    │  │ (catalog +    │ ingest │ (immutable FS) │
  │  observations)    │  │  analytics)   │        └────────────────┘
  └───────────────────┘  └──────┬────────┘
                                ▼
                     ┌────────────────────┐
                     │ SURFACES PLANE     │──▶ STATUS, digest, dashboard,
                     │ (all read-only)    │    traces, alarms
                     └────────────────────┘
  Cross-cutting: AUTHORING PLANE (task foundry), QUALITY PLANE (CI/gates),
  IDENTITY & POLICY (profiles, quotas, GATE).
```

## 2. Data architecture (normative)

### 2.1 Canonical entities and identifiers

All IDs are ULIDs unless stated. All digests are `sha256:` content digests.

| Entity | Key | Core fields (contract) |
|---|---|---|
| AgentProfile | agent_name | provider, credential_probe, default_model, daily_run_quota, enabled |
| TaskVersion | (task_ref, version) | task_digest, verifier_digest, source{repo,commit}, contamination{public_since, in_pretrain: y/n/unk, basis}, human_minutes?, craft_ref |
| Suite | (name, version) | member TaskVersion refs[], frozen_at; frozen suites are immutable |
| ExperimentSpec | spec_id | purpose∈{baseline,comparison,elicitation,drift,calibration,craft,practice}, hypothesis, question_ref, suite_ref|task_refs, agents[], k, elicitation{preamble_hash, toolset, env_overrides}, power{mdd, planned_n}, policy_rule, est_units, submitted_by |
| Job | job_id | spec_id, harbor lock digest, provider_units, status |
| Trial | trial_id | job_id, attempt_n, reward, reward_dims{}, exception_type?, tokens{in,out,cache}, duration_s |
| Trajectory | trial_id | ATIF path, session_id, parquet partition refs |
| AnalysisRecord | analysis_id | trial_id, rubric_digest, model, category, evidence[{path,step}], confidence |
| ObservationRecord | trial_id | template_version, factual fields per OBSERVATORY TEMPLATE |
| CalibrationRecord | calib_id | judge_model, rubric_digest, corpus_digest, per-criterion agreement, date |
| CraftRecord | (task_ref, facets_schema_version) | facet fields per build-plan WS-A |
| Proposal | proposal_id | seed_class, ref_task?, state∈{proposed,battery_passed,craft_reviewed,registered,rejected}, battery{4 bool + evidence}, review_score |
| Lesson | lesson_id | view_name, facet filter, n, interval, statement, first_seen |
| Verdict | (discovery_id) | status∈{accepted,rejected,needs_evidence,pending}, by, at, note |
| QueueEvent | event_id | (existing schema; append-only) |

Join spine (must always hold): `spec_id → job_id → trial_id →
{trajectory, analysis, observation}`; `task_ref@version` joins trials to
CraftRecord and Suite; `agent_name` joins to AgentProfile. Any component
breaking a spine join fails CI (`tests/test_join_spine.py`).

### 2.2 Storage zones

Aligned with the existing 4-zone provenance model; normative layout:

- **Z1 Evidence (immutable filesystem).** `runs/<job>/…` exactly as Harbor
  writes it, plus promotion area `research/evidence/`. Writes: executor
  only. Mutations: never; deletions only via `gc` with tombstone JSON
  (job_id, digests, reward summary). Content manifests with digests at
  promotion time.
- **Z2 Catalog (PostgreSQL).** Rebuildable index. Tables mirror §2.1
  entities that need relational lookup: jobs, trials, rewards, artifacts,
  analysis, calibration, quota_consumption(provider, utc_day, runs,
  tokens), registry(task versions + states), verdicts. Constraints: FKs on
  the join spine; unique (job, trial); idempotent DDL in `sql/schema.sql`.
  Views (in `sql/views.sql`): v_spine (the canonical join), v_quota_today,
  v_suite_leaderboard, lessons views. Backup: nightly `pg_dump` to
  Z1-adjacent ignored dir (exists).
- **Z3 Analytics (Parquet + DuckDB).** Layout:
  `derived/parquet/{trials,steps,tool_calls,craft,ledger}/`
  - hot: per-trial partitions `job_id=…/trial_id=…/` retained 7 days;
  - cold: nightly compaction to `compact/<table>/dt=YYYY-MM-DD/part-*.parquet`
    (one file per table per day; target file size 64–256 MB as volume
    grows).
  Access is exclusively via the **unified attach surface**:
  `evallab db attach` emits a DuckDB session with postgres_scanner (Z2) +
  Z3 globs + Z4 front-matter tables under one namespace. Every consumer
  (dashboard, agents, ad-hoc) uses this; direct path-globbing in new code
  is a review reject.
- **Z4 Knowledge (generated markdown + front-matter).** `research/**` and
  generated docs. Contract: YAML front-matter `{status, audience,
  generated_by?, schema_version}`. Machine-written files carry
  `generated_by`; CI blocks human edits to them. INDEX.md generated.
- **Z5 Coordination (board, briefs, handoffs, packs).** Append/move-only
  semantics like the queue; archived by COORD-GC on completion.

### 2.3 Data flows (producer → artifact → consumers)

| # | Flow | Producer | Artifact | Consumers |
|---|---|---|---|---|
| F1 | Dispatch | executor | QueueEvents, Job | surfaces, quota meter |
| F2 | Ingest (single path) | executor post-run + nightly reconcile | Z2 rows + Z3 hot partitions | all analysis |
| F3 | Compaction | nightly | Z3 cold partitions | DuckDB scans (perf budget guards) |
| F4 | Deterministic analysis | facts/cohort | Z2 analysis rows, Z3 | lessons, cards, dashboard |
| F5 | Model-assisted analysis | analysis worker (GATE-authorized) | AnalysisRecords (Z2+Z4 sidecars) | lessons, DISCOVERIES |
| F6 | Observation | OBSERVATORY role | ObservationRecords (Z4) | lessons, calibration of readers |
| F7 | Craft | craft scan/classify | CraftRecords (Z3), cookbooks (Z4) | authoring rubric, context packs, lessons |
| F8 | Authoring | authoring pipeline | Proposals + ledger (Z3/Z5), tasks in `_proposed/` | registry (human), suites |
| F9 | Reporting | digest/STATUS/dashboard generators | Z4 generated + UI | Peter, agents' packs |
| F10 | Verdicts | Peter (or authorized session) | Verdict rows (Z2) | researcher prompts, lessons |

Lineage rule: every artifact records the digests of its inputs; `evallab
lineage <artifact>` walks the chain (implemented over Z2 + front-matter).

## 3. Execution plane

Components: DirectoryQueue (state dirs = states; atomic moves), PolicyGate
(standing-approvals + purpose check + quota check + GATE authorization for
paid runs), Executor (sole Harbor/Docker invoker; per-trial wall-clock
watchdog; scoped container cleanup; transient-error classification with
capped backoff; crash-safe reconcile), Scheduler (launchd tick/nightly),
CredentialHub (AgentProfile probes; per-agent deferral), QuotaMeter
(consumption per provider per UTC day, from #64). Invariants fuzz-tested
(WS-F): no spec lost, no double dispatch, quota never exceeded, STOP always
honored. Extension seam: `ProviderAdapter` — adding an agent is one
AgentProfile row + probe function, zero executor changes.

## 4. Experimentation plane

Spec lifecycle: `draft → preregistered (hypothesis + expected result +
decision rule + power check) → queued → … → analyzed → carded`.
Services: `preflight` (quota snapshot + queue-by-purpose + power warnings;
runs at tick start and on demand), `power` (from Z3 variance), LADDER
(declared grids → specs under quotas), suite manager (frozen snapshots).
Output contract: every completed spec yields an **eval card** in
`research/cards/` (generated skeleton: config digests, n/k, intervals,
elicitation tuple, contamination note, threats, verdict) — cards are the
platform's citable results.

## 5. Analysis plane

Deterministic first (facts, cohort/TRUTH with clustered bootstrap +
paired-by-task + refuse-to-rank), then model-assisted (analysis worker,
bounded, GATE-authorized), then aggregation (lessons views with statistical
gates), plus meta-evaluation (judge calibration on a calendar; judged
numbers render with their agreement score or not at all). Multi-agent
access: analyst agents query the §2.2 attach surface read-only; their
outputs enter only as AnalysisRecords/ObservationRecords/DISCOVERIES drafts
— never as direct edits to lessons or cards.

## 6. Knowledge plane

CRAFT corpus + cookbooks (WS-A), context-pack compiler (WS-B), lessons
(WS-D), DISCOVERIES journal + verdict loop, doc lifecycle with generated
INDEX. Contract: knowledge artifacts are either generated-with-provenance
or human-authored-and-tagged; packs are compiled deterministically from
tags + facets; agents receive packs, not directory listings.

## 7. Authoring plane

The Proposal state machine (§2.1) with quarantine `library/tasks/_proposed/`,
qualification battery (oracle/nop/fair-oracle/adversarial), craft-review
scorer, human-only registry promotion, qualification ledger as the
pass-rate metric. Seeds: mutation | scenario | craft-gap. Registered
TaskVersions are immutable; changes create versions.

## 8. Coordination plane (multi-agent operating system)

- **Board:** the single dispatch source. Mission briefs are files with a
  schema: {mission, owns[], interfaces touched, acceptance, pack_ref}.
  Idle agents claim by moving brief to active/ (atomic, like the queue).
- **Roles as contracts:** builder/analyst/operator/integrator roles defined
  by (allowed paths, allowed CLIs, required pack, handoff schema). The
  Integrator (succession per PR #51) merges only green PRs and runs smoke.
- **Isolation:** worktree-per-mission under `.worktrees/`; disjoint owned
  paths; shared files additive-only.
- **Janitorial duty (T7):** every mission's acceptance includes: worktree
  removed, branch deleted after merge, brief archived by COORD-GC, docs it
  obsoleted moved to archive. `evallab tidy --dry-run` sweeps strays
  (untracked junk, stale worktrees, unindexed docs) into a report.
- **Token economy:** orchestrator writes briefs once and reads handoffs;
  agents self-serve packs; transcripts are never a coordination medium.

## 9. Extension seams (the "not closed off" guarantee)

| Seam | Interface | First expected consumer |
|---|---|---|
| ProviderAdapter | AgentProfile row + probe fn | Grok, Gemini profiles |
| StorageBackend | Z1/Z3 path providers behind config | S3/object store at scaling gate 1 |
| AnalysisPlugin | registered view/metric module over attach surface | new statistics, new failure taxonomies |
| SurfaceRenderer | reads attach surface, writes Z4/UI | alternative dashboards, reports |
| TrajectoryExport | `evallab export sft --suite S --filter reward=1` → messages-format dataset with provenance manifest | slime/verl-class post-training stack |
| RewardInterface | TaskVersion.verifier contract already engine-agnostic | RL environment wrappers |
| EnvironmentRegistry | registry of TaskVersions ≡ environment catalog | RL curriculum selection |

RL/post-training attaches at the last three seams and changes nothing
upstream: the platform's qualified tasks, verified rewards, and
facet-annotated trajectories are its input format. That is the whole
migration plan — by construction, not by future rewrite.

## 10. Build-out decomposition (for a large parallel team)

Contract-first protocol: E00 lands interface stubs + fixtures for every
epic below (pydantic models, empty CLIs, schema files, golden fixtures);
CI freezes those contracts; all other epics implement against them in
parallel. Epics (owns → depends):

E00 contracts+fixtures (all schemas §2.1) → none
E01 quota+preflight+purpose gate → E00
E02 LADDER grid generator → E01
E03 parquet compaction + scan budgets → E00
E04 unified attach surface (`db attach`) → E00
E05 catalog views + join-spine CI test → E00
E06 craft scan (deterministic) → E00
E07 craft classify + cookbooks → E06, GATE
E08 context-pack compiler + doc front-matter sweep → E00
E09 lessons views + generated lessons.md → E04–E06
E10 authoring pipeline + ledger → E06–E08, workbench
E11 eval-card generator → E04, TRUTH
E12 STATUS.md + INDEX generators + storm alarm → E04
E13 dashboard v2 on attach surface → E04, E11
E14 lineage walker → E05
E15 hypothesis fuzz + golden files → E00
E16 tidy/janitorial sweeps → E00
E17 SFT export (dry) → E04, registry
E18 skills (.claude/skills: lab-status, mission-launch, review) → E12

Parallelism: after E00, everything except the arrows runs concurrently;
maximum useful width ≈ 12 simultaneous missions. Days of diligent work at
fleet scale — exactly as requested.

## 11. Non-goals (v1) and risks

Non-goals: distributed execution, hosted multi-tenancy, RL training itself,
another eval harness, vector memory before its trigger. Risks: schema churn
(mitigated by E00 contract freeze + schema_version fields), small-files
regression (E03 + budgets), judge drift (calibration calendar), agent
sprawl (board + janitorial contracts), single-machine loss (backups now;
StorageBackend seam at gate).

## Changelog
- 2026-08-16 — v1 (Claude, at Peter's direction): full platform
  architecture; absorbs build-plan.md workstreams as epics E01–E18.
