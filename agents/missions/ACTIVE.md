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

`origin/main` is `1471f41` (PR #52, the system cartography report).

- **Both feature-implementation branches are at review, and both were returned
  for bounded repair.** M006 is PR #47 on `role/m006-analysis-worker`; M007 is
  PR #49 on `role/m007-task-workbench`. Each was reviewed at an exact head —
  `1f4cf6f` and `c6c35a4` respectively — by an independent reviewer, and each
  was judged `incorrect`. Neither is mergeable as reviewed. Repair work is in
  flight inside each mission's existing worktree and lease; a repair commit
  invalidates that branch's previously green CI, which is expected. The
  integrator requires fresh exact-head CI on the repaired head before merge.
- **COORD-GC is active** (Integration): coordination-artifact garbage
  collection, board correction, structure-map repair, stale-claim retirement.
- **PERF-REBASELINE is at review** (Platform): PR #53, head `a12ea3c`,
  re-baselines the miscalibrated `ingest` CI perf budget. Awaiting integrator
  merge; not merged as of this board update.
- **The system cartography mission merged** as PR #52 (`1471f41`). Its handoff
  is still in `agents/handoffs/` pending the next Integration sweep.

## Ready

- Nothing. No mission is dispatchable-but-unstarted. The next build slot opens
  when M006 or M007 merges.

## Next

- **M006/M007 merge sequence (blocked on repair):** review the repaired heads,
  require fresh exact-head CI on each, merge, then sunset both worktrees and
  branches and archive both handoffs.
- **Maintenance merges (ready for the integrator):** PERF-REBASELINE PR #53 and
  COORD-GC. Both touch disjoint paths from M006/M007 and from each other, so
  merge order between them is free; merging them before the repaired feature
  heads means M006/M007 rebase onto a corrected board rather than the reverse.
- **Next Integration sweep:** archive `system-cartographer.md` (PR #52 merged),
  and `perf-rebaseline.md` once #53 merges. COORD-GC left the cartographer
  handoff in place because its keep-list said to; the mission itself is done.
- **Integrator live flight (blocked on M006):** after M006 merges, start
  services from current main, run a real free control, produce and index a
  saved-response analysis, inspect it in the explorer, and test recovery. Only
  after that passes should a 24-hour soak or scheduled analysis execution be
  enabled.

## Needs Peter

- **Keep or archive the four hand-authored HTML documents under `docs/`?**
  `docs/agent-workflow.html`, `docs/eval-rd-roadmap.html`,
  `docs/repository-state.html`, `docs/system-cartography.html`, and their
  shared `docs/repository-overview.css`. No committed code generates them
  (`src/`, `scripts/`, `dashboard/`, `Makefile`, and `.github/` contain no
  reference to any of them), so they cannot be rebuilt and they will drift
  silently against the Markdown they duplicate. Reference status, measured:
  the first three plus the CSS are referenced *only* by each other;
  `system-cartography.html` is additionally named by
  `docs/checkpoints/2026-08-15-system-cartography.md` and
  `docs/prompts/system-cartographer-2026-08-15.md`, so it has an authored
  owner the other three lack. This is a documentation-surface question, not a
  lane decision: either they are a deliberate human-readable surface worth
  maintaining by hand, or they are spent one-off renders that belong in
  `agents/archive/`. COORD-GC deliberately neither moved nor deleted them.

  Peter retains policy/spend, publication, research-direction, and
  task-registration authority; current implementation and merge decisions
  belong to the integrator.

---

## Missions

Full copy-paste prompts and sequencing for the M-numbered missions are in
`docs/prompts/functionalization-missions-2026-08-15.md` — that file is the
authoritative mission-prompt generation. COORD-GC and PERF-REBASELINE were
dispatched directly by the integrator without an M number and without a
committed brief; their scope is their board row.

| ID | Outcome | Lane | Agent/model | Worktree / branch | Exclusive paths (lease) | Deps | Acceptance | PR | State | Merge owner |
|---|---|---|---|---|---|---|---|---|---|---|
| M006 | Every eligible completed trial gets one provenance-frozen analysis lifecycle, with zero calls outside profile + policy admission | Research, Platform review | external worker; exact agent/model recorded in handoff | `.worktrees/m006-analysis-worker` / `role/m006-analysis-worker` | `src/evallab/analysis_worker.py`, `tests/test_analysis_worker.py`, `tests/fixtures/analysis_worker/`, `docs/analysis-worker.md`, `agents/handoffs/m006-analysis-worker.md`; minimal additive schema/database/automation/queue/CLI wiring | M002, M003 | idempotent/concurrent/crash-safe lifecycle; evidence tamper quarantine; saved-response end-to-end; cycle x3; premerge + **fresh** exact-head CI on the repaired head | #47 | review | integrator |
| M007 | Candidate task can be inspected, control-tested, mutation-tested, and packaged for review without self-registration or publication | Tasks | external worker; exact agent/model recorded in handoff | `.worktrees/m007-task-workbench` / `role/m007-task-workbench` | `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`, `tests/fixtures/task_workbench/`, `library/synthetic/`, `research/registration/candidates/`, `docs/task-workbench.md`, `agents/handoffs/m007-task-workbench.md` | M005 | deterministic inspect/check/packet; oracle/nop + adversarial discrimination; isolation/provenance; no promotion powers; premerge + **fresh** exact-head CI on the repaired head | #49 | review | integrator |
| COORD-GC | The coordination layer describes the repository that exists: spent handoffs archived, board factually correct, structure map true, stale CLI claims retired | Integration | free-tier worker; recorded in handoff | `.worktrees/coord-gc` / `role/coord-gc` | `agents/handoffs/`, `agents/archive/`, `agents/missions/ACTIVE.md`, `agents/STRUCTURE.md`, `docs/prompts/README.md`, `docs/checkpoints/2026-08-14.md` | none | `agents/handoffs/` holds only live missions; every archived file reachable from `agents/archive/` with `git log --follow` intact; board matches PR/branch/state facts; `STRUCTURE.md` names `dashboard/` and its `docs/` submap matches `git ls-tree origin/main docs/` | — | active | integrator |
| PERF-REBASELINE | The `ingest` CI perf budget is calibrated to measured CI reality instead of a laptop capture, so the gate fails on regressions rather than on runner noise | Platform | free-tier worker; recorded in handoff | `.worktrees/perf-rebaseline` / `role/perf-rebaseline` | `scripts/profile/budgets.json`, `docs/engineering.md` (one appended dated subsection), `agents/handoffs/perf-rebaseline.md` | none | budget derived from the recorded `speed-profile-report` medians of successful `ubuntu-latest` perf runs, with the derivation written down; harness and workflow changes are an explicit non-goal; no gate weakening beyond what the measurements justify | #53 | review | integrator |

### Repair assignments (not separate missions)

The two repair efforts are worker assignments inside the existing M006 and M007
rows above. They create no new branch, worktree, lease, or board row, and they
do not renumber the missions. Their output is a new head on the same PR.
