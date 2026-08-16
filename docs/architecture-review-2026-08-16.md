# Architecture review — 2026-08-16

Requested by Peter: state inspection, block map, data-architecture verdicts
(LanceDB / Postgres / Parquet growth), experiment governance for next week's
runs, doc hygiene, and coordination economics. Analysis, not prompts. The
in-repo Integrator (PR #51 succession line, M-numbered missions) owns
sequencing; this review is input to that board, and Peter's decisions where
marked.

## 1. State, honestly

Since 08-14: ~22 PRs merged (M005–M009 flights, OPERATOR fixes, TRACE,
ANALYSIS worker, WORKBENCH certification, PERF budgets, cartography), test
suite >200, queue events show 12/16 dispatches completed, 2 Postgres backups,
zero dollars spent, and the fleet has self-organized an Integrator with a
board and a succession handoff (PR #51). Open: #51, #64 (QUOTA — real
subscription consumption instead of notional dollars).

**Progress verdict.** The feeling of a "sorry state" is real but localized:
the *plumbing* (queue, policy, evidence, CI, backups, analysis workers) is
several engineer-weeks of solid work delivered in five days. What lags is the
**surfaces block** — dashboard, live status, experiment rationale — which is
the only part Peter touches daily. The lab is not slow; it is *illegible from
the driver's seat*. Fixing legibility, not adding capability, is the highest-
leverage work this week.

## 2. Block map

| Block | State | Gap |
|---|---|---|
| Supply (library/, fetch, workbench certification) | strong | registry/pinned-suite completion |
| Execution (queue, policy, executor, launchd) | strong, hardened by SOLIDIFY | per-provider RUN quotas (see #64) |
| Evidence (runs/, catalog, Parquet, backups) | strong | Parquet compaction plan (§3) |
| Analytics (facts, cohort/TRUTH, analysis worker, observations) | good | one unified query surface (§3) |
| **Surfaces (dashboard, docs, digests)** | **weak — the felt problem** | rebuild on unified surface; generated STATUS; doc lifecycle (§5) |
| Governance (CI, premerge, CHECKS, calibration) | good | experiment *purpose* governance (§4) |
| Coordination (Integrator, board, handoffs) | emergent, working | one orchestrator at a time; token-cheap dispatch (§6) |

## 3. Data architecture verdicts

**Postgres — sound; keep; no redesign.** Rebuildable-index contract held
through every incident; backups now run. Add only: a few catalog views for
the common joins (experiment → job → trial → analysis) so every consumer
stops hand-writing them.

**Parquet — the small-files clock is ticking, with a date.** Current layout
is per-trial directories (~34 files/job; 808 files at 24 jobs). At the
planned nightly (suite × roster × k ≈ 100–240 trials) that is ~1–4k files
*per night*; DuckDB scan planning degrades noticeably in the 10k–50k range —
i.e. **one to two weeks after real nightlies begin**. Remedy, scheduled not
premature: a nightly **compaction step** — consolidate closed days into
`derived/parquet/compact/dt=YYYY-MM-DD/*.parquet` (one file per table per
day), keep per-trial granularity only for the trailing 7 days, and wire scan
latency into PERF's existing budget so the trigger is measured, not guessed.
Rebuildability contract unchanged (compaction is a projection of a
projection).

**LanceDB — no, and here is the trigger.** Agents deciding over experiment
data need *structured* recall first, and that is SQL's job. The actual
connective tissue to build now is **one unified read surface**: a DuckDB
attach database (`lab.duckdb` or a generated `ATTACH` script) exposing
catalog tables (via postgres scanner), Parquet facts, observation-record
front-matter, and calibration records as one SQL namespace. Dashboard,
researcher agents, and Peter's ad-hoc questions all read the same surface.
LanceDB earns entry only when (a) the textual corpus (observations,
discoveries, cards) exceeds ~500 documents AND (b) an agent demonstrably
fails to find relevant priors via SQL+grep. Revisit then, not before.

## 4. Experiment governance (before next week's runs)

The credit-burn incident class gets three locks plus a brain:

1. **Real consumption accounting** — PR #64's direction is right: meter
   runs/tokens per provider per UTC day from the catalog; notional dollars
   stay only as the runaway-API-key alarm.
2. **Per-provider run quotas in policy** — subscriptions are the real
   budget; the executor defers when a provider's daily quota is spent
   (deferral, not failure — morning digest shows what waited).
3. **Deferral/failure storm alarm** — N same-reason events within an hour
   flips a visible banner in digest + dashboard. Silence was the actual bug
   in the incident, more than spend.
4. **The brain — every spec declares WHY.** Add a required `purpose` field:
   `baseline | comparison | elicitation | drift | calibration | practice`,
   plus a one-line question link. `evallab preflight` (run at tick start and
   printable on demand) then answers Peter's exact ask: *what will run next,
   why, with which models, at what remaining quota, and what the night is
   trying to learn.* A spec without a purpose does not dispatch. The weekly
   view of queue-by-purpose IS the experiment plan — generated, never
   hand-written.

## 5. Surfaces: make the lab legible (the week's real priority)

- **One dashboard, rebuilt on the unified surface (§3):** panes = preflight
  (quota + queue-by-purpose), last night, canary trend, suite leaderboard
  rendered through TRUTH (n, intervals, or "not comparable" — never bare
  means), fleet/board state. Read-only stays law.
- **Generated STATUS.md** at repo root, rewritten by CI on every merge and
  by the nightly: what merged today, suite health, quota consumption, open
  decisions. This — not hand-written docs — is "documentation updated in
  real time." Hand-maintained status documents are hereby deprecated as a
  class.
- **Doc lifecycle:** `docs/` now holds five+ overlapping state snapshots
  (repo_overview.html, repository-state.html, system-cartography.html,
  eval-rd-roadmap.html, agent-workflow.html…). Policy: every doc is
  `living` (indexed in a generated docs/INDEX.md, referenced by AGENTS.md)
  or `historical` (moved to docs/archive/ with a one-line tombstone).
  One-off HTML reports are historical by default the day after they are
  read. Agents' read-list contains living docs only — this is the context-
  pollution fix. The Integrator's existing COORD-GC pattern extends to docs.

## 6. Coordination economics (Peter's speed complaint, structurally)

- **One orchestrator at a time.** Merge #51's succession model: an
  Integrator with authority boundaries, a board, and a copy-paste startup
  brief. Two planning brains (in-chat and in-repo) drafting missions
  independently caused duplicated intent this week; the in-repo board is
  now the single dispatch source, chat is for Peter-level decisions.
- **Token-cheap dispatch:** the orchestrator writes mission *files* to the
  board and reviews *handoffs*, never transcripts. Long-horizon agents
  self-serve briefs from the repo. The orchestrator's own token spend
  should be ~review-only; drafting happens once, in the brief.
- **Peter's interface stays three surfaces:** STATUS.md, digest, PR list —
  plus one weekly decision batch (registry approvals, DISCOVERIES verdicts,
  quota/policy edits).

## 7. Decisions for Peter (everything else is board work)

1. Approve the purpose taxonomy + preflight gate (§4.4).
2. Approve per-provider run quotas replacing dollar ceilings as the primary
   meter (#64 direction).
3. Approve the doc lifecycle policy (§5) so archiving can proceed.
4. Choose the week's experiment intent: recommended — *baseline week*:
   registered suite × available agents × k=5, purpose=baseline, to produce
   eval card #1 and the variance data every later comparison needs.

## Double-check log

Claims verified against the live tree on 2026-08-16: PR list and merge log
(#40–#64), event counters (16 dispatches, 3 failed — all 2026-08-14 model-
name class), 808 Parquet files / 24 jobs, >200 tests green, backups present
in events, docs inventory as listed. Parquet growth math assumes ~34
files/job at current projection layout; re-measure after compaction lands.
