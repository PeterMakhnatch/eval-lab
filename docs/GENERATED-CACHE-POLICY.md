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
| `research/lessons.md` | Python APIs `evallab.lessons.build_lessons` and `evallab.lessons.render_lessons_markdown` build and render the Markdown for an explicit write | `python -m evallab.lessons` checks statistical evidence and freshness |
| `research/registration/inventory.json` | Registration workflow | `evallab registry audit --json`; cleanup does not register or recertify tasks |

`research/experiments/STATUS.md` is an August 15 historical report, not a current status snapshot.


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

Retention notices are report-only; they do not authorize evidence deletion. A scratch-looking name is not sufficient evidence that a file is disposable.
Never run a broad filesystem purge, service change, queue reset, or database deletion as cleanup.
Retain the source and verify surviving evidence before retiring an owned worktree.

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
5. Recoverable commits. Keep branch refs unless their separate deletion criteria are met.

Then use native `git worktree remove`, without force, and measure the result.
Protect dirty, detached-but-unproven, nested-copy, and unknown trees. Do not run a
blanket prune or delete `.worktrees/`, `runs/`, `derived/`, or `backups/` wholesale.

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

