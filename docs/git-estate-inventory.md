---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Git Estate Inventory & Reconciliation

Repository current main 5c707900; primary reconciliation is complete. Current module locations are stable for now; new work uses the current authoritative paths.

This durable inventory provides an exhaustive, verified accounting of all pull requests, local branches, remote tracking branches, and registered git worktrees in the `PeterMakhnatch/eval-lab` repository.

**Important**: No cleanup or deletion occurs in this PR. All findings are observational and cataloged to enable safe, phased hygiene batches under conservative gates.

---

## 1. Inventory Summary (Total Audited: 591 Entities)

| Entity Type | Total Count | Active / Protected | Merged-Clean | Superseded | Unmerged-Useful | Unknown | Stage 1 Prune | Stage 2 Worktree | Handoff / Retain | Not Applicable |
|---|---|---|---|---|---|---|---|---|---|---|
| **Pull Requests** | 236 | 1 | 224 | 10 | 1 | 0 | 0 | 0 | 0 | 236 |
| **Local Branches** | 144 | 5 | 40 | 35 | 8 | 56 | 41 | 34 | 64 | 0 |
| **Remote Branches** | 153 | 2 | 43 | 1 | 8 | 99 | 44 | 0 | 107 | 0 |
| **Worktrees** | 58 | 5 | 1 | 40 | 12 | 0 | 0 | 41 | 12 | 0 |
| **Total** | **591** | **13** | **308** | **86** | **29** | **155** | **85** | **75** | **183** | **236** |

*Note on Remote Branches accounting*: The 153 remote branch rows represent canonical default branch `origin/main` (protected) plus exactly 152 remote tracking branch references under `refs/remotes/origin/*`.

---

## 2. Methodology & Audit Commands

The estate audit was executed across GitHub API and local Git metadata:
1. **Pull Requests (236)**:
   - `gh pr list --state all --limit 300 --json number,title,state,headRefName,headRefOid,mergedAt,baseRefName`
   - Scanned all 236 PRs (#1 through #236). PRs are immutable GitHub records, not local refs; all 236 PR rows are assigned cleanup stage `NOT_APPLICABLE`.
2. **Local Branches (144)** & **Remote Branches (153)**:
   - Local: `git for-each-ref --format='%(refname:short)|%(objectname)|%(upstream:short)|%(subject)' refs/heads`
   - Remote: `git for-each-ref --format='%(refname:short)|%(objectname)|%(subject)' refs/remotes/origin`
   - Ancestry & Equivalence: `git merge-base --is-ancestor <sha> origin/main` and `git cherry origin/main <sha>`
   - Reconciled against independent live verifier: 41 unattached Stage 1 local branches, 44 Stage 1 remote tracking branches.
3. **Worktrees (58)**:
   - `git worktree list --porcelain`
   - Evaluated clean state (`git status --porcelain` per path): 41 verified safe Stage 2 worktrees (0 modified, 0 staged, 0 untracked), 5 protected active worktrees (root, 3 lanes, hygiene worktree), and 12 dirty/quarantined worktrees.

---

## 3. Conservative Cleanup Invariants

Any future hygiene execution MUST adhere strictly to the following invariants:
1. **PR Immutable Records**: PRs are permanent GitHub records and cannot be pruned (`NOT_APPLICABLE`).
2. **Zero-Dirty Invariant**: No worktree with `uncommitted_changes > 0`, `staged_changes > 0`, or `untracked_files > 0` may ever be removed.
3. **Zero-Unpushed Invariant**: No branch with `upstream_ahead_count > 0` or unpushed experimental work may be deleted.
4. **2-Stage Worktree-First Deletion**: A local branch attached to a worktree cannot be pruned directly via `git branch -d`. Clean worktrees must be removed first via `git worktree remove`, followed by branch deletion.
5. **Protected Lanes**: The primary repository root (`/Users/petermakhnatch/Developer/eval-lab`), three long-lived lanes (`.worktrees/lane-architect`, `.worktrees/lane-execution`, `.worktrees/lane-storage`), and the active hygiene worktree (`.worktrees/hygiene-git-estate-inventory`) are permanently protected.
6. **Preservation of Uncertainty**: Any historical branch without definitive supersession or merge proof (quarantined unknown branches) is retained for domain operator review.

---

## 4. Dataset Artifacts & Navigation

- **Machine-Readable Snapshot**: [`research/analysis/git-estate-inventory-2026-08-27.json`](../research/analysis/git-estate-inventory-2026-08-27.json) (Contains all 591 entity rows with full evidence digests).
- **Handoffs & Quarantined Ledger**: [`research/analysis/git-estate-handoffs-2026-08-27.md`](../research/analysis/git-estate-handoffs-2026-08-27.md) (Comprehensive accounting of all dirty worktrees, unmerged-useful assets, unknown historical branches, and primary evidence reconciliation).
- **Preserved Primary Context**: [`research/analysis/preserved-primary-evidence-AGENTS-2026-08-27.md`](../research/analysis/preserved-primary-evidence-AGENTS-2026-08-27.md) (Byte-identical preservation of divergent primary evidence note, SHA-256: `f9a82e52cbebb23c9b815caf70f37e395d0e10f1d1ad75b817a76333459607b5`).
