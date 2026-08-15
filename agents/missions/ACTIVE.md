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

- No implementation branch is active or at review in the observed local/GitHub
  state. M006 is already assigned and may start; M007 is newly dispatchable.

## Ready

- **M006 (ready, Research + Platform review):** its M002 and M003 dependencies
  merged as PRs #42 and #43. The already-assigned worker may now create its
  worktree from current `origin/main` and proceed. No M006 worktree or branch
  was visible at this board update, so the board does not claim it is active.
- **M007 (ready, Tasks):** M005 merged as PR #44, satisfying its dependency and
  opening the second build slot. Its full copy-paste prompt is durable below.

## Next

- **Integrator live flight (blocked):** after M006 merges, start services from
  current main, run a real free control, produce/index a saved-response
  analysis, inspect it in the explorer, and test recovery. Only after that
  passes should a 24-hour soak or scheduled analysis execution be enabled.

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
| M006 | Every eligible completed trial gets one provenance-frozen analysis lifecycle, with zero calls outside profile + policy admission | Research, Platform review | assigned external worker; exact agent/model required in handoff | `.worktrees/m006-analysis-worker` / `role/m006-analysis-worker` | `src/evallab/analysis_worker.py`, `tests/test_analysis_worker.py`, `tests/fixtures/analysis_worker/`, `docs/analysis-worker.md`, `agents/handoffs/m006-analysis-worker.md`; minimal additive schema/database/automation/queue/CLI wiring | M002, M003 | idempotent/concurrent/crash-safe lifecycle; evidence tamper quarantine; saved-response end-to-end; cycle x3; premerge + exact-head CI | — | ready | integrator |
| M007 | Candidate task can be inspected, control-tested, mutation-tested, and packaged for review without self-registration or publication | Tasks | unassigned | `.worktrees/m007-task-workbench` / `role/m007-task-workbench` | `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`, `tests/fixtures/task_workbench/`, `library/synthetic/`, `research/registration/candidates/`, `docs/task-workbench.md`, `agents/handoffs/m007-task-workbench.md` | M005 | deterministic inspect/check/packet; oracle/nop + adversarial discrimination; isolation/provenance; no promotion powers; premerge + exact-head CI | — | ready | integrator |
