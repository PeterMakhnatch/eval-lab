# Active-state audit — 2026-08-15

Evidence-backed disposition of leftover Eval Lab worktrees and non-`main`
branches. **No worktree was removed, no branch was deleted or merged, no
recovery command was executed.** This file only records and recommends.

Snapshot taken after `git fetch origin` at 2026-08-15T17:04:53Z.
`origin/main` = `9ed987403462df2f752b3001dfde3bf9aa66096e`
(`PROGRAM-REPAIR: correct scientific record (#39)`).

## How a row was classified

Not from squash-merge ancestry alone. For every ref:

1. `git rev-list --left-right --count origin/main...REF` (behind / ahead).
2. `git cherry origin/main REF` (`+` = patch unique vs main; `-` = equivalent
   patch already on main).
3. `git diff --name-only origin/main...REF` for unique paths.
4. For each path blob on the ref: exact `origin/main:path` match, else
   `git log origin/main --find-object=<blob>`. “Already on origin/main?” is
   **yes** only when every differing path’s blob is found that way.

| Label | Rule |
|---|---|
| retain for review | Unique blobs not on `origin/main`, or uncommitted work, or a dirty/diverged main checkout. |
| safe cleanup candidate after integrator review | Clean HEAD; unique path blobs all exist on `origin/main`; leftover is a spent branch/worktree. Integrator still spot-checks two rows before acting. |
| unresolved | Inventory incomplete or conflicting signals (not used in this snapshot). |

Handoff-only closeout branches (paths are `agents/ROLES.md` + a role handoff)
are called out separately from **Observatory / research records**. Observatory
trial files **are already on `origin/main`** (`research/observations/`, 27
paths). Cleaning the spent Observatory *branches* is not the same as discarding
those records.

## Disposition table

Ahead/behind is vs `origin/main` (behind, ahead), taken from
`git rev-list --left-right --count origin/main...REF` (tab-separated in the
analysis log). Dirty is the worktree working tree if one exists; branches
without a worktree are “n/a”.

| Kind | Name / path | HEAD | Dirty | Behind / ahead | Cherry (+ / −) | PR | Unique paths (count) | Unique bytes already on origin/main? | Class |
|---|---|---|---|---|---|---|---|---|---|
| worktree | `~/Developer/eval-lab` (`main`) | `b5c29a8` | **yes** (see Main checkout) | 4 / 1 | +1 / −0 | none for this local digest commit | `digests/2026-08-15.md` | **no** (`4bef77a047e4` not on main) | retain for review |
| worktree + branch | `.worktrees/a001-state-audit` `role/a001-state-audit` | `9ed9874` | this audit’s uncommitted files only at snapshot; then the two owned files | 0 / 0 at snapshot | +0 / −0 | this PR | this audit only | n/a (new) | retain for review |
| worktree | `.worktrees/m001-governance` `role/m001-governance` | `9ed9874` | **yes** (uncommitted governance draft) | 0 / 0 | +0 / −0 | none | uncommitted: `agents/{ROLES,STRUCTURE,WORKFLOW}.md`, `docs/operating-manual.md`, `scripts/fleet-status.sh`, `agents/OWNERS.md`, `agents/archive/`, `agents/missions/` (includes `ACTIVE.md`), `agents/handoffs/m001-governance.md`, `.github/pull_request_template.md`, `tests/test_fleet_status.py` | uncommitted work is **not** on main | retain for review |
| worktree | `.worktrees/m002-operability` `role/m002-operability` | `9ed9874` | no | 0 / 0 | +0 / −0 | none | none | n/a | safe cleanup candidate after integrator review |
| worktree + branch | `.worktrees/organization-prompts` `role/organization-prompts` | `4627feb` | no | 0 / 2 | +2 / −0 | **none** | `docs/prompts/dispatch-now-2026-08-15.md`, `docs/prompts/mission-system-reset-2026-08-15.md` | **no** | retain for review |
| worktree + branch | `.worktrees/wave4-prompts` `role/wave4-prompts` | `15dbf28` | no | 3 / 1 | +1 / −0 | **none** | `docs/prompts/wave4-followons-2026-08-15.md` | **no** | retain for review |
| worktree + remote | `.worktrees/data-strategy` `role/data-strategy-final` = `origin/role/data-strategy-final` | `3bb53cc` | no | 7 / 1 | +0 / −1 | #33 MERGED (same head) | `agents/ROLES.md`, `agents/handoffs/data-strategy.md` | **yes** (2/2) | safe cleanup candidate after integrator review |
| worktree | `.worktrees/observatory` `role/observatory-b3` (remote **gone**) | `d2b51cf` | no | 12 / 1 | +0 / −1 | #28 MERGED (same head) | 4 paths under `research/observations/` + handoff | **yes** (4/4); files also on `origin/main` | safe cleanup candidate after integrator review |
| worktree | `.worktrees/program` `role/program` (remote **gone**) | `3108f92` | no | 4 / 3 | +3 / −0 | #37 MERGED (same head) | 19 experiment/STATUS/PROGRAM paths | **yes** (19/19); `PROGRAM.json` and `STATUS.md` exist on `origin/main` | safe cleanup candidate after integrator review |
| worktree | `.worktrees/solidify` `role/solidify-close` (remote **gone**) | `4dbc07b` | no | 5 / 1 | +0 / −1 | #35 MERGED (same head) | `agents/ROLES.md`, `agents/handoffs/solidify.md` | **yes** (2/2) | safe cleanup candidate after integrator review |
| local branch | `role/data-strategy` (no worktree; `origin/role/data-strategy` **gone**) | `7a830be` | n/a | 9 / 10 | +10 / −0 | #30 MERGED (same head) | 11 docs/tests/schema paths | **yes** (11/11) | safe cleanup candidate after integrator review |
| local + remote | `role/data-strategy-close` = `origin/role/data-strategy-close` | `e38d30c` | n/a | 8 / 2 | +2 / −0 | #32 MERGED (same head) | 4 paths | **yes** (4/4) | safe cleanup candidate after integrator review |
| local branch | `role/observatory` (remote **gone**) | `bfd1cd1` | n/a | 14 / 1 | +0 / −1 | #26 MERGED (same head) | 15 observation records + template/checklist | **yes** (15/15); **Observatory research on main** | safe cleanup candidate after integrator review |
| local branch | `role/observatory-b2` (remote **gone**) | `280b21c` | n/a | 13 / 1 | +0 / −1 | #27 MERGED (same head) | 11 observation records | **yes** (11/11); **Observatory research on main** | safe cleanup candidate after integrator review |
| local branch | `role/solidify` (tracks gone `origin/role/solidify-final`) | `54d403a` | n/a | 6 / 53 | +53 / −0 | #34 MERGED (same head) | 46 paths | **yes** (46/46) | safe cleanup candidate after integrator review |
| remote branch | `origin/role/solidify` | `805d45e` | n/a | 8 / 44 | +44 / −0 | #31 **CLOSED** (same head; not merged) | 44 paths; **15 blobs not on main** | **no** (15 files) | retain for review |

No `unresolved` rows. Every leftover local/remote non-`main` branch and every
worktree from `git worktree list` is in the table.

## Called out separately

### Historical handoff-only closeouts

These refs’ unique *committed* paths vs `origin/main` are only role bookkeeping
(`agents/ROLES.md` and/or `agents/handoffs/<role>.md`). Cherry is `−` (patch
already equivalent on main).

- `role/data-strategy-final` / worktree `.worktrees/data-strategy` (PR #33).
- `role/solidify-close` / worktree `.worktrees/solidify` (PR #35).

Safe cleanup **after** the integrator confirms those two handoff files on
`origin/main` match the intended closeout text.

### Observatory / unique research records

Observatory **outputs are already on `origin/main`** (27 paths under
`research/observations/`, including `CHECKLIST.md`, `TEMPLATE.md`, and the
trial records from batches 1–3). `git cherry` on `role/observatory`,
`role/observatory-b2`, and `role/observatory-b3` is `−` (equivalent patch on
main). Blob check: 15+11+4 path blobs all present on main.

Do **not** treat branch deletion as deleting Observatory research. Recommended
check (not run as a mutation): `git ls-tree -r --name-only origin/main --
research/observations` (already counted 27 files in the inventory capture).

`role/program` research (`research/experiments/PROGRAM.json`, `STATUS.md`,
six STUDY.md files, three drafts) also has **all blobs on `origin/main`**
(PR #37).

### Unique research/docs **not** on main (keep)

- `role/organization-prompts`: mission dispatch + operating-model docs.
- `role/wave4-prompts`: wave-4 follow-on prompt.
- Main checkout commit `b5c29a8` (`digests/2026-08-15.md`) plus dirty
  `digests/DISCOVERIES.md` and untracked `docs/prompts/Untitled`,
  `docs/repo_overview.html`.
- `origin/role/solidify` (`805d45e`, closed PR #31): 15 file blobs not found
  on `origin/main` (`agents/ROLES.md`, `agents/handoffs/solidify.md`,
  `docs/operations.md`, several `src/evallab/*.py` and tests). This is **not**
  the same SHA as local `role/solidify` (`54d403a`, merged PR #34). Do not
  delete `origin/role/solidify` from squash ancestry of #34 alone.

### Main checkout (recorded, not touched)

Path: `~/Developer/eval-lab`. Branch `main`. HEAD `b5c29a8` **ahead 1, behind
4** of `origin/main`. Unique commit: `Add 2026-08-15 lab digest`. Dirty:
modified `digests/DISCOVERIES.md`; untracked `docs/prompts/Untitled`,
`docs/repo_overview.html`. **This audit did not write, reset, rebase, or
stash that checkout.**

## Recommended verification and recovery (none executed)

1. Integrator spot-checks **at least two** table rows against
   `git cherry origin/main <ref>` and the blob/`find-object` method above
   (this mission’s verification re-runs two rows into scratch).
2. **Retain — organization-prompts / wave4-prompts:** open or decline PRs
   from those exact HEADs (`4627feb`, `15dbf28`). Do not rebase onto main
   until the unique prompt files are copied or merged.
3. **Retain — m001-governance dirty tree:** do not `git worktree remove`.
   Commit or stash on that worktree only after Peter/integrator review.
   `agents/missions/ACTIVE.md` is untracked there; do not delete it as
   “cleanup.”
4. **Retain — main checkout:** do not `git reset --hard`. If the 2026-08-15
   digest should land, cherry-pick `b5c29a8` onto a fresh branch from
   `origin/main` after inspecting the uncommitted DISCOVERIES edit.
5. **Retain — `origin/role/solidify`:** `git diff origin/main origin/role/solidify --`
   the 15 NOT_ON_MAIN paths; only then decide keep-as-archive or delete the
   remote branch.
6. **Safe cleanup candidates (after 1–2 spot-checks):**
   - `git worktree remove` for: `m002-operability`, `data-strategy`,
     `observatory`, `program`, `solidify` (the `solidify-close` worktree).
   - Then `git branch -d` / `git push origin --delete` only for refs whose
     unique blobs were confirmed on `origin/main`: `role/data-strategy`,
     `role/data-strategy-close`, `origin/role/data-strategy-close`,
     `origin/role/data-strategy-final`, `role/observatory{,-b2,-b3}`,
     `role/program`, `role/solidify` (local `54d403a` only),
     `role/solidify-close`.
   - Do **not** include `origin/role/solidify` (`805d45e`) in that delete list.
7. None of the steps in (2)–(6) were executed by this audit.

## Inventory commands used

Read-only: `git fetch origin`, `git worktree list`, `git branch -avv`,
`git status` / `rev-parse` / `rev-list` / `cherry` / `diff --name-only` /
`rev-parse REF:path` / `log --find-object`, `git ls-tree origin/main`,
`gh pr list`. No `git worktree remove`, no `gh pr merge`, no push except
`role/a001-state-audit` when opening this PR.
