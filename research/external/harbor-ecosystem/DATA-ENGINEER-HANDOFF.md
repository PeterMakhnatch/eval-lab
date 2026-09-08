---
status: current
role: Data Engineer (Harbor-native track)
pane: wK:pJ
as_of: 2026-09-08
revision: branch feat/dispatch-queue-compiler @ 884dbc17 (lane changes uncommitted in working tree)
companion: "harbor corpus `~/Developer/research-context/harbor/corpus/LOCAL-VERIFICATION.md`, `UPSTREAM-ISSUE-agentname-crash.md`, `FACET-LOADER-DECISION.md` (moved there by the Research lane; prior `research/external/harbor-ecosystem/` paths are stale)"
---

# Data Engineer — current lane handoff

Canonical handoff for this lane. Placed beside this lane's other durable artifacts;
`agents/handoffs/` is Integration-owned (`agents/OWNERS.md:11`) and absent from this
checkout, so nothing there was created or edited. Coordination is **pull-only**:
this file is updated silently and is the intake surface. No notification is sent.

## Standing mission (Peter via charter 2026-09-08, supersedes finite-campaign framing)

**Data Engineer — experiment data reliability and useful datasets.** Real data products from Harbor ecosystem experiments. Added to the existing role: type/link legacy diagnostic evidence, make the first new Lab job queryable, resolve recorded catalog/projection coverage issues without deleting evidence, deliver useful cohort/failure/curation outputs. Reuse merged #381 (traj-store read rule) and existing ingestion/schemas/read rules; **do not build another store**. Shared boundaries: execution DTOs + `cli.py` integration = Eval Runner; data projections = this lane; workbench/comparison = Harbor Integration. No `cli.py` edits from this lane; no `missions/ACTIVE.md` edits (integrator only).

### Ready (independent work, startable now)

| Packet | Input | Owner/files | Acceptance |
|---|---|---|---|
| R1 evidence inventory | Quality 49 executions, Factory candidates/controls, Integration PR #388 + task_000003/000011 pairs | scout worker, read-only, no files | typed inventory: authoritative vs contaminated with paths, intake mapping per record class |
| R2 coverage report | `ingest_verify.verify_ingest` + `storage.attach` inventory | new `src/evallab/coverage_report.py` + `tests/test_coverage_report.py` only | library function, JSON-serializable, fixture-backed tests, no CLI wiring |
| R3 new-job ingestion | `runs/tb21-codex-terra-slice`, `runs/harbor-tw20-oracle`, `runs/harbor-skill-verify` | lead, existing `evallab ingest` path | catalog rows + partitions + traces for agentic trials |

### Waiting (blocked on another lane or approval)

| Packet | Blocked on | Resumable state |
|---|---|---|
| W1 first *Lab-submitted* job | Eval Runner qualifying a native Lab job (the three jobs above predate the Lab path) | ingest command + verification queries recorded below |
| W2 curated export consumer | Analyst/Integration naming the consumer + curation shape | trace datasets + coverage report as inputs |

### Next (queued behind ready work)

| Packet | Depends on | Scope |
|---|---|---|
| N1 failure/cohort tables | R1 inventory + R2 coverage | analysis-ready tables over existing schemas, PR #381 read rules, named consumer |
| N2 doctor honesty | Peter pick (standing offer) | `evidence_path` in `_load_catalog_projection_rows` + checkout scoping in `check_projection_invariant` |
### Landed this cycle (2026-09-08)

| Packet | Result (executed, measured) |
|---|---|
| R1 evidence inventory | `EVIDENCE-INVENTORY-2026-09-08.md` (same dir): **49 authoritative** Quality executions (rows 1–30 baseline, 31–49 paired, count verified), 4 contaminated attempt groups retained as observations, 5 Factory candidates at 0 admissions with control receipts, 4 Integration evidence items mapped to `evallab.task_workbench` readers (2 consumable now, 2 need `observed-summary.json` filename tolerance), 5 intake extensions (all function-level, no new store), 4 open questions. Nothing manufactured; direct-Docker records stay external |
| R2 coverage report | new `src/evallab/coverage_report.py` (`build_coverage_report` reusing `verify_ingest` + attach inventory; JSON-serializable; no CLI wiring — Eval Runner owns `cli.py`) + `tests/test_coverage_report.py` (**5 passed**). Live output: 93 catalogued / 33 projected, reasons `{evidence_absent 157, projection_failed:MissingCASAuthority 67, job_unfinished 7, outside 3, nested 1}`, agent availability `{codex 42 trials/13 with trajectory, opencode 167/0, oracle 36 + nop 9 expected-absent, antigravity 13/0}`. Lead caught + fixed one defect in review: unfinished entries emitted `evallab ingest` (which refuses unfinished jobs) — now `unfinishable-by-ingest` marker with `origin: catalog\|disk-only` |

| | |
|---|---|
| Source | **Peter, directly** (two turns: the Harbor-native ingest/traces/manifest brief, then "prepare the system for quality buildout … remove redundancies") |
| Authorized slice, now landed | Prune zero-row Parquet tables — approved verbatim: "if its an empty parquet file then why have it … ok i guess go implement", with his own constraint "unless its for like accounting of trials/runs history" |
| Not authorized | Any deletion (worktrees, Lance, `traj_labels`, `_settlement`, `.merge_file_*`), paid runs, production queue/policy/schedule edits |

## Observed result and evidence level

Evidence level: **executed and measured on real lab data** (not inferred).

| Result | Measurement |
|---|---|
| Completeness reporting was dishonest | `verify_ingest` silently dropped **161 of 247** catalog jobs (65%), then reported green over the rest. Now classified and reported: `evidence_absent 157`, `outside_checkout 3`, `nested_checkout 1`, plus `job_unfinished 2` |
| Zero-row Parquet pruned, accounting preserved | Trial Parquet files **3,755 → 3,082**; re-projected trials **15.0 → 11.2** files/trial; oracle-only jobs **105 → 48** (−54%); 146 `_partition.json` manifests written |
| Completeness rule strictly better | Under the old all-files rule only **73** partitions qualified; manifest-aware rule counts **219** (+146 = exactly the re-projected set) |
| Feature registry had drifted from disk | Registry declared `edit_tool_call_count` (no such column) with formula over `state_diff_path_count`; real column is `edit_call_count` = `state_mutations_count / edit_call_count`. Fixed + guarded |
| Quality ledger could lose rows | Was unlocked read-modify-write + non-atomic `pq.write_table` on every ingest; now `fcntl.flock` + `write_table_atomic`, proven with 4 concurrent processes (all 4 retained, schema byte-identical) |
| Lake migrated through the standard path | `evallab ingest` over 30 jobs / 146 trials; `ingest_verify` = **0 gaps, COMPLETE**; 0 `.tmp` residue |
| Tests | **428 passed, 0 failed** across 16 modules (projection, storage, attach, compaction, conformance, CLI, catalog) |

Two pre-existing bugs fixed in passing, both confirmed at HEAD via `git show`:
`trajectory_quality.py` crashed bulk ingest on `agent_result: null` (same latent
pattern at `agent_info`); `test_cli_audit`'s CLI inventory never listed the
`dispatch`/`dispatch-set` commands this branch itself added.

## Primary evidence paths

- Code: `src/evallab/evidence/{atif,facts,event_mart,parquet_io}.py`, `src/evallab/ingest_verify.py`, `src/evallab/storage/attach.py`, `src/evallab/interpretation/{trajectory_quality,feature_registry}.py`, `src/evallab/database.py`, `sql/schema.sql`
- Contracts: `tests/test_partition_manifest.py` (5), `tests/test_feature_schema_conformance.py` (3)
- Tooling: `scripts/export_harbor_traces.py` (per-trial traces export, survives the upstream custom-agent crash without patching harbor)
- Corpora: `derived/harbor-packs/MANIFEST.md` (committed, force-added; packs stay git-ignored)
- Observed harbor 0.21.0 behavior: `LOCAL-VERIFICATION.md` §7 regeneration commands

## Cross-lane dependency any integration owner must know

The **PostgreSQL catalog (`localhost:54329`) is shared by every checkout, while
`derived/parquet` is per-checkout and git-ignored.** A job ingested from worktree A
stays in the catalog permanently, but its partitions exist only under A's derived
root — so no other checkout's completeness check can ever close it. That asymmetry
is now *reported* rather than silently filtered. Ingesting from your own worktree is
safe; expect your jobs to appear as out-of-scope elsewhere.

## Next bounded step (awaiting Peter's pick — not started)

1. **Recommended — make `evallab doctor` honest.** `catalog-parquet` reports
   `catalog=247 projected=103 missing=90` and is permanently red because
   `check_projection_invariant` lacks the checkout scoping `verify_ingest` now has.
   Fix: add `evidence_path` to `_load_catalog_projection_rows`' SELECT, then apply
   the same rule. A red check nobody can act on trains everyone to ignore health.
2. `dt=`/`task_family=` partition hierarchy — neither date nor task family is in the
   path today, so "all steps for family X in last 30 days" full-scans the lake.
   Needs dual-layout discovery during migration.
3. RL step surface — global monotonic step ordinal (subagent steps restart at
   `step_id=1`), per-step reward attribution, CAS pointers for replay.

## Blockers and decisions pending

- **Peter:** slice pick above (1/2/3 or hold). Nothing else blocks this lane.
- **Peter:** Harbor Hub credentials for the harbor-index corpus (`research/external/harbor-index/README.md`).
- **Peter:** deletion approvals. Staleness was **verified, not assumed**: of 14 worktrees with no commit in 7+ days, **11 hold uncommitted work** (1–11 dirty files) and **9 hold unmerged commits** ahead of `origin/main`. Nothing was deleted; the commit-date heuristic was wrong.

## Known remaining gaps (not mine to close from this checkout)

- 55 legacy partitions are genuinely incomplete at 8-of-15 tables (projections predating the event mart); their evidence lives in other checkouts, so only the owning checkout can re-project them.
- `derived/harbor-tasks` (1.78 GiB) is a staged corpus not covered by `MANIFEST.md`.
- `projection_table_settlements` is the largest catalog table (9 MB, 3,994 rows) with **no writer in this checkout** — ownership unresolved.
- `traj_baseline.py` still documents a legacy `state_journal_status` domain; `evidence/facts.py` emits a different domain to `trial_facts` than `traj.py` emits to `traj_features`.
- Untracked `synthetic_tool_memory` files in the tree are **not** this lane's work.
