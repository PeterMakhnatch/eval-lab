# Mission board

The sole live board. Only the integrator edits this file. States:
`ready` -> `active` -> `review` -> `merged`, with `blocked` possible anywhere.
Template: `TEMPLATE.md`. Finished missions move to `agents/archive/` with a
date prefix.

Workers own implementation inside their path lease and stop at review. The
integrator owns cross-mission conflict resolution, semantic review, rebase,
fresh exact-head CI, merge, board transition, and worktree sunset. A review
bot may advise later; it is never merge authority.

## Now

- **M008 (active, Integration):** close the reviewed PR queue, archive finished
  missions, publish the functionalization prompts, and repair live-fleet
  classification before handing control back.

## Review

- **M005 (Platform), PR #44:** the worker reports the explorer and fixture
  suite complete and has stopped for integrator review.

## Ready

- **M006 (ready, Research + Platform review):** its M002 and M003 dependencies
  merged as PRs #42 and #43. The already-assigned worker may now create its
  worktree from current `origin/main` and proceed. No M006 worktree or branch
  was visible at this board update, so the board does not claim it is active.

## Next

- **M007 (blocked, Tasks):** start after either M005 or M006 merges and a build
  slot opens. It is disjoint from both current implementation leases.

## Needs Peter

- Nothing. Peter retains policy/spend, publication, research-direction, and
  task-registration authority; current implementation and merge decisions
  belong to the integrator.

---

## Missions

Full copy-paste prompts and sequencing are in
`docs/prompts/functionalization-missions-2026-08-15.md`.

| ID | Outcome | Lane | Agent/model | Worktree / branch | Exclusive paths (lease) | Deps | Acceptance | PR | State | Merge owner |
|---|---|---|---|---|---|---|---|---|---|---|
| M005 | Operator can inspect task -> job -> trial -> trajectory -> analysis, distinguish evidence states/failure classes, and copy a safe next command without the UI writing state | Platform | assigned external worker; exact agent/model required in handoff | `.worktrees/m005-explorer` / `role/m005-explorer` | `src/evallab/explorer.py`, `tests/test_explorer.py`, `tests/fixtures/explorer/`, `dashboard/{explorer,app}.py`, `dashboard/README.md`, `docs/run-explorer.md`, `agents/handoffs/m005-explorer.md` | M002 | malformed/cold-start-safe linked views; path/secret/hidden-data safety; fixture coverage; render smoke; premerge + exact-head CI | #44 | review | integrator |
| M006 | Every eligible completed trial gets one provenance-frozen analysis lifecycle, with zero calls outside profile + policy admission | Research, Platform review | assigned external worker; exact agent/model required in handoff | `.worktrees/m006-analysis-worker` / `role/m006-analysis-worker` | `src/evallab/analysis_worker.py`, `tests/test_analysis_worker.py`, `tests/fixtures/analysis_worker/`, `docs/analysis-worker.md`, `agents/handoffs/m006-analysis-worker.md`; minimal additive schema/database/automation/queue/CLI wiring | M002, M003 | idempotent/concurrent/crash-safe lifecycle; evidence tamper quarantine; saved-response end-to-end; cycle x3; premerge + exact-head CI | — | ready | integrator |
| M007 | Candidate task can be inspected, control-tested, mutation-tested, and packaged for review without self-registration or publication | Tasks | unassigned | `.worktrees/m007-task-workbench` / `role/m007-task-workbench` | `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`, `tests/fixtures/task_workbench/`, `library/synthetic/`, `research/registration/candidates/`, `docs/task-workbench.md`, `agents/handoffs/m007-task-workbench.md` | M005 or M006 | deterministic inspect/check/packet; oracle/nop + adversarial discrimination; isolation/provenance; no promotion powers; premerge + exact-head CI | — | blocked | integrator |
| M008 | Reviewed PR queue merged; live board and archive reconciled; functionalization prompts made durable; fleet-status preserves dirty zero-ahead work | Integration | Codex, GPT-5.6 | `.worktrees/integrator-closeout` / `role/integrator-closeout` | `agents/missions/ACTIVE.md`, `agents/archive/2026-08-15-missions-m001-m004-a001.md`, `agents/handoffs/integrator-closeout.md`, `docs/prompts/functionalization-missions-2026-08-15.md`, `scripts/fleet-status.sh`, `tests/test_fleet_status.py` | M001, M002, M003, A001 | exact-head PR state reconciled; dirty-work + future-mission regression tests; premerge + exact-head CI | — | active | integrator |
