Status: review-wanted
Last: archived the five handoffs that merged today 1:1 by git mv into agents/archive/2026-08-15-handoffs/, rewrote agents/missions/ACTIVE.md to current truth, and carried four follow-ups onto the board as Mission candidate rows
Next: integrator review and merge of the BOARD-REFRESH PR; do not merge from this role
Blockers: none

# BOARD-REFRESH handoff

Agent/model: Claude Opus 4.5 (Anthropic), Oh My Pi harness, subscription-only.
Worktree `.worktrees/board-refresh` on `role/board-refresh`, branched from
`origin/main` `86380b0`. No paid model, cloud sandbox, Harbor run, Docker build,
deploy, or publication. No API-key environment variable read or introduced.
Nothing under `policy/` touched or weakened. No Python changed, no test changed,
no formatter run project-wide. `docker compose` was never invoked; the running
Postgres and Phoenix containers were left alone. No `evallab tick`.

## Lease, and what was written

Leased: `agents/missions/ACTIVE.md`, `agents/handoffs/`, `agents/archive/`, and
this file. Written, and nothing else:

- `agents/missions/ACTIVE.md` — rewritten.
- `agents/archive/2026-08-15-handoffs/INDEX.md` — second-sweep section added.
- `agents/archive/2026-08-15-handoffs/{m006-analysis-worker,m007-task-workbench,system-cartographer,perf-rebaseline,coord-gc}.md`
  — moved in, byte-identical.
- `agents/handoffs/board-refresh.md` — this file.

`src/`, `tests/`, `docs/`, `policy/`, `library/`, and `research/` were not
touched. Confirmed by `git show --stat` on both commits.

## `agents/STRUCTURE.md` needed no edit

No new top-level entry was created, so the root freeze is not engaged.
`STRUCTURE.md` already describes exactly what this mission did: `agents/archive/`
is documented as holding "dated closed-mission and registry records, plus dated
handoff subdirectories (spent handoffs are moved here 1:1 by `git mv`, never
edited or deleted, each with an INDEX.md)" (lines 23–26), and
`agents/handoffs/<role>.md` is documented as "live status, one file per **live**
mission only; finished ones move to archive/ (ACTIVE.md policy)" (lines 29–30).
This mission executed that documented behaviour inside an existing directory.

## Archiving

Before: 5 files in `agents/handoffs/` (`coord-gc.md`,
`m006-analysis-worker.md`, `m007-task-workbench.md`, `perf-rebaseline.md`,
`system-cartographer.md`). After: 1 (`board-refresh.md`, this mission).

Archive path used: **`agents/archive/2026-08-15-handoffs/`** — today's existing
dated directory, not a new one. Today's date is 2026-08-15, which is the date
already on that directory, and the assignment said to open a new dated directory
only "if the date differs". Same scheme as the first sweep: 1:1 `git mv`,
original filenames, no file edited, merged, truncated, or deleted. Note that
`gh pr list` timestamps #53 and #54 as `2026-08-16` UTC; local date is
2026-08-15, and the directory follows local date, consistent with the first
sweep on the same day.

The first sweep's `INDEX.md` opened with "Historical record only — do not
update". That line was corrected rather than obeyed, because obeying it was not
possible without leaving the archive lying: its "Still live in
`agents/handoffs/`" section named all five of these missions as live. The
archived handoffs stay frozen; the index that points at them has to stay true
when the directory gains files. It now says so explicitly.

### Merge state verified per file, not taken from the assignment's list

All five were checked against `git log origin/main` and
`gh pr view <n> --json headRefOid,mergeCommit`. All five are genuinely merged;
**no file was found whose mission is not merged**, so nothing was held back.

| Archived file | Mission | PR | Head | Merge commit |
|---|---|---|---|---|
| `m006-analysis-worker.md` | M006 | #47 | `3b15e25` | `4d23d7d` |
| `m007-task-workbench.md` | M007 | #49 | `4d47054` | `86380b0` |
| `system-cartographer.md` | system cartography | #52 | `a408881` | `1471f41` |
| `perf-rebaseline.md` | ingest budget re-baseline | #53 | `a12ea3c` | `e080dd0` |
| `coord-gc.md` | coordination GC | #54 | `a41266e` | `2173268` |

Head SHAs are `headRefOid` from `gh`, not from the handoffs. Neither M006 nor
M007 names its own final head: M006's header points at `d454bbe` and M007's last
round at `1713830`, each the last *code* commit, with the merged head being the
handoff/doc commit on top. `git merge-base --is-ancestor d454bbe 3b15e25` and
`git merge-base --is-ancestor 1713830 4d47054` both succeed, so the handoffs do
not contradict `git log origin/main`; they simply predate their own final
commit. `git log origin/main` is the authority recorded on the board.

### History survives the moves

`git show --stat -M` on the archive commit reports all five as pure renames with
zero content change:

```text
 agents/archive/2026-08-15-handoffs/INDEX.md        | 55 +++++++++++++++++-----
 .../2026-08-15-handoffs}/coord-gc.md               |  0
 .../2026-08-15-handoffs}/m006-analysis-worker.md   |  0
 .../2026-08-15-handoffs}/m007-task-workbench.md    |  0
 .../2026-08-15-handoffs}/perf-rebaseline.md        |  0
 .../2026-08-15-handoffs}/system-cartographer.md    |  0
```

`git log --follow --oneline agents/archive/2026-08-15-handoffs/m006-analysis-worker.md`:

```text
c921b88 BOARD: archive the five handoffs that merged today
4d23d7d M006: add guarded post-trial analysis worker (#47)
```

The history resolves across the rename back to the merge commit that introduced
the file. Concatenation into a single archive file was rejected for exactly this
reason and would have destroyed it.

## Board rewrite

`Now` states `origin/main` `86380b0`, no active implementation mission, PR #51
(`role/orchestrator-handoff`, head `874f568`) as the only open PR with its
disposition explicitly unsettled, and M009 in flight in `.worktrees/m009-flight`
as an integrator acceptance exercise that consumes no build slot. M006 and M007
moved to `merged` with PR numbers and final heads. Both fences are stated where
a planner will hit them: M006's calibration gate CLOSED with `_no_adapter` and a
saved-response-only stage 5, and M007 granting nothing (`admission_granted`
false, `library/registry/` empty).

Review cost is recorded as **four review-and-repair rounds each**, citing the
round headings in the archived handoffs so the count is checkable: M006 at lines
76, 142, 208, 327 (last two independent exact-head reviews of `1f4cf6f` and
`95d31e4`); M007 at 211, 270, 629, 854 (last three independent). The
integrator's brief said three for M006; the archived record shows four, because
the first two M006 rounds preceded the current integrator session. The
integrator confirmed the checkable framing over their own count. M007's
withdrawal round (line 583, `2a6aec0`) is recorded as a **distinct** item, not
folded into a repair round: it withdrew an already-committed certification
packet that asserted isolation for control runs whose verifier had full network
egress, and it changed zero `.py` files.

`Next` sequences M009 (in flight) → M010 + M011 in parallel → M012 → M013, with
M014 as later hardening, lanes taken from the dispatch order in
`docs/prompts/next-functionalization-missions-2026-08-15.md:20-48`.

## Follow-ups carried onto the board

Four `Mission candidate` rows under `Next`, each with lane and rationale and
each explicitly not active work. One of the four was already on the board from
the first sweep and was **carried forward rather than duplicated**: the `ingest`
perf metric / `initialize()`-inside-the-timed-region row, now also carrying the
second-re-baseline requirement and the non-comparable-series warning. I grepped
the pre-existing board for `observator|calibration|harbor|verifier|initialize`
before adding anything; only that one row matched, so the other three
(container-level verifier build observation — Tasks + Platform; Harbor in CI so
the live drift comparison stops skipping — Platform; calibration ground truth
mostly unscored — Research) are new and not duplicates. The assignment expected
two to be pre-existing; measured, it was one.

## Needs Peter: exactly two items

(a) the `docs/*.html` keep-or-archive question, carried unchanged; (b) whether
to sequence a `cli.py` command-registry conversion before M012.

Every figure in (b) was re-measured at `86380b0` rather than copied, and two of
the numbers in the brief did not survive:

- `cli.py` is **1,412 lines**, not 1,355 — M006 added 57 lines to it after that
  count was taken. Recorded with the correction visible.
- The flat `if args.command == ...` chain is about **490 lines**, not 475
  (`run_cli()` at lines 873–1372, first branch 884, last 1336). `set_defaults`
  appears **0** times. `parser()` is a 402-line pure-argparse function (lines
  133–534) with 111 `add_argument`, 46 `add_parser`, 7 `add_subparsers`; 892 of
  1,412 lines (63%) are wiring plus string dispatch. "~90% argparse wiring" was
  not reproducible as a line fraction, so the measured breakdown replaced it.
- Highest churn in `src/` confirmed: 24 commits by `git log --follow`, next is
  `researchers.py` at 14, then `schemas.py` at 13.
- **The shared-file designation is not in `policy/`.** `grep -rn "cli\.py"
  policy/` returns nothing. The additive-only shared-file rule for `cli.py`
  lives in `docs/prompts/overnight-missions.md:48`, a dated dispatch brief. The
  board now says so, and notes that if the rule is meant to bind it currently
  has no binding home. This is a correction to the assignment's premise.
- Rebase stall confirmed: `agents/archive/2026-08-15-handoffs/pipeline.md:2`.
- M007's routing-around confirmed: 6,577 insertions across 47 files, a
  3,238-line `task_workbench.py` and a 1,580-line test, `cli.py` touched **0**
  times, its own `__main__` at `task_workbench.py:3237`. I did not repeat the
  "second feature to route around the CLI" ordinal, because measurement gives a
  different and checkable fact: `src/evallab/` now has four modules with their
  own `__main__` (`calibrate.py`, `cli.py`, `smoke.py`, `task_workbench.py`)
  while `[project.scripts]` exposes only `evallab`. M006, by contrast, did route
  through the CLI (+57 lines).

## Two facts found while verifying, recorded on the board

Neither was in the assignment; both are things a reader of this board would
otherwise get wrong.

- **Branch sunset is incomplete and undetectable by the documented test.**
  Worktrees were removed for all five merged missions, but `origin` still
  carries 18 `role/*` branches, 17 of them spent (all but
  `role/orchestrator-handoff`). `agents/WORKFLOW.md`'s "zero commits ahead of
  `main`" rule finds none of them, because every merge was a squash merge:
  `role/solidify` reports 44 commits ahead, `role/m007-task-workbench` 17,
  `role/m006-analysis-worker` 10. Deleting refs is integrator sunset work and
  this lease is files, not refs, so it is recorded, not done.
- **PERF-REBASELINE's executing agent/model is unrecoverable.** Its archived
  handoff names no agent or model anywhere (`grep -niE
  "gemini|grok|claude|codex|gpt|opus|sonnet"` returns nothing), so its board
  cell says "not recorded" instead of the previous board's unsupported
  "free-tier worker; recorded in handoff".

## Worktree census recorded on the board

Five worktrees besides the primary checkout, measured by `git worktree list`:
`board-refresh` (this mission), `m009-flight` (M009), `orchestrator-handoff`
(PR #51, 1 ahead), `organization-prompts` (4 ahead / 14 behind, no PR),
`wave4-prompts` (1 ahead / 17 behind, no PR). The "down to four" count predates
`board-refresh` and `m009-flight`. The primary checkout at `~/Developer/eval-lab`
was never entered for any write and remains on `main` at `b5c29a8`.

## Verification

Content-only mission: no Python, no tests, no fixtures changed, so there is
nothing for `pytest` or `ruff` to cover in this diff. Verification is the merge
evidence above plus rename preservation, both reproducible from the commands
quoted. Full validation is the Integrator's per the mission constraints.
