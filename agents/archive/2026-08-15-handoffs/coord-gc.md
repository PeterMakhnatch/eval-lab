Status: review-wanted
Last: archived 34 spent handoffs 1:1 by git mv, corrected the board, made STRUCTURE.md true, retired the stale CLI claims and the prompt index
Next: integrator review and merge of PR #54; do not merge from this role
Blockers: none

# COORD-GC handoff

Agent/model: Claude Opus 4.5 (Anthropic), Oh My Pi harness, subscription-only.
Worktree `.worktrees/coord-gc` on `role/coord-gc`, branched from `origin/main`
`1471f41`. No paid model, cloud sandbox, Harbor run, Docker build, deploy, or
publication. No API-key environment variable read or introduced. Nothing under
`policy/` touched or weakened. No Python changed.

Wrote only the leased paths: `agents/handoffs/`, `agents/archive/`,
`agents/missions/ACTIVE.md`, `agents/STRUCTURE.md`, `docs/prompts/README.md`,
`docs/checkpoints/2026-08-14.md`. Did not touch `src/`, `tests/`, `policy/`,
`library/`, `research/`, `digests/`, or `scripts/`.

## What this mission was

Executing standing policy that had lapsed, not inventing policy.
`agents/missions/ACTIVE.md` already said: *"Finished missions move to
`agents/archive/` with a date prefix."* It had stopped happening, so the
coordination layer had become the least trustworthy thing in the repository —
and it is the first thing every new agent reads.

## 1. Archive scheme and result

**Scheme: one dated subdirectory, `agents/archive/2026-08-15-handoffs/`, with
each handoff moved 1:1 by `git mv` under its original filename, plus an
`INDEX.md`.**

Chosen over consolidating into dated `.md` files for a concrete reason:
concatenating 34 files into one is not a rename, so git rename detection dies
and `git log --follow` stops resolving them. The acceptance criterion for this
mission was that history survive; only 1:1 moves deliver that. No file was
edited, truncated, or deleted — every archived line is still in the tree.

`agents/handoffs/` before: **35 files, 3,381 lines.**
`agents/handoffs/` after: **2 files, 8 + this file** (`system-cartographer.md`,
`coord-gc.md`).
Moved: **34 files, 3,373 lines**, unchanged in content.

No new top-level entry was created (`agents/archive/` already existed), so no
root-freeze exception was needed — but `agents/STRUCTURE.md` was edited anyway,
for the reasons in §3.

History proof:

```
$ git log --follow --oneline -- agents/archive/2026-08-15-handoffs/solidify.md
7b9224c COORD-GC: archive 34 spent handoffs into agents/archive/2026-08-15-handoffs/
5c66352 SOLIDIFY: record merged closeout (#35)
fefabe3 SOLIDIFY: harden the evaluation loop (#34)
```

Authority for "finished": `agents/archive/2026-08-15-role-registry.md` (frozen
by M001), the two archived mission records, and `git log origin/main` merge
evidence. Accounting closes exactly: 25 retired codename roles + 5 already
archived M-numbered missions + 4 with no registry row = 34, plus
`system-cartographer.md` kept = the 35 files that were there.

Nothing in the codebase reads a handoff by name, so the moves are safe:
`scripts/fleet-status.sh` derives `$wt/agents/handoffs/${branch#role/}.md` from
the branch of a live worktree, and `src/evallab/researchers.py` globs
`agents/handoffs/*.md`. Both degrade to "fewer rows", which is the point.

### The four with no registry row

All four are finished, and all four had stale headers advertising open work.

- **`greenline.md` — an ad-hoc CI-coverage mission.** Merged `b2e2898`
  (PR #38). Made default `pytest` collection cover `dashboard/tests/`,
  `research/analysis/tests/`, and `research/calibration/tests/` (263 tests),
  and added `tests/test_ci_coverage.py` so an omitted committed test module
  fails the build, plus `tests/test_program_contract.py`. It ran between the
  codename wave and M001, which is why it has no registry row. Its header still
  read `Blockers: REGISTER PR #36 is open; do not self-merge GREENLINE #38` —
  both merged (#36 is `447f6a8`), so the blocker was cleared by the very merge
  it was waiting on.
- **`integrator-closeout.md` — M008 under a non-M-numbered filename.** Merged
  `d6cb26b` (PR #45), with an outcome row already in
  `agents/archive/2026-08-15-missions-m005-m008.md`. Integrator bookkeeping:
  reconciled the reviewed queue, archived the prior wave, published the
  M005–M007 prompts, and fixed `scripts/fleet-status.sh`, which classified a
  zero-commits-ahead branch as spent before checking its attached worktree and
  so reported M005 spent while it held six uncommitted implementation paths.
  The file was never renamed to `m008-*`, which is exactly why the M008 archive
  sweep missed it — a naming-convention gap, not a judgement call.
- **`observatory.md` — a calibration-record production role.** Merged as
  `396a422` (#26), `82f594b` (#27), `4f6fdc1` (#28). Produced 25 draft
  completed-trial records in three batches under a 2-of-10 self-audit protocol
  with the `observatory-1` template. Predates M001 governance. Its `Next: PR
  OBSERVATORY: remaining 3 draft records` is PR #28, merged.
- **`program-repair.md` — a follow-up repair to PROGRAM (#37).** Merged
  `9ed9874` (PR #39). Corrected the experiment ledger's scientific record:
  batch-level verifier attribution, the illegal proposal to mount
  `tests/test_outputs.py` into the evaluated agent image, failure counts
  re-derived from observation text, and explicit inherited/unresolved
  provenance labels for studies whose only execution record lived in removed
  worktrees. 17 validator regressions.

**One residual survives archiving and is recorded in the INDEX, not closed
by it:** `observatory.md` states that 23 of its 25 trajectory labels point at
`harbor-practice/` source paths that do not exist in this repository, so they
were never scored — its 8/8 agreement figure covers only the two in-repo
labeled trials. That is a live Research-lane gap in calibration ground truth.
Archiving records where it came from; it does not fix it.

## 2. Board corrections

The board claimed *"No implementation branch is active or at review"* with M006
and M007 both at PR `—` and State `ready`. Corrected to the observed facts:
M006 is PR #47 on `role/m006-analysis-worker`, reviewed at exact head
`1f4cf6f`; M007 is PR #49 on `role/m007-task-workbench`, reviewed at exact head
`c6c35a4`; both judged `incorrect` by independent exact-head review and returned
for bounded repair, so both now require *fresh* exact-head CI on the repaired
head. PR #52 (`1471f41`) recorded as the merged cartography mission. `Ready` is
now empty, because nothing was dispatchable-but-unstarted.

Registered the two maintenance missions dispatched without M numbers. The
PERF-REBASELINE row is from its own worker's correction over coordination, not
my inference: lease is exactly `scripts/profile/budgets.json`,
`docs/engineering.md`, `agents/handoffs/perf-rebaseline.md` — harness and
workflow change was an explicit non-goal — and its state is `review` at PR #53
head `a12ea3c`, not active. Also stated explicitly that the two repair efforts
are assignments *inside* the existing M006/M007 rows: no new branch, worktree,
lease, or row, and no renumbering.

Carried one Platform mission candidate onto the board so it does not vanish with
an archived handoff: the `ingest` perf metric times `initialize()` inside the
measured region (full `sql/schema.sql` DDL replay plus a second fresh
connection), so it does not measure ingest logic and carries most of the
variance. Re-baselining treated the symptom. I recorded it on the board rather
than in `docs/engineering.md` because that file is another mission's lease.

`Needs Peter` carries exactly one item, as instructed: keep or archive
`docs/agent-workflow.html`, `docs/eval-rd-roadmap.html`,
`docs/repository-state.html`, `docs/system-cartography.html`, and
`docs/repository-overview.css`. Reference status was measured, not assumed, and
it is not uniform — the first three plus the CSS are referenced only by each
other, but `system-cartography.html` is additionally named by
`docs/checkpoints/2026-08-15-system-cartography.md` and
`docs/prompts/system-cartographer-2026-08-15.md`, so it has an authored owner
the other three lack. No committed code generates any of them (`src/`,
`scripts/`, `dashboard/`, `Makefile`, `.github/` contain no reference). Neither
moved nor deleted — Sponsor's call.

## 3. Structure map

`dashboard/` has existed at the repository root since PRs #11 and #15 and
appeared **nowhere** in `agents/STRUCTURE.md`. That is the sharp finding: the
root-freeze rule was being enforced against a map that silently omitted a
top-level entry. Added as Platform lane per `agents/OWNERS.md`, with the reason
it is not part of `src/` (presentation surface over committed evidence, never an
execution path).

The `docs/` submap named 6 of the 28 committed entries; all 28 are now named
verbatim, including `docs/checkpoints/`, `docs/research/`, and the four `.html`
files with their CSS. Also added `agents/CHECKS.md` and `.github/` (both
previously absent), the root config dotfiles, dated handoff subdirectories under
`agents/archive/`, and the statement that `agents/handoffs/` holds live missions
only. Refreshed the ownership label on `src/ tests/ sql/ scripts/` from
"BUILDER-owned" to Platform lane, since M001 retired the BUILDER codename.
Dated change-log entry added. **The repository was not restructured — only the
map changed.**

Verified mechanically, and this is the check to re-run after any `docs/` change:

```
$ for e in $(git ls-tree origin/main docs/ --name-only | sed 's|docs/||'); do
    grep -q -- "$e" agents/STRUCTURE.md || echo "MISSING: $e"; done
(no output — all 28 entries named)
```

## 4. Retired claims

`docs/checkpoints/2026-08-14.md` is a dated historical record, so nothing in it
was rewritten. Two marked `Correction 2026-08-15 (COORD-GC)` blocks were added
inline at the wrong claims, with the originals intact, plus a header note
stating the convention.

- "22 CLI commands" → measured on `1471f41` by building the real parser
  (`from evallab.cli import parser`) and walking its subparsers: **29 top-level
  groups, 42 registered commands** (29 top-level + 13 nested under `schedule`,
  `canary`, `report`, `analyze`, `db`, `registry`), of which 36 are
  leaf-invocable. The 29-group figure independently matches what the system
  cartographer reported for PR #52.
- "`gc`, `fetch`, `dashboard` are not in the CLI" → **resolved.** All three are
  registered in `src/evallab/cli.py` (`dashboard` 148, `fetch` 437, `gc` 466)
  with dispatch in `run_cli` (878, 865, 1036), and all three appear in the
  built parser's top-level choices.

`docs/prompts/README.md` indexed only briefs 01–04. Now indexes every committed
file: briefs 01–09 and 12, and the five dated mission-prompt sets. Two things
worth flagging beyond the mechanical fix:

- **Briefs 10 and 11 do not exist and must not be renumbered.** Brief 10 was
  the deferred LanceDB memory layer (`docs/design-additions.md` still says
  "deferred until brief 10"; no `memory` dependency group in `pyproject.toml`,
  so it never ran). Brief 11 was the Streamlit surface
  (`docs/fleet-tracking.md` refers to "the Streamlit app (brief 11)"); no brief
  was written, and the surface that exists is `dashboard/` from PRs #11/#15 —
  so `dashboard/` is a capability that landed with no brief behind it, which is
  consistent with it also being missing from the structure map. Live docs point
  at both numbers, so closing the gap by renumbering would break them.
- **The prompt-generation question has a two-part answer, not one file.** The
  board is authoritative over any prompt set. `functionalization-missions-2026-08-15.md`
  (which the board named) specifies M005–M007, the missions in flight; the later
  `next-functionalization-missions-2026-08-15.md` (PR #48) holds the M006-R
  repair prompt and the unstarted M009–M014 plan. Neither supersedes the other
  wholesale, so the board now says exactly that instead of naming one file.

Attribution in the brief table is limited to what the frozen registry states
explicitly; `—` marks briefs it does not name, rather than guessing.

## Verification

Capability labels used per the mission contract:

- Archive moves, `git log --follow` survival, the 28-entry `docs/` submap check,
  and the 29-group/42-command CLI measurement: **proven live** in this worktree,
  commands and output above.
- `gc`/`fetch`/`dashboard` wiring: **proven live** — registration and dispatch
  read in `src/evallab/cli.py` and confirmed present in the built parser's
  top-level choices.
- The board's PR/branch/head facts for M006, M007, #52, #53: **pending in PR** —
  taken from the integrator's dispatch and, for PERF-REBASELINE, from that
  mission's own worker; I did not query GitHub.
- No test suite was run and no Python changed, so `uv run ruff check .` and
  `uv run pytest` were not exercised by this mission and cannot have been broken
  by it. Diff is Markdown plus renames only.

## Boundaries observed

No conflict with another mission's paths. `agents/handoffs/perf-rebaseline.md`,
`agents/handoffs/m006-analysis-worker.md`, and
`agents/handoffs/m007-task-workbench.md` belong to their own missions and arrive
with PRs #53, #47, and #49; I neither created nor moved them, and my archive
sweep predates them. The primary checkout was never written to — one read-only
`git worktree list` / `git branch` enumeration only, no `pull`, `reset`,
`clean`, or `checkout`. Stopping at `review-wanted`: no merge, no squash, no
rebase onto main.

One knowing deviation, flagged rather than silently taken:
`system-cartographer.md` is itself spent (PR #52 merged `1471f41`) but the
keep-list named it, so it stays live and is queued for the next Integration
sweep on the board. `agents/handoffs/` therefore holds one finished mission by
instruction, not by oversight.
