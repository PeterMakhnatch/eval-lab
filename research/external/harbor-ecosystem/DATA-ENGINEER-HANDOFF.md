---
status: current
role: Data Engineer (Harbor-native track)
pane: wK:pJ
as_of: 2026-09-08
revision: branch feat/dispatch-queue-compiler @ 884dbc17 (lane changes uncommitted in working tree)
companion: "harbor corpus `~/Developer/research-context/harbor/corpus/LOCAL-VERIFICATION.md`, `UPSTREAM-ISSUE-agentname-crash.md`, `FACET-LOADER-DECISION.md` (moved there by the Research lane; prior `research/external/harbor-ecosystem/` paths are stale)"
---

# Data Engineer — current lane handoff

Canonical handoff for this lane. Coordination is via **monitored callbacks**
(completion monitor registered; no manual notification on normal checkpoints).

## Build assignment (Peter 2026-09-08 idle-lead correction; supersedes report-first pacing)

Charter §'Immediate build assignments' + Data Engineer §'consume incomplete jobs and native accounting'. Two parallel owned slices, one writer per isolated worktree, disjoint files. Baseline committed as `938336fd` on new branch `lane/data-build-20260908` (lane files only; shared-branch files like `cli.py`, `AGENTS.md`, `docs/STATUS.md` deliberately left uncommitted). Venvs rebuilt per worktree-hygiene skill; `evallab` import verified to resolve inside each worktree.

| Slice | Worktree | Worker owns (only) | Real inputs (read-only) |
|---|---|---|---|
| A partial-job intake | `.worktrees/data-partial-intake` | NEW `src/evallab/partial_intake.py` + `tests/test_partial_intake.py`; ONE hunk in `ingest_verify.py` (partial-marker rule) | `runs/failed-network-policy-oracle`, `runs/brief07-query-controls/…`, disk-only unfinished jobs — lock.json/config trials, NO trial result.json, NO rewards/trajectories to invent |
| B request accounting | `.worktrees/data-request-accounting` | NEW `src/evallab/request_accounting.py` + `tests/test_request_accounting.py`; NO existing-file edits | harness `native-lab-20260908/adapter-contract.json` (request_sidecar schema v1, actor/image_controller, nulls-stay-null, cost-null) + `native-runtime-transport-replay.json` + `lab-development-usage.json` |

Hard rules on both: no second store/catalog, no fabricated rewards/completion/ATIF/probabilities, no cost inference, sealed task_000009 excluded, no `cli.py`/`missions/ACTIVE.md` edits, no paid/network/Docker/Harbor execution. Workers skip validation mid-flight; lead runs integrated checks on delivery.

### Landed 2026-09-08 (both slices, integrated by lead)

Branch `lane/data-build-20260908` (base `938336fd`): merges `lane/data-partial-intake` + `lane/data-request-accounting`. NOT merged to any shared branch — no merge authority granted; integrator owns merges.

| Slice | What landed | Proof (executed, measured) |
|---|---|---|
| A partial-job intake | `src/evallab/partial_intake.py` (lenient loader w/ deterministic UUID5 trial ids, catalog + jobs/trial_facts only, `_partial.json` marker, FORBIDDEN list enforced) + 1 hunk in `ingest_verify.py` (partial_jobs accounting) + owner-side `results.py` identity fallback + `atif.partition_missing_tables` delegation to the intake predicate | All 8 unfinished jobs converted (2 cataloged + 6 disk-only): jobs+trial_facts only, 0 failures, 0 reward/trajectory bytes. Verify now reads unfinished 0 / partial 8 / gaps 0 COMPLETE. Tests: 6 new + full sweep below |
| B request accounting | `src/evallab/request_accounting.py` (RequestAttempt per contract: roles, attempt ordinals, nullable tokens, cost always None, late-receipt updates, sealed-000009 guard) + `derived/request-accounting/request_attempts.parquet` (13 rows) + `_accounting.json` manifest | 9 replay + 4 development attempts; role summary actor 13 (6 success / 5 error / 2 unknown-ish incl. timeouts), late 2; all 4 bound trials show ATIF steps != request attempts (9v1, 11v1, 12v1, 15v1); nulls preserved; cost null everywhere. Tests: 6 new |

Integrated sweep: **316 passed, 2 skipped** across 13 modules (partial, accounting, ingest_verify, manifest, pipeline, z2, traj, semantics, conformance, coverage, compaction, attach, event_mart).

### Next authorized successor — GATE IDENTIFIED, not started

### Delivered 2026-09-08, second packet (Integration R2 request via inbox)

Singleton `derived/coverage/coverage-scope-90e3c9e9a058.json` (`job_ids` exactly
`[f94f1507-7958-4e08...]` = evidence UUID, spec `01M2199Y5ZE41QNPVCS7TSKA3Y` bound
in `external_links`): catalogued 1, projected 1, oracle 1 trial expected-absent,
reasons {} — plus corrected 79-set `coverage-scope-3454aaa747d7.json` (catalogued
21/21, native 21 scoped, agents real, reasons scoped, failed 1 genuine Factory
crash). Code: `scoped_catalog_loader` agent enrichment, `selected_job_ids`
scoping for exceptions/failed/native/projected, `not_cataloged` surfacing,
`catalogued` count==names fix. Tests: 13 in `tests/test_coverage_report.py`.
Coordination: `research/inbox/data-engineer-coverage-scope-v2-20260908.md`
(v1 note stands for the preserved v1 product).
Incident, reported not smoothed: evidence UUID `4e08` vs a transient catalog row
`4c80`; row + 19:42 partition vanished by 19:44 with no queue event during
concurrent ingests, evidence intact throughout; both restored under the evidence
Branch `lane/data-build-20260908`; worktree `data-coverage-scope` retained.

### Delivered 2026-09-08, third packet (continuity correction)

Writer-integrity repairs (slice branch `lane/data-writer-integrity`, worktree
retained; 9 new behavioral tests, isolated DB + tmp roots, never prod R2/catalog):
unique temp names (concurrent-tear reproduced: 2 errors + unreadable file under
old behavior), `lab_metadata.supersedes` on same-path UUID replacement (same-id
regeneration records nothing), staged per-job publish with atomic renames
(interruption leaves zero live trace; one `test_pipeline` assertion updated to
the atomicity contract with rationale), opt-in `purge_inert_staging()`.
Correction product `derived/coverage/coverage-scope-45a1d8a6d0e0.json` (R2
singleton, spec-link status derived as `bound-catalogued-projected` via new
`summarize_binding`, matrix-pinned). v1/v2/incident preserved. Note:
`research/inbox/data-engineer-coverage-scope-v3-20260908.md`. Sweep: 46 passed
in main checkout. Known environmental: `test_ingest_verify_cli_output` fails in
fresh worktrees lacking `queue/events.jsonl` (passes in main); R2 UUID
transposition itself unattributed by design — now recorded rather than silent.
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
