---
status: living
audience:
  - operator
  - builder
---

# Working Tree Discipline and Tidy Sweeps (`evallab tidy`)

The platform janitor enforcing tenet **T7** (*janitorial duty inside every contract*),
specified in `docs/platform-architecture.md` (T7, §2.6, §8).

`evallab tidy` sweeps the repository and linked worktrees for working tree debt,
untracked strays, stale worktrees, unindexed documentation, and retention violations.

`--dry-run` is the default. The command reports findings with byte counts and reasons.
Under `--dry-run`, the exit code is non-zero (1) exactly when at least one item is
actionable (eligible for deletion under `--apply`), and zero (0) when the working tree
is clean or every finding is preserved/report-only. Deletions occur only when `--apply`
is explicitly passed.

## Five Sweeps

### 1. Stale Worktrees
The inventory authority is Git: candidates come from parsing `git worktree list
--porcelain`, never from directory listings. Registered linked worktrees may live
anywhere on disk (including outside `.worktrees/`, e.g. `/private/tmp`-style scratch
locations) and are all inventoried; worktree paths are never inferred from directory
names. Unregistered directories under `.worktrees/` are ignored. On Git failure or
malformed output the sweep fails closed: no candidates, no actions.
- **Exclusion of current invocation:** The worktree running `evallab tidy` is excluded
  from candidates before any status checks or size walks, and never reported as stale.
  The primary checkout is likewise never a stale candidate.
- **Active worktrees separation:** Worktrees with uncommitted changes or active unmerged
  branches are reported under a separate `Active worktrees (not swept)` section with the
  count of uncommitted files or branch status, protecting in-progress missions. The stale
  count includes only genuinely sweepable worktrees.
- **Sizes are intentionally not walked for active worktrees:** Recursive byte counting
  runs only for actionable entries whose path still exists, so `--dry-run` stays a cheap
  reclaim estimate. Active entries are reported as `sizes not walked` instead of falsely
  claiming byte totals for live environments that were not scanned.
- **Prunable registrations:** When Git itself reports a registration `prunable` (stale
  metadata, e.g. the worktree directory was deleted elsewhere), tidy classifies it as
  `prunable` and actionable — a registration-only cleanup. Locked registrations are
  never actionable.
- **Action under `--apply`:** Removes clean worktrees whose branch has merged or vanished
  through `git worktree remove`; removes Git-reported prunable registrations exclusively
  through `git worktree prune`. Prune never deletes branch refs and never touches any
  directory's contents — a prunable marker can never escalate into deleting a live
  directory.

### 2. Merged Local Branches
Inspects local branches matching `role/*` to see if their commits are fully contained
in `origin/main`.
- **Active worktree protection:** Branches currently checked out in an active worktree
  or backing uncommitted work are preserved and classified separately, never labelled as
  "merged" on ancestry containment alone nor presented under a heading that invites deletion.
- **Safety invariant:** Never deletes a branch with an open GitHub pull request. If the
  `gh` CLI is not installed, not authenticated, or returns an error, the sweep skips the
  branch and preserves it rather than guessing.
- **Action under `--apply`:** Deletes clean local `role/*` branches that are merged and
  confirmed to have no open PR and no active worktree.

### 3. Unindexed Documentation
Inspects documentation files under `docs/` using `evallab.docindex` to identify markdown
files absent from the generated `docs/INDEX.md` or carrying missing/invalid YAML front-matter.
- **Front-matter contract:** Requires `status: living | historical` and `audience` covering
  valid roles (`builder`, `analyst`, `runner`, `operator`).
- **Action under `--apply`:** **Report only.** Documentation files represent knowledge
  records and are never deleted by tidy sweeps. Operators fix front-matter or regenerate
  the index via `python -m evallab.docindex generate`.

### 4. Untracked Strays
Inspects untracked files not ignored by `.gitignore`. Classifies every untracked file into
either **recognized junk** or **unrecognized stray (possible draft)**.
- **Recognized junk signatures:**
  - Extensions: `.tmp`, `.temp`, `.bak`, `.backup`, `.swp`, `.swo`, `.orig`, `.rej`, `.old`, `.log`, `.pyc`, `.pyo`, `.pyd`, or files ending in `~`.
  - Filenames: `.DS_Store`, `Thumbs.db`, `dump.rdb`, `core`.
  - Cache/Build directories: `__pycache__`, `.pytest_cache`, `.coverage`, `.mypy_cache`, `.ruff_cache`.
  - Prefixes: `tmp_`, `temp_`, `scratch_`, `test_output_`.
  - Stems: `scratch.*`, `temp.*`, `tmp.*`.
- **Safety invariant:** Never deletes untracked files without a recognized junk signature.
  Unrecognized files (e.g. `.py`, `.md`, `.sql`, `.json` source files) are reported and
  preserved as possible work-in-progress drafts.
- **Action under `--apply`:** Deletes only files matching recognized junk signatures.

### 5. Retention Violations
Inspects storage zones for records that have exceeded their retention limits under
`docs/platform-architecture.md` §2.6:
- **Z3 hot partitions:** Parquet files in `derived/parquet/` older than 7 days (compaction required).
- **Z1 unpromoted jobs:** Completed job directories in `runs/` older than 14 days without promotion.
- **Events log:** `queue/events.jsonl` entries older than the 30-day rolling retention window.
- **Action under `--apply`:** **Report only.** Deleting evidence is the exclusive responsibility
  of `evallab gc` with immutable tombstones. Tidy reports retention violations and directs
  the operator to run `evallab gc`.

## Never-Touch List (Hard Invariant)

The following paths are never touched, never swept as strays, and never eligible for deletion:

1. `research/evidence/` — promoted evidence (retention ∞, delete never).
2. `policy/` — standing approvals policy and gate rules.
3. `docs/prompts/`, `agents/handoffs/`, `board/`, `agents/briefs/` — Z5 coordination briefs and handoff artifacts (archive-only, never delete).

## Division of Labour: `tidy` vs `gc`

| Responsibility | Tool | Mutates | Artifacts / Safety |
|---|---|---|---|
| Working tree hygiene | `evallab tidy` | Clean stale worktrees, merged branches, recognized junk strays | Fast sweep; preserves dirty worktrees, open PRs, drafts |
| Documentation index | `evallab.docindex` | `docs/INDEX.md` | Deterministic index generation and validation |
| Evidence retention & pruning | `evallab gc` | `runs/<job>` (compress 14d, prune 60d) | Leaves immutable tombstones in `.tombstones/`, emits queue events |
