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

`--dry-run` is the default. The command reports findings with byte counts and reasons,
exiting non-zero if any findings exist so that CI or automated health checks can gate
on workspace cleanliness. Deletions occur only when `--apply` is explicitly passed.

## Five Sweeps

### 1. Stale Worktrees
Inspects `.worktrees/*` in the repository for linked worktrees whose branch is fully
merged into `origin/main` (or `main`) or whose branch no longer exists in Git refs.
- **Safety invariant:** Never removes or touches a worktree with uncommitted changes.
  Dirty worktrees are always reported as `dirty — skipped` with the count of uncommitted
  files, protecting in-progress missions.
- **Action under `--apply`:** Removes clean worktrees whose branch has merged or vanished,
  pruning the worktree registration in Git.

### 2. Merged Local Branches
Inspects local branches matching `role/*` to see if their commits are fully contained
in `origin/main`.
- **Safety invariant:** Never deletes a branch with an open GitHub pull request. If the
  `gh` CLI is not installed, not authenticated, or returns an error, the sweep skips the
  branch and preserves it rather than guessing. Never deletes a branch currently checked out
  in an active worktree.
- **Action under `--apply`:** Deletes clean local `role/*` branches that are merged and
  confirmed to have no open PR.

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
