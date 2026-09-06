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
| `docs/INDEX.md` | `python -m evallab.docindex generate -o docs/INDEX.md` | `python -m evallab.docindex check`; indexes front-matter and hand-written doc digests (excludes generated `repo-map.md` and `STATUS.md` from inputs digest list) |
| `docs/repo-map.md` | `python -m evallab.repomap generate -o docs/repo-map.md` | `python -m evallab.repomap check`; recursive Python discovery with structural declaration digests (resilient to function body edits) |
| `docs/STATUS.md` | `evallab status --update` | Generated catalog snapshot (`status: historical`, excluded from context packs); not a clean-checkout byte-freshness invariant |
| `research/lessons.md` | Python API `evallab.lessons.generate_lessons_file` | `python -m evallab.lessons` checks statistical evidence and freshness |
| `research/registration/inventory.json` | Registration workflow | `evallab registry audit --json`; cleanup does not register or recertify tasks |

Run **`make docs`** for both deterministic documentation generators. Review the
result before committing; their freshness checks are read-only and required by
[CHECKS.md](../agents/CHECKS.md). `research/experiments/STATUS.md` is an August 15
historical report, not another output of `evallab status`.

The optional Git merge driver produces a **provisional** index from the working
tree available during merge. It reports failures and preserves the conflict;
it cannot certify that every merged source file is already installed. Run
`make docs` after merge/rebase, before final checks and exact-head review.
`scripts/setup-git.sh` configures only the driver; it never changes hook
configuration, which is shared by linked worktrees. Previously installed hooks
are now read-only notices. No repository hook stages, commits, or amends files.

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

See [tidy.md](tidy.md) for the existing dry-run/apply contract. Retention notices
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
