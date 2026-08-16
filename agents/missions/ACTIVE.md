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

`origin/main` is `86380b0` (PR #49, the M007 task-quality workbench).

- **No implementation mission is active.** Both feature slots emptied today when
  M006 and M007 merged. `agents/handoffs/` holds exactly one file,
  `board-refresh.md`, which is this Integration bookkeeping mission.
- **The only open PR is #51** (`role/orchestrator-handoff`, head `874f568`, one
  commit ahead of `origin/main`): a replacement-orchestrator handoff document.
  It is prose, not code. **Its disposition is unsettled** — the integrator has
  neither merged nor closed it, and this board does not decide it.
- **M009, the end-to-end integration flight, is in flight now** in
  `.worktrees/m009-flight` on `role/m009-flight`. It is the first attempt to
  prove that the separately-built components form one usable system rather than
  a set of independently green modules. It is an integrator acceptance exercise
  with exclusive use of the already-running local services, so it does not
  consume a build slot and does not make a third build PR.
- **Five missions merged today**, newest first: M007 (#49, `86380b0`), M006
  (#47, `4d23d7d`), COORD-GC (#54, `2173268`), PERF-REBASELINE (#53,
  `e080dd0`), system cartography (#52, `1471f41`). All five handoffs are
  archived 1:1 in `agents/archive/2026-08-15-handoffs/`; that directory's
  `INDEX.md` is the pointer, and `git log --follow` resolves each moved path.
- **Worktrees**: five besides the primary checkout —
  `board-refresh` (this mission), `m009-flight` (M009),
  `orchestrator-handoff` (PR #51 open, 1 commit ahead),
  `organization-prompts` (4 commits ahead, no PR), and
  `wave4-prompts` (1 commit ahead, no PR). The last two are deliberately kept
  and are the only unmerged committed work with no PR attached; both are 14 and
  17 commits behind `origin/main` respectively, so either needs a fresh branch
  off current main rather than a rebase of a stale tree. The "down to four"
  count recorded earlier today predates `board-refresh` and `m009-flight`.
- **Branch sunset is incomplete, and the usual test for it does not work here.**
  Worktrees were removed for all five merged missions, but every branch
  survives: `origin` still carries 18 `role/*` branches, and 17 of them are
  spent (all but `role/orchestrator-handoff`, whose PR #51 is open). None can be
  found by `agents/WORKFLOW.md`'s "zero commits ahead of `main`" rule, because
  every merge today was a squash merge, so each spent branch still reports
  commits ahead — `role/solidify` reports 44, `role/m007-task-workbench` 17,
  `role/m006-analysis-worker` 10. Deleting them is integrator sunset work and
  BOARD-REFRESH's lease is files, not refs, so this is recorded rather than
  done. Until it is done, treat any `role/*` branch whose PR is merged as spent
  per `agents/CHECKS.md`: start follow-up work from a fresh branch off
  `origin/main`, never by rebasing one of these.

### What the two merged features are, and are not

Both merged with their capability deliberately fenced. Read this before
planning anything that depends on them.

- **M006 is not live model analysis.** It merged with its calibration gate
  CLOSED and its default adapter `_no_adapter`. The stage-5 path is a
  saved-response stub. Opening that gate is M010's job, not a configuration
  change.
- **M007 grants nothing.** It is a certification tool: `admission_granted` is
  false, nothing self-registers, and `library/registry/` still holds zero
  records. Turning a candidate into a registerable packet is M011's job;
  registration itself remains Peter's reserved authority.

### Review cost, recorded because it is the most useful planning input here

Each feature took **four review-and-repair rounds**, and every round found a
real defect rather than a style objection. The round headings in the archived
handoffs are the checkable record.

- **M006** (`agents/archive/2026-08-15-handoffs/m006-analysis-worker.md`):
  rounds at lines 76, 142, 208, and 327. The first two were integrator reviews;
  the **last two were independent exact-head reviews** of `1f4cf6f` and
  `95d31e4`. The fourth round's defect was a `sidecar/` dirent that was not
  fsynced into its request directory — the name that proves a paid result
  exists could survive as an unreferenced inode after a crash.
- **M007** (`agents/archive/2026-08-15-handoffs/m007-task-workbench.md`):
  rounds at lines 211, 270, 629, and 854, of which the **last three were
  independent exact-head reviews**. The fourth round closed a
  `tests/docker-compose.yaml` escape past Harbor's egress control.
- **M007 also had a distinct withdrawal round** (line 583, commit `2a6aec0`),
  which is a different kind of event from fixing code: it **withdrew an
  already-committed certification packet** that asserted isolation for control
  runs whose verifier in fact had full network egress. Zero `.py` files
  changed; what changed was a published claim about evidence.

Planning consequence: for work of this class, budget the review rounds as part
of the mission, not as an exception. A single green CI run on a first head has
not once been sufficient today.

## Ready

- Nothing is dispatchable-but-unstarted. M010 and M011 unlock together when the
  M009 flight passes; until then there is nothing to hand a build worker.

## Next

- **M009 — end-to-end integration flight (in flight, integrator).** Prove the
  merged lab as one local, restartable Harbor-to-analysis product: start
  services from current main, run a real free control, produce and index a
  saved-response analysis, inspect it in the explorer, and test recovery. Every
  failure becomes a narrowly scoped follow-up instead of a success claim.
  Nothing below starts until this passes; a 24-hour soak or scheduled analysis
  execution stays off until then.
- **M010 and M011 — two build slots, in parallel, after M009 passes.**
  - **M010 qualified stage-5 analysis runtime** (Research + Platform): replace
    M006's hard-coded closed gate with a real tuple-specific qualification gate
    and a queue-authorized bounded adapter, fail-closed until measured
    agreement reaches 0.90.
  - **M011 first certifiable task pack** (Tasks): drive the merged M007
    workbench to turn the existing `event-summary` candidate into a
    version-pinned, adversarially tested certification packet Peter can
    knowingly register — without registering or publishing it.
  Their leases are disjoint, which is why they can run together.
- **M012 unified operator cockpit** (Platform), when a slot opens: one read-only
  UI showing what ran, what is running, what is queued, what is certifiable,
  and how each completed trial moved into analysis — without a second Streamlit
  app. Must not overlap another dashboard mission. See the `cli.py` question
  under `Needs Peter`, which is a sequencing question about this mission.
- **M013 restart-safe analysis service and soak** (Platform + Research review),
  after M010 and M009: restart-safe completion ingestion and analysis
  reconciliation, proven under an accelerated soak, with no autonomous spend.
- **M014 CI determinism and maintenance** (Integration + Platform), later
  hardening: remove host-state and wall-clock flakiness, expose untested
  command surfaces, prove any cleanup before deleting it. Waits until feature
  branches are quiet because its test/CI lease is broad.

### Mission candidates — recorded here so they cannot vanish with an archived handoff

Not active work. Each is unassigned, has no lease, and needs an M number and a
brief before dispatch.

- **Mission candidate — verifier-build observation is a text scan, not an
  observation (Tasks + Platform, unassigned).** M007's build-time check reads
  task-authored files and matches install idioms as *text*; roughly 20 plain
  idioms were added in its fourth repair round. That is a blocklist, and a
  blocklist over an arbitrary Dockerfile is defeated by any idiom nobody
  enumerated. The honest replacement is to build the verifier image in a
  container and observe what it actually does at build time. Scope note for
  whoever takes it: this changes M007 from "reads the task" to "runs the task's
  build", so it needs a Docker-daemon dependency the current check does not
  have.
- **Mission candidate — install Harbor in CI so M007's live drift comparison
  stops skipping (Platform, unassigned).** M007's Harbor drift check was split
  in its fourth round so that a static pin runs when no Harbor is installed.
  CI has no Harbor, so CI exercises only the static pin and the live comparison
  never runs there — the half that would actually catch Harbor drift is the half
  that is skipped. Harbor 0.21.0 is present on the workstation at
  `~/.local/bin/harbor`, so the gap is CI provisioning, not capability.
- **Mission candidate — the `ingest` perf metric measures the wrong thing
  (Platform, unassigned).** PERF-REBASELINE reports that the `ingest` metric
  times `initialize()` inside the measured region — a full `sql/schema.sql` DDL
  replay plus a second fresh connection — so the number is not ingest logic and
  carries most of the variance. Re-baselining the budget (#53, `e080dd0`)
  treated the symptom; this is the cause. Two constraints for whoever takes it:
  moving `initialize()` outside the timed region requires a **second**
  re-baseline, because the number drops sharply and the existing ceiling becomes
  far too loose (which would start tripping the below-50%-of-budget
  re-baseline notice); and it makes the pre-fix and post-fix `ingest` series
  **non-comparable**, which must be said wherever the series is read.
- **Mission candidate — calibration ground truth is mostly unscored (Research,
  unassigned).** The archived `observatory.md` produced 25 draft
  completed-trial records, but 23 of its 25 trajectory labels point at
  `harbor-practice/` source paths that do not exist in this repository, so those
  labels were never scored. Its recorded 8/8 field agreement therefore covers
  only **two** in-repo trials. Any claim resting on that agreement figure is
  resting on a sample of two. Archiving the handoff recorded where the gap came
  from; it did not close it. This also sets the floor for M010, whose gate is
  defined against measured agreement of 0.90.

## Needs Peter

Exactly two open items. Everything else on this board is a lane decision.

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
  `agents/archive/`. Carried unchanged from the previous board update; COORD-GC
  and BOARD-REFRESH both deliberately left the files in place.

- **Sequence a `cli.py` command-registry conversion before M012?** This is a
  spend-and-sequencing call — it buys no new capability and delays a feature
  mission — so it is Peter's, not the integrator's. The measured case, all
  figures taken from `origin/main` at `86380b0`:
  - `src/evallab/cli.py` is **1,412 lines**, and 892 of them (63%) are argparse
    wiring and string dispatch: a 402-line `parser()` (lines 133–534) that does
    nothing but build the parser tree — 111 `add_argument`, 46 `add_parser`,
    7 `add_subparsers` — plus a flat `if args.command == ...` chain of about
    **490 lines** inside a single ~500-line `run_cli()` (lines 873–1372; first
    branch at 884, last at 1336). There are **zero** `set_defaults(func=...)`,
    so dispatch is a linear string comparison chain that every new command must
    edit in the same place. (The earlier "~90% wiring, 475-line chain" estimate
    was taken before M006 added 57 lines; these figures are measured at
    `86380b0`.)
  - It is the **highest-churn file in `src/`**: 24 commits by
    `git log --follow`, against 14 for the next file (`researchers.py`) and 13
    for `schemas.py`.
  - It is already designated a shared file — additive-only, smallest possible
    diff — by `docs/prompts/overnight-missions.md:48`. Note the correction:
    that designation lives in a dispatch brief, **not** in `policy/`, which
    contains no reference to `cli.py` at all. If the shared-file rule is meant
    to be binding it currently has no binding home.
  - It has **already stalled a mission**. The archived `pipeline` handoff
    (`agents/archive/2026-08-15-handoffs/pipeline.md:2`) records a required
    rebase aborted after `cli.py` conflicted with five new `origin/main`
    commits, with the worker correctly refusing to resolve another role's
    conflict.
  - M007 just shipped **6,577 insertions across 47 files** — a 3,238-line
    `src/evallab/task_workbench.py` and a 1,580-line test — with its own
    `python -m evallab.task_workbench` entry point, and touched `cli.py`
    **zero times**. `src/evallab/` now has four modules with their own
    `__main__` entry (`calibrate.py`, `cli.py`, `smoke.py`,
    `task_workbench.py`) while `[project.scripts]` exposes only `evallab`. By
    contrast M006 did route through the CLI, adding 57 lines to it.
  - Why it bears on M012 specifically: M012's whole premise is *one* operator
    surface. Building a unified cockpit on top of a dispatch layer that new
    features are routing around means the cockpit inherits the fragmentation.
    Converting first costs a mission slot; converting after M012 means
    converting a file M012 has just grown.

  Peter retains policy/spend, publication, research-direction, and
  task-registration authority; current implementation and merge decisions
  belong to the integrator.

---

## Missions

This board is authoritative; a prompt set records what was dispatched on its
date. Two generations are live and neither supersedes the other wholesale:

- `docs/prompts/functionalization-missions-2026-08-15.md` specifies **M005,
  M006, M007** — all three now merged.
- `docs/prompts/next-functionalization-missions-2026-08-15.md` (later, merged
  as PR #48) holds the **M009 flight** and the **M010–M014** forward plan, and
  is the operative brief for everything ahead.

`docs/prompts/README.md` indexes both. COORD-GC and PERF-REBASELINE were
dispatched directly by the integrator without an M number and without a
committed brief; their scope was their board row. BOARD-REFRESH is the same
kind of Integration bookkeeping mission.

| ID | Outcome | Lane | Agent/model | Worktree / branch | Exclusive paths (lease) | Deps | Acceptance | PR | State | Merge owner |
|---|---|---|---|---|---|---|---|---|---|---|
| M006 | Every eligible completed trial gets one provenance-frozen analysis lifecycle, with zero calls outside profile + policy admission | Research, Platform review | Claude Code, claude-opus-5[1m] | worktree removed; branch `role/m006-analysis-worker` still present locally and on `origin` (squash-merged, spent — do not reuse) | `src/evallab/analysis_worker.py`, `tests/test_analysis_worker.py`, `tests/fixtures/analysis_worker/`, `docs/analysis-worker.md`, archived handoff; minimal additive schema/database/automation/queue/CLI wiring | M002, M003 | met after four review-and-repair rounds; merged at head `3b15e25` as `4d23d7d`. Calibration gate CLOSED, default adapter `_no_adapter` — saved-response path only | #47 | merged | integrator |
| M007 | Candidate task can be inspected, control-tested, mutation-tested, and packaged for review without self-registration or publication | Tasks | OpenAI Codex / GPT-5 | worktree removed; branch `role/m007-task-workbench` still present locally and on `origin` (squash-merged, spent — do not reuse) | `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`, `tests/fixtures/task_workbench/`, `library/synthetic/`, `research/registration/candidates/`, `docs/task-workbench.md`, archived handoff | M005 | met after four review-and-repair rounds plus one packet withdrawal; merged at head `4d47054` as `86380b0`. `admission_granted` false, nothing self-registers, `library/registry/` still empty | #49 | merged | integrator |
| COORD-GC | The coordination layer describes the repository that exists: spent handoffs archived, board factually correct, structure map true, stale CLI claims retired | Integration | Claude Opus 4.5, Oh My Pi | worktree removed; branch `role/coord-gc` still present locally and on `origin` (squash-merged, spent — do not reuse) | `agents/handoffs/`, `agents/archive/`, `agents/missions/ACTIVE.md`, `agents/STRUCTURE.md`, `docs/prompts/README.md`, `docs/checkpoints/2026-08-14.md` | none | met; merged at head `a41266e` as `2173268`. Archived 34 handoffs 1:1; found and fixed an already-merged root-freeze violation (`dashboard/` absent from `STRUCTURE.md`) | #54 | merged | integrator |
| PERF-REBASELINE | The `ingest` CI perf budget is calibrated to measured CI reality instead of a laptop capture, so the gate fails on regressions rather than on runner noise | Platform | **not recorded** — the archived handoff names no agent or model anywhere, so this mission's executing identity is unrecoverable from the repository | worktree removed; branch `role/perf-rebaseline` still present locally and on `origin` (squash-merged, spent — do not reuse) | `scripts/profile/budgets.json`, `docs/engineering.md` (one appended dated subsection), archived handoff | none | met; merged at head `a12ea3c` as `e080dd0`. Budget set to 115.0 ms from 14 CI artifact samples. Left the measurement-region cause open — now a mission candidate above | #53 | merged | integrator |
| SYSTEM-CARTOGRAPHER | The evaluation R&D platform is mapped as it exists, with corrected component cards and closed status labels | Integration | Grok 4.6 (xAI), Grok Build TUI | worktree removed; branch `role/system-cartographer` still present locally and on `origin` (squash-merged, spent — do not reuse) | `docs/system-cartography.html`, `docs/checkpoints/2026-08-15-system-cartography.md`, archived handoff | none | met; merged at head `a408881` as `1471f41`. 19 cards and 29 CLI groups corrected | #52 | merged | integrator |
| M009 | The merged lab is proven as one local, restartable Harbor-to-analysis product, with exact recorded evidence and every failure turned into a narrowly scoped follow-up | Integration (integrator-run acceptance exercise) | integrator session; recorded in its handoff | `.worktrees/m009-flight` / `role/m009-flight` | its own flight record and handoff; no feature lease — it consumes the merged system rather than changing it | M006, M007 | a real free control run, an indexed saved-response analysis, explorer inspection, and a proven recovery — all from current `origin/main`, not from fixtures | — | active | integrator |
| BOARD-REFRESH | `agents/handoffs/` holds only live missions, and the board states current truth with recorded follow-ups that cannot vanish with an archived file | Integration | Claude Opus 4.5, Oh My Pi | `.worktrees/board-refresh` / `role/board-refresh` | `agents/missions/ACTIVE.md`, `agents/handoffs/`, `agents/archive/`, `agents/handoffs/board-refresh.md` | none | every archived file reachable from `agents/archive/` with `git log --follow` intact; PR numbers, head SHAs, and merge states verified against `git log origin/main` and `gh pr list` | pending | review | integrator |
