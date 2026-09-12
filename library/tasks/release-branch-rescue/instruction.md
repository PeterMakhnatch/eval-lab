# Ticket REL-2048: Recover lost release work and integrate divergent upstream

During an emergency deployment hotfix yesterday, an engineer attempted a release recovery on `/workspace/release`, but left the repository in a broken, detached HEAD state at tag `v1`. Two unpublished release commits were apparently erased by a reset.

Meanwhile, upstream development has continued independently in the authoritative offline remote repository at `/srv/origin.git` (configured as remote `origin`).

Your job is to recover the lost release commits, integrate the latest upstream changes, and restore the release repository to a clean, tracked state ready for deployment review.

## Requirements

1. **Recover unpublished work:** Locate the unpublished commits in the local repository history. You must tag the exact pre-reset tip commit as `rescue/pre-reset`.
2. **Preserve historical release tag:** The existing `v1` tag must remain intact and point to its original baseline commit.
3. **Integrate upstream:** Fetch the latest changes from `origin` and integrate them into local `main`. Both merge histories (`git merge`) and linear histories (`git rebase` or `git cherry-pick`) are acceptable, provided all local release changes and all upstream changes are incorporated.
4. **Active branch:** Local branch `main` must be checked out (`HEAD` must symbolically target `refs/heads/main`).
5. **Upstream tracking:** Local branch `main` must be configured to track `origin/main`.
6. **Clean worktree:** The working tree and index in `/workspace/release` must be completely clean (no unstaged changes, no uncommitted changes, no untracked files).

## Constraints

- Do not push to or alter `/srv/origin.git`. Upstream is authoritative and read-only.
- Do not use replacement refs (`refs/replace`), grafts, or object alternates. Repository integrity must be maintained using standard Git objects and references.

## Done Means

Your work is graded in a fresh, isolated verification container. The grader collects `/workspace/release` and `/srv/origin.git` at their original absolute paths and inspects them using trusted Git tooling:

- `HEAD` symbolically points to `refs/heads/main`.
- `main` tracks `origin/main`.
- `rescue/pre-reset` points to the original pre-reset commit.
- `v1` points to the original baseline release commit.
- Upstream `origin/main` is an ancestor of local `main`.
- The tracked files contain both local changes (retries=3 in `config/limits.conf`, `eu-west` in `config/regions.txt`) and upstream changes (timeout=60 in `config/timeouts.conf`, updated runbook in `docs/runbook.md`).
- Working tree and index are clean.
- `/srv/origin.git` remains unmodified.
