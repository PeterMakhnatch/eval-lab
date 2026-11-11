---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Generated products and safe cache policy

`.gitignore` controls Git discovery, not retention or authority. Ignored files may
be unique evidence. The counts and deletion suggestions in
[content-inventory.md](../research/archive/2026-W36/content-inventory.md) and
[git-estate-inventory.md](archive/git-estate-inventory.md) are historical snapshots, not
current cleanup instructions. Module-location rules live in `src/evallab/AGENTS.md`.

## Explicit generation before review

| Product | Writer | Read-only verification / scope |
|---|---|---|
| `docs/STATUS.md` | `evallab status --update` | Generated catalog snapshot (`status: historical`); not a current operator contract or clean-checkout byte-freshness invariant |
| `research/lessons.md` | Python API `evallab.lessons.generate_lessons_file` | `python -m evallab.lessons` checks statistical evidence and freshness |
| `research/registration/inventory.json` | Registration workflow | `evallab registry audit --json`; cleanup does not register or recertify tasks |

`research/experiments/STATUS.md` is an August 15 historical report, not another
output of `evallab status`.


## Authority and retention

| Path / content | Treatment |
|---|---|
| `research/evidence/`, registered task versions and verifier fixtures | Preserve immutable evidence and version contracts. Not cleanup candidates. |
| `runs/` | Harbor execution evidence and operational scratch are mixed. Preserve job evidence; use the documented evidence lifecycle, not bulk deletion. |
| `derived/evidence-cas/` | Durable content-addressed evidence. Never delete as a cache. |
| Other `derived/` content | Determine each producer and source authority before classifying it. A projection is rebuildable only when its source bytes and exact regeneration procedure are available. Model judgments are not assumed cheaply or identically reproducible. |
| `queue/` | Live leases, specifications, and append-only event history. Do not reset or remove while workers run. |
| `backups/` | Recovery material; retain according to the corresponding backup policy. Not disposable because it is ignored. |
| `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.hypothesis/` | Regenerable tool caches; remove only when their owning process is inactive. |
| `.venv/` | Rebuildable dependency environment, not transferable worktree state. Preserve any nonstandard local source before replacing it. |
| Untracked Markdown, Python, JSON, SQL | Drafts until classified. Preserve unique bytes and provenance before any move or deletion. |

See the tidy contract below for the existing dry-run/apply behavior. Retention notices
are report-only; they do not authorize evidence deletion. A scratch-looking name
is not sufficient evidence that a file is disposable.

## Worktree retirement

Git's `worktree list --porcelain` is the registration authority, including trees
outside `.worktrees/`. An age threshold or a clean Git status alone is insufficient.
Before removal, record all of:

1. Closed/inactive ownership, no open PR or process using the tree, no merge/rebase
   operation, and no Git lock.
2. Clean tracked and untracked state, with unchanged HEAD since inspection.
3. Ancestry or exact merged-PR head evidence against the intended integration ref.
   Branch-name reuse and squash merges must not be mistaken for closure.
4. No unique ignored evidence. Rebuildable caches may go; duplicated ignored
   evidence must have a verified surviving copy.
5. Recoverable commits and a per-path disposition manifest. Keep branch refs unless
   their separate deletion criteria are met.

Then use native `git worktree remove`, without force, and measure the result.
Protect dirty, detached-but-unproven, nested-copy, and unknown trees. Do not run a
blanket prune or delete `.worktrees/`, `runs/`, `derived/`, or `backups/` wholesale.

The disposition manifest in item 5 is produced by `uv run evallab worktrees
--markdown-out <path> --json-out <path>` (`src/evallab/worktree_disposition.py`). It
lists every registration from `git worktree list --porcelain` with branch,
ahead/behind against `origin/main`, PR state from one batched `gh pr list`, and a
mechanical verdict: `delivered` (every commit is on the base by ancestry, patch-id,
or the head of a merged PR into the base branch), `unique-unmerged` (a branch-attached
HEAD carries commits the base lacks), or `blocked` (missing or prunable tree, Git
cannot report state, a detached HEAD with unaccounted commits, or a delivered HEAD
with uncommitted work or a merge/rebase in progress). Locked registrations, open PRs,
and ignored `runs/`/`derived/`/`queue/` entries (presence only) are per-row retirement
blockers, never verdict changes. The command only reads; a dated report is evidence of
state at generation time, not approval to retire any tree. The first report is
`docs/checkpoints/2026-10-29-worktree-disposition.md`.

## Active and historical assets

Active source, tests, experiment specs, registries, and query projections can evolve through reviewed changes.
Historical status does not make evidence disposable.
Promoted runs, CAS blobs, labels, closed study reports, and release manifests retain their source authority.
Historical analyses remain append-only evidence about their original corpus and revision.
Removing an unused product's source does not remove its stored evidence or approvals ledger.

The 4 provenance categories in [data architecture](data-architecture.md) describe origin.
Storage zones describe physical authority. Neither taxonomy grants cleanup permission.
A rebuildable Parquet partition is not equivalent to a unique model judgment beside it.
Keep registered task versions and verifier fixtures immutable.
Create new versions and lineage for changes.
Generated status remains a snapshot, never a current service-health claim.

## Tidy contract

`evallab tidy` reports working-tree debt. Dry-run is the default.
Exit 1 means at least 1 finding is actionable under its apply contract.
Exit 0 also covers inventories containing only preserved or report-only findings.
An inventory failure never authorizes deletion.
`--apply` is an explicit cleanup action, not a routine verification step.

- Git registration owns the worktree inventory, including worktrees outside `.worktrees/`.
- The invocation worktree and primary checkout are not stale-worktree candidates.
- Dirty, active, locked, missing-branch, and unproven worktrees remain protected.
- Native worktree removal must succeed before a removal is reported.
- Prunable registration metadata is not permission to delete a surviving directory.
- Branch cleanup requires merged commits, no open PR, and no active worktree.
- Failed GitHub inspection preserves the branch.
- Unrecognized untracked files remain possible drafts.
- A scratch-looking filename never overrides known evidence or ownership.
- Retention findings remain report-only. Tidy does not remove execution evidence.

`evallab gc` has a separate evidence lifecycle and writes immutable tombstones.
Its default is planning only. It does not become authorized because tidy reports old files.
Never run a broad filesystem purge, service change, queue reset, or database deletion as cleanup.
Retain the source and verify surviving evidence before retiring the owned worktree.
