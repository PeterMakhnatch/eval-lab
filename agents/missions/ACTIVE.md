# Mission board

The sole live board. Only the integrator edits this file. States:
`ready` -> `active` -> `review` -> `merged`, with `blocked` possible anywhere.
Template: `TEMPLATE.md`. Finished missions move to `agents/archive/` with a
date prefix.

Workers own implementation inside their path lease and stop at review. The
integrator owns cross-mission conflict resolution, semantic review, rebase,
fresh exact-head CI, merge, board transition, and worktree sunset. A review
bot may advise later; it is never merge authority.

## Night result: all five loops finished, all five PRs held on red CI

29 cycles across five missions, every cycle committed and pushed. Nothing merged,
because Actions has not run a single step since 06:15 UTC (see below). Each PR
carries local proof; I independently re-verified the load-bearing claim of each
rather than accepting its handoff.

| PR | Mission | Cycles | What I verified myself |
|---|---|---|---|
| #117 | M015 AUDIT | 7 | Ledger is 17 CONFIRMED / 2 DRIFTED / 1 UNPROVEN with a full append-only correction chain |
| #119 | M016 SURFACE | 6 | Zero-trial day renders "nothing ran" with no trial ids; reverting the guard fails 2 tests |
| #121 | M017 CARDS | 5 | Five cards valid; validator rejects a card with either mandatory caveat stripped |
| #118 | M018 FUZZ | 6 | Reintroducing PR #102's local-time bug fails the quota property suite |
| #120 | M019 LESSONS | 5 | 18 powered findings vs 14 gated `insufficient n`; regeneration byte-identical |

### The two findings worth reading first

**`docs/STATUS.md` shipped confidently wrong, and I caught it before it became a
habit.** Its "RECENT (Yesterday: 2026-08-17)" section listed five trials. The
catalog holds **zero** trials for that date; the runs it named actually executed
on 2026-08-13 and 2026-08-14. Cause: when the catalog legitimately returned
nothing for the reporting day, `status_generator.py:175` fell back to an
unfiltered all-time filesystem scan and printed it under yesterday's heading. An
empty day is a real finding — "nothing ran" is true and useful — and substituting
a different dataset for it is worse than rendering nothing, because it is
confidently wrong in the one file meant to be read without a terminal. Now gated
on catalog inaccessibility, labelled `trials_source`, and guarded by a test I
verified bites.

**`TrialConsumption.day` bucketed timezone-aware timestamps by local date** —
PR #102's defect surviving in a second location, found by M018's property tests
rather than by reading. Fixed in 7 lines. I confirmed the suite catches the
regression by reintroducing it.

### Where my own steering went wrong, recorded because it shaped the ledger

I told M015 that a clean sweep of CONFIRMED verdicts across invisible-surface
modules was suspicious. It responded by producing two DRIFTED rows claiming
`storm.py` and `status_generator.py` were "not imported or called anywhere in
`src/`" — refuted by one grep (`digest.py:29`, `status_generator.py:22`,
`automation.py:34`). I had pushed toward a conclusion instead of toward a method;
the corrected instruction was to establish unreachability by finding the absence
of a caller. The ledger keeps all of it — original, wrong correction, and
corrected correction — which is the right behaviour for an append-only record and
more useful than a clean sheet. The salvaged finding is sharper than either
verdict: `status_generator` was **wired but never run**, since nothing loads the
nightly schedule.

## BLOCKED: CI is not running — no loop PR can be merged

**Every pull-request workflow run since roughly 07:08 UTC fails in 2–3 seconds with
zero steps executed.** This is not a test failure and it is not any loop's code.

Evidence:

- `main` last ran green at **06:15 UTC**; nothing has been merged since.
- PRs **#117, #118, #119** — three unrelated leases — fail all five checks
  identically, in 2–3s, with `"steps": []` in the job API. Work never started.
- No branch modified `.github/workflows/**`.
- `uv.lock` is intact and `uv sync --frozen` succeeds locally, so this is not the
  missing-lockfile failure it superficially resembles.

**[INFERENCE]** The repository is private, so Actions minutes are metered, and
roughly 33 PRs ran five checks each tonight. Exhausted minutes or a tripped
spending limit produce exactly this signature — jobs that fail instantly with no
steps. I could not confirm it: the billing API needs a `user` token scope this
installation lacks, and GitHub attaches no message to the failed check runs.
Peter can settle it in one look at Settings → Billing → Actions.

Consequence, and the reason nothing was merged: the loop protocol's stop
condition for red CI is to note it, keep working locally, and never rebase onto
or merge on red. Three PRs are therefore finished-and-held, not abandoned. Each
carries local proof in its handoff instead of a green tick:

| PR | Mission | Local verification standing in for CI |
|---|---|---|
| #118 | M018 FUZZ | 1293 passed, ruff clean, `ty` 28; mutation-tested — reintroducing PR #102's local-time bug makes the quota property suite fail, so the tests genuinely bite |
| #117 | M015 AUDIT | ledger + evidence only, no source changes |
| #119 | M016 SURFACE | held for rework — see the STATUS.md defect below |

Once Actions runs again these need a fresh exact-head run before merge; none of
them should be merged on the strength of local runs alone.

## Now

`origin/main` is `e5d3257` (the night-loops prompt doc, committed on top of
`f836f6c` / PR #116). **30 PRs merged**, full suite **1272 passed, 1 skipped,
1 xfailed**, ruff clean, `ty` at its 28-diagnostic baseline. Zero open PRs and
zero active worktrees at dispatch time.

### Five loop missions dispatched tonight (M015–M019)

These are LOOPS, not builds. The standing risk after ~55 missions in days is
divergence — built ≠ proven ≠ used — so each mission re-verifies existing work,
extends it one step, hardens it with a test, and records. Max 6 cycles each,
one commit and one push per cycle, every cycle ends mergeable. Spec:
`docs/prompts/night-loops.md`.

| Mission | Loop | Lease | Provider |
|---|---|---|---|
| M015 | AUDIT — re-run merged handoffs' claims, verdict them | `research/audits/**` only | cursor |
| M016 | SURFACE — first real `docs/STATUS.md`, digest sections | `status_generator.py`, `digest.py`, goldens | antigravity |
| M017 | CARDS — eval cards from existing data | `cards.py`, `research/cards/**` | cursor |
| M018 | FUZZ — hypothesis properties per state machine | `tests/test_*_properties.py` | antigravity |
| M019 | LESSONS — aggregates with statistical gates | `lessons.py`, `sql/lessons.sql` | cursor |

Leases are mutually disjoint and clear of SG lanes (`authoring.py` internals,
`library/meta/`, `authoring/templates/`, `calibrate.py`). Cross-loop
coordination is append-only through `research/audits/board-notes.md`; no loop
edits another's files. Split 3/2 across two providers because four concurrent
Cursor streams hit `resource_exhausted` earlier tonight, and dispatched
staggered by 20s for the same reason. All five run as supervised processes with
`restart: on-failure`, which is safe precisely because the protocol rechecks
before extending and pushes every cycle — a restarted agent resumes rather than
repeats.

The one handshake: M019 exposes `lessons_digest_section()` for M016 to import.
M016 uses it only if it is already on `origin/main`, otherwise files a
board-note and does other work. Neither blocks on the other.

### What the earlier wave established (33 PRs — context for why these loops)

These five loops exist because the previous wave kept finding one class of
defect: code that was built, tested, and unreachable.

- **`NightlyCycle` was a hardcoded sequence**, which is why `parquet_compaction.py`
  (751 lines) and `lessons.py` (910 lines) were fully tested and completely dead.
  PR #106 replaced it with a declared step registry; both are now reached.
- **`storm.py` (517) and `status_generator.py` (484)** were likewise unreachable
  until PR #103 wired them into the digest and nightly path. `status_generator`
  has still never produced a file — that is M016's cycle 1.
- **The dashboard and the CLI disagreed about the same number.** Daily spend
  buckets were four hours off because the dashboard used local time while
  `quota.py` normalises to UTC (PR #102).
- **The verdict feature took three review rounds**, each defect the same shape:
  tests passed while reality disagreed. Real discovery ids
  (`D-20260815-KTXJSHGZ`) were rejected by a blanket ULID rule; writes persisted
  while reads queried a view that was never created; three tests passed only on a
  machine with Postgres.
- **The suite was writing to the live catalog.** `verdicts` held 583 rows when
  found and 768 by the time PR #116 isolated it — it grew during the verification
  runs themselves. On an append-only decision table that pollution is permanent
  by design. Those 768 rows are deliberately left in place; clearing an
  append-only audit trail is Peter's call, not a side effect of a test fix.

Planning consequence, now better evidenced: budget review rounds as part of the
mission. A single green CI run on a first head has not once been sufficient. And
a module's tests passing says nothing about whether anything calls it — which is
the entire premise of M015.

## Ready

- **Nothing is dispatchable-but-unstarted for a build worker.** All five slots
  are held by M015–M019 tonight. M009–M014 are merged and archived; the items
  below are what the v2 architecture audit left standing, and each needs an M
  number and a brief before dispatch.

## Next

Ranked by what actually blocks the lab, from the v2 architecture audit:

- **Queue leases and per-provider concurrency (E01, unassigned).** Dispatch is
  single-threaded with no concurrency control: no `running/<spec>.lease`
  heartbeat, no per-provider semaphore. This is the largest unbuilt item in the
  audit and it caps every parallel run the lab can attempt. Tonight's own
  dispatch is the evidence — provider concurrency limits had to be managed by
  hand, staggering launches by 20 seconds, because nothing in the queue does it.
- **Profiles CLI cutover and credential unification (E01, unassigned).** Two
  credential paths still coexist (`credentials.py` and `quota.py`), with a
  single `AgentProfile` specified but not cut over.
- **Analysis conclusions are not indexed into vector memory (unassigned).**
  LanceDB indexes tasks, trials, and trajectory steps — not what an analyst
  concluded. So "find analyses similar to this one" is not answerable, which is
  the query the memory exists for. Small, well-scoped, and the last structural
  gap in the study loop.
- **`craft.py` classify batching and cookbook idempotence (E07, unassigned).**
- **Operator board (E-board, unassigned).** Still no single read-only surface
  for what ran, what is running, what is queued, what is certifiable. M016's
  `docs/STATUS.md` is the cheap file-based answer to part of this; whether the
  full board is still wanted afterwards is a Peter question, not a build one.
- **`evallab tidy` is blind to squash merges (unassigned, found tonight).** It
  reported "Stale worktrees (0 items)" while five fully-merged worktrees held
  2.3 GB, because it tests ancestry and this repo squash-merges — so a merged
  branch tip is never an ancestor of `main`. Ancestry-true, content-false. A
  wrong answer in the other direction deletes live work, so this needs care
  rather than speed.

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

Five open items: three raised by the recent build waves, then the two carried
documentation and sequencing questions. Everything else on this board is a lane
decision.

- **Green-light real generation and analysis runs?** This is the one that
  matters. Every box in the study loop now exists — run experiment, capture
  trajectory, model studies it, thoughts stored, model builds evals, runs on
  Harbor — and the chain has never been run once end to end with a real model.
  `evallab analyst` refuses without `--model` by design, and the synthesis loop
  has only ever run with a stub generator. So the paper-derived >85% synthesis
  success rate, and any actual trajectory study, stay theoretical until tokens
  are spent deliberately. Nothing else on this board unblocks this; it is purely
  a spend decision.
- **Turn the nightly schedule on?** The pipeline exists and the step registry
  landed (PR #106), but `launchctl list` shows nothing loaded, so nothing runs
  unattended. Building the pipeline and enabling it were deliberately kept as
  separate decisions. Only free `oracle`/`nop` work can dispatch without a gate
  request, so switching it on does not itself spend.
- **Clear the 768 residue rows in `verdicts`?** The suite was writing to the live
  catalog until PR #116; the table held 583 rows when found and 768 by the time
  the fix landed, all of them test and verification residue rather than real
  decisions. Clearing is therefore safe, but the table is append-only precisely
  so that judgements cannot be rewritten, and truncating it must be an explicit
  human act rather than a side effect of a cleanup. Say the word and it goes to
  empty, so the next verdict is row one.

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
  mission — so it is Peter's, not the integrator's. The measured case,
  **re-measured at `e5d3257`** (the earlier figures were taken at `86380b0` and
  have since been overtaken; the direction of drift is the argument):
  - `src/evallab/cli.py` is now **2,117 lines**, up from 1,412 — it grew **705
    lines, 50%, in roughly 70 merged PRs** without the structure changing.
    Argparse wiring scaled with it: **154 `add_argument`** (was 111) and **60
    `add_parser`** (was 46).
  - There are still **zero `set_defaults(func=...)`**, so dispatch remains a
    linear string-comparison chain that every new command must edit in the same
    place. That is the sequencing point: the cost of conversion rises with every
    command added, and 14 new commands landed while the question sat open.
  - It is the **highest-churn file in `src/`**: **37 commits** by
    `git log --follow` (was 24), against 14 for the next file
    (`researchers.py`) and 13 for `schemas.py`.
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
| BOARD-REFRESH | `agents/handoffs/` holds only live missions, and the board states current truth with recorded follow-ups that cannot vanish with an archived file | Integration | Claude Opus 4.5, Oh My Pi | `.worktrees/board-refresh` / `role/board-refresh` | `agents/missions/ACTIVE.md`, `agents/handoffs/`, `agents/archive/`, `agents/handoffs/board-refresh.md` | none | every archived file reachable from `agents/archive/` with `git log --follow` intact; PR numbers, head SHAs, and merge states verified against `git log origin/main` and `gh pr list` | #55 | review | integrator |
