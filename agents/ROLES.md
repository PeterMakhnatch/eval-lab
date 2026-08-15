# Role registry

Who exists, what they own, where they stand. New role = new row, by PR.
Status column is updated by the role itself or the integrator.

| Role | Branch | Owns (exclusive write) | Mission | Status (2026-08-14) |
|---|---|---|---|---|
| BUILDER | `main` | `src/`, `tests/`, `sql/`, `docs/prompts/`, `docs/`, `scripts/`, `compose.yaml`, `pyproject.toml`, `uv.lock`, `Makefile` | Briefs from `docs/design-additions.md` + `docs/fleet-tracking.md` | Current on `main`; no active feature branch. |
| CURATOR | — | `library/curated/`, `agents/handoffs/curator.md` | Verified library of 15–25 open-source Harbor tasks with provenance/verification cards | Done: 19 verified tasks are on `main`; branch and worktree sunset. |
| ADAPTER | — | `library/adapters/`, `agents/handoffs/adapter.md` | One external benchmark adapted end-to-end (QuixBugs) | Done: adapter, tasks, and verification evidence merged; branch and worktree sunset. |
| EVIDENCE | — | `research/calibration/`, `agents/handoffs/evidence.md` | Judge-calibration corpus + failure-taxonomy trajectory labels | Done: corpus and labels merged; branch and worktree sunset. |
| RECON | — | `research/explorations/`, `agents/handoffs/recon.md` | Working demos + adoption notes for unused Harbor 0.21 capabilities | Done: capability map and demos merged; branch and worktree sunset. |
| INGEST | — | `library/benchmarks/`, `agents/handoffs/ingest.md` | Frontier benchmark survey + Harbor-runnable materialization | Done: PR #3 merged; four Hub pins and the sample verification are on `main`. |
| OBSERVER | — | `src/evallab/tracing.py`, `agents/handoffs/observer.md` (+additive compose/cli/pyproject) | Phoenix + ATIF trace shipping + OpenInference (brief 08) | Done: PR #2 merged; branch and worktree sunset. |
| ANALYST | — | `src/evallab/{atif,facts,cohort}.py`, `research/analysis/`, `agents/handoffs/analyst.md` | ATIF→Parquet, deterministic facts, cohort compare, analysis sidecars (briefs 01–03) | Done: integrated into `main`; branch and worktree sunset. |
| RUNNER | — | `research/experiments/`, `agents/handoffs/runner.md` | Design/submit/interpret real experiments via the queue | Done: PR #5 merged; branch and worktree sunset. |
| AUTOPILOT | — | `src/evallab/researchers.py`, `digests/DISCOVERIES.md`, `agents/handoffs/autopilot.md` | Bounded 24/7 researcher loop + discovery journal | Done: integrated into `main`; branch and worktree sunset. |
| FORGE | — | `.github/`, `docs/engineering.md`, `agents/handoffs/forge.md` | Measured performance + CI/type hardening | Done: PR #6 merged green; branch and worktree sunset. |
| JUDGE | — | `src/evallab/calibrate.py`, `research/calibration/records/`, `agents/handoffs/judge.md` | Judge calibration + DSPy experiment 1 (brief 09) | Done: PR #4 merged; measured Codex judge remains below the 0.90 floor. |
| MEDIC | — | CI compatibility, deterministic canary test, premerge gate, and green-check governance | Make local and GitHub green agree; land PR #6 | Done: PRs #7, #6, and #8 merged green; branches and worktrees sunset. |
| REFRAME | — | Repository-wide identity migration and closeout | Establish Eval Lab as the research identity, with Harbor as execution engine | Done: PRs #9 and #10 merged green; branches and worktrees sunset. |
| DASHBOARD | — | `dashboard/`, additive CLI wiring, `agents/handoffs/dashboard.md` | Read-only research overview | Done: PRs #11 and #15 merged; seven-pane cold start reverified by MENDER. |
| FETCH | — | benchmark acquisition/audit paths and `agents/handoffs/fetch.md` | Pinned Hub acquisition with integrity audit | Done: PRs #13 and #18 merged; 5/5 audit reverified by MENDER. |
| RETENTION | — | GC paths and `agents/handoffs/retention.md` | Evidence-aware compression and pruning | Done: PRs #12 and #21 merged; dry-run plan reverified by MENDER. |
| PIPELINE | — | catalog/Parquet ingest path and `agents/handoffs/pipeline.md` | One completion path for both stores | Done: PR #17 merged; duplicate PR #16 closed and its branch/worktree sunset. |
| SPEED | — | profile harness, perf workflow, `agents/handoffs/speed.md` | Reproducible budgets for six hot paths | Done: PRs #14 and #20 merged; report reproduced by MENDER. |
| INSPECTOR | — | `research/inspections/`, `agents/handoffs/inspector.md` | Three evidence-quality inspections | Done: PR #19 merged; all three reports reverified by MENDER. |
| MENDER | — | integration closeout, `agents/ROLES.md`, `agents/handoffs/mender.md` | Verify the merged wave and sunset spent fleet state | Done: PR #22 merged with five green checks; final closeout branch/worktree sunset after this record lands. |
| TRUTH | `role/truth` | `src/evallab/{cohort,report}.py`, `research/{analysis,cards}/`, `tests/test_truth.py`, `agents/handoffs/truth.md` (+ additive CLI/schema wiring) | Task-clustered inference, power planning, trajectory family reports, and eval cards | Done: task-level intervals and guarded paired comparisons, power planning, trajectory family reports, and eval-card drafts accepted locally, in a fresh clone, and on GitHub PR #29. |
| DATA-STRATEGY | `role/data-strategy` | `docs/research/`, `docs/data-architecture.md`, DS section of `research/analysis/queries.sql`, `tests/test_provenance.py`, `agents/handoffs/data-strategy.md`; additive `src/evallab/schemas.py` (ProvenanceMetadata, by mission order) | Literature survey, external ATIF catalog, 4-zone provenance architecture, trajectory-intelligence queries, synthetic-task blueprint | P1-P5 delivered 2026-08-15; PR pending review. |
| SOLIDIFY | `role/solidify` | smoke/CI, credential-scoped execution, derived-data topology, executor resilience, soak operations, `agents/handoffs/solidify.md` | Make the composed evaluation loop repeatably reliable before capability expansion | Building: independent review remediated; four-hour soak and final exact-head gates in progress. |


**Wave 1 outcome (2026-08-14):** INGEST, OBSERVER, ANALYST, RUNNER, AUTOPILOT,
JUDGE all merged into `main` (PRs #2–5 plus two local branches integrated).
FORGE landed through PR #6 after MEDIC restored green CI. Details per role in
`agents/handoffs/`; first
measured result: codex-as-judge calibration lands below the 0.90 floor.

Peter owns `policy/standing-approvals.yaml` content (agents ship conservative
defaults, never loosen).

## Worktree locations

Finished mission worktrees were removed by MENDER on 2026-08-14. Its temporary
closeout worktree is removed after this final record merges; only the primary
`main` checkout remains.
