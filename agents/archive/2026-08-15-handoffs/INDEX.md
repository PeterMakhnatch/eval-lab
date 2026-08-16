# Archived handoffs (2026-08-15)

The archived handoffs in this directory are frozen — never edited, merged, or
deleted. This index is not frozen: it must stay true when the directory gains
files, and it gained a second same-date batch (see **Second sweep** below).
Live status: `agents/handoffs/`. Live board: `agents/missions/ACTIVE.md`.
Ownership: `agents/OWNERS.md`.

`agents/missions/ACTIVE.md` states the standing policy: *"Finished missions
move to `agents/archive/` with a date prefix."* That policy had lapsed. Its
**first sweep** (COORD-GC, PR #54, `2173268`) executed the policy for the 34
spent handoff files that were still sitting in
`agents/handoffs/`, where 21 of them still advertised open, blocked, or
`review-wanted` work for missions that had already merged — the first thing
every new agent reads.

**Scheme.** One dated subdirectory, `agents/archive/2026-08-15-handoffs/`, with
each handoff moved **1:1 by `git mv`** under its original filename. No file was
edited, merged, truncated, or deleted, so `git log --follow` resolves every
moved path across the rename. A consolidated single-file archive was rejected
precisely because concatenation destroys per-file rename detection.

Authority for "finished": `agents/archive/2026-08-15-role-registry.md` (the
frozen final role table, closed by M001),
`agents/archive/2026-08-15-missions-m001-m004-a001.md`,
`agents/archive/2026-08-15-missions-m005-m008.md`, and `git log origin/main`
merge evidence.

## Retired codename roles (25)

Superseded by the four permanent lanes in `agents/OWNERS.md` when M001 (PR #41,
merge `06c069c`) replaced the codename roster with the numbered mission board.
Each role's final status is its row in `2026-08-15-role-registry.md`; the
registry is the authority, this table is the pointer.

| Handoff | Role row in the frozen registry |
|---|---|
| `adapter.md` | ADAPTER — done, QuixBugs adapter + evidence merged |
| `analyst.md` | ANALYST — done, briefs 01–03 integrated |
| `autopilot.md` | AUTOPILOT — done, bounded researcher loop integrated |
| `curator.md` | CURATOR — done, 19 verified tasks on `main` |
| `dashboard.md` | DASHBOARD — done, PRs #11 and #15 |
| `data-strategy.md` | DATA-STRATEGY — done, PRs #30 and #32 |
| `evidence.md` | EVIDENCE — done, calibration corpus + labels merged |
| `fetch.md` | FETCH — done, PRs #13 and #18 |
| `forge.md` | FORGE — done, PR #6 |
| `ingest.md` | INGEST — done, PR #3 |
| `inspector.md` | INSPECTOR — done, PR #19 |
| `judge.md` | JUDGE — done, PR #4 (judge stays below the 0.90 floor) |
| `medic.md` | MEDIC — done, PRs #7, #6, #8 |
| `mender.md` | MENDER — done, PR #22 |
| `observer.md` | OBSERVER — done, PR #2 |
| `pipeline.md` | PIPELINE — done, PR #17 (duplicate #16 closed) |
| `program.md` | PROGRAM — merged as PR #37 (`078dd7b`); registry row still reads "Building 2026-08-15" and was frozen mid-flight |
| `recon.md` | RECON — done, capability map + demos merged |
| `reframe.md` | REFRAME — done, PRs #9 and #10 |
| `register.md` | REGISTER — done, PR #36 (`447f6a8`) |
| `retention.md` | RETENTION — done, PRs #12 and #21 |
| `runner.md` | RUNNER — done, PR #5 |
| `solidify.md` | SOLIDIFY — done, PR #34 |
| `speed.md` | SPEED — done, PRs #14 and #20 |
| `truth.md` | TRUTH — done, PR #29 |

## Numbered missions already archived (5)

Their outcome records are in `2026-08-15-missions-m001-m004-a001.md` and
`2026-08-15-missions-m005-m008.md`; only the handoff files were left behind.

| Handoff | Mission | PR | Merge |
|---|---|---|---|
| `a001-state-audit.md` | A001 residual branch/worktree inventory | #40 | `4937922` |
| `m001-governance.md` | M001 lanes-and-missions governance | #41 | `06c069c` |
| `m002-operability.md` | M002 completion-to-analysis slice | #42 | `4f0824e` |
| `m003-profiles.md` | M003 subscription profiles + credential preflight | #43 | `128db8d` |
| `m005-explorer.md` | M005 read-only run explorer | #44 | `4ddc548` |

## Roles in no registry at all (4)

These four were in neither the frozen role registry nor any archived mission
record. Each was investigated against `git log origin/main`; all four are
finished, and each one's stale header was actively misleading.

| Handoff | What it actually was | Evidence | Why its header was wrong |
|---|---|---|---|
| `greenline.md` | An ad-hoc CI-coverage mission: made default `pytest` collection cover `dashboard/tests/`, `research/analysis/tests/`, and `research/calibration/tests/` (263 tests), and added `tests/test_ci_coverage.py` so an omitted committed test module fails the build, plus `tests/test_program_contract.py`. Ran between the codename wave and M001, so it never got a registry row. | `b2e2898 GREENLINE: make CI cover the repo (#38)` | `Status: review-wanted`, `Blockers: REGISTER PR #36 is open; do not self-merge GREENLINE #38`. Both #36 and #38 merged; the blocker was resolved by the merge it was waiting for. |
| `integrator-closeout.md` | **M008 under a non-M-numbered filename.** Integrator bookkeeping: reconciled the reviewed queue, archived the prior wave, published the M005–M007 prompts, and fixed `scripts/fleet-status.sh`, which classified a zero-commits-ahead branch as spent before checking its attached worktree and so reported M005 spent while it held six uncommitted implementation paths. | `d6cb26b M008: reconcile integration queue and fleet state (#45)`; outcome row in `2026-08-15-missions-m005-m008.md` | `Status: review-wanted`, `Next: open M008 PR`. The PR was opened and merged; the file was never renamed to `m008-*`, which is why the M008 archive sweep missed it. |
| `observatory.md` | A calibration-record production role: produced 25 draft completed-trial records in three batches with a 2-of-10 self-audit protocol and the `observatory-1` template, scoring 8/8 fields on the two in-repo labeled trials. Predates M001 governance; no registry row. | `396a422` (#26), `82f594b` (#27), `4f6fdc1 OBSERVATORY: last three completed-trial draft records (#28)` | `Status: review-wanted`, `Next: PR OBSERVATORY: remaining 3 draft records`. That PR is #28 and merged. Its one real residual is recorded below. |
| `program-repair.md` | A follow-up repair to PROGRAM (#37): corrected the experiment ledger's scientific record — batch-level verifier attribution, the illegal proposal to mount `tests/test_outputs.py` into the evaluated agent image, failure counts re-derived from observation text, and explicit inherited/unresolved provenance labels for studies whose only execution record lived in removed worktrees. 17 validator regressions. | `9ed9874 PROGRAM-REPAIR: correct scientific record (#39)` | `Status: review-wanted`, `Next: Push and open the PR`. Pushed, opened, and merged as #39. |

### Carried-forward residual, not closed by archiving

`observatory.md` records that 23 of its 25 trajectory labels point at
`harbor-practice/` source paths that do not exist inside this repository, so
those labels were never scored — its 8/8 agreement figure covers only the two
in-repo labeled trials. That is a live gap in calibration ground truth, owned by
the Research lane, and it survives this archiving. Archiving the handoff records
where the gap came from; it does not resolve it.

## Second sweep (2026-08-15, BOARD-REFRESH)

Five more handoffs went spent the same day, after the first sweep was written.
Same date, so they land in this same directory under the same scheme — 1:1
`git mv`, original filenames, no edits — rather than in a near-duplicate
directory. Every mission below was verified merged against `git log origin/main`
and `gh pr list --state merged`; none was assumed spent from a keep-list.

| Handoff | Mission | PR | Head reviewed/merged | Merge commit |
|---|---|---|---|---|
| `m006-analysis-worker.md` | M006 guarded post-trial analysis worker | #47 | `3b15e25` | `4d23d7d` |
| `m007-task-workbench.md` | M007 task-quality workbench | #49 | `4d47054` | `86380b0` |
| `system-cartographer.md` | System cartography report | #52 | `a408881` | `1471f41` |
| `perf-rebaseline.md` | `ingest` CI perf budget re-baseline | #53 | `a12ea3c` | `e080dd0` |
| `coord-gc.md` | Coordination-artifact garbage collection | #54 | `a41266e` | `2173268` |

`system-cartographer.md` was the first sweep's one knowingly retained exception
— spent since PR #52 (`1471f41`) but named on COORD-GC's keep-list. That
exception is now closed.

Neither `m006-analysis-worker.md` nor `m007-task-workbench.md` names its own
final head: M006's header points at `d454bbe` and M007's last round at
`1713830`, each the last *code* commit, with the merged head being the handoff
commit on top (`git merge-base --is-ancestor` confirms both). `git log
origin/main` is the authority for the merged SHAs in the table above.

Two residuals survive this sweep and are recorded as `Mission candidate` rows in
`agents/missions/ACTIVE.md` so they cannot vanish with an archived file: M007's
static-text verifier-build scan wanting a container-level observation and its
live Harbor drift comparison skipping for want of Harbor in CI, and the
perf-harness `initialize()` measurement region. The `observatory.md`
calibration-ground-truth residual recorded above is now on the board too.

## Still live in `agents/handoffs/`

`board-refresh.md` only — the mission that wrote this section. No
implementation mission is live as of this sweep.
