Status: review-wanted
Last: M024 TIDY-SQUASH — squash-aware merged detection with three-state classification and deletion safety
Next: Integrator review and merge PR into main
Blockers: none

# M024: TIDY-SQUASH Handoff

## Summary
Resolved production bug where `evallab tidy` reported `Stale worktrees (0 items)` despite fully-merged worktrees occupying significant disk space under squash-merge workflows.

Replaced the graph ancestry-only check (`git merge-base --is-ancestor`) with a rigorous, deletion-safe **three-state classification** (`merged`, `unmerged`, `unproven`) using git plumbing `git merge-tree --write-tree` content equivalence.

---

## Target & Lease
- **Target**: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/m024-tidy` on branch `role/m024-tidy` (Mission **M024 TIDY-SQUASH**).
- **Leased Files**:
  - `src/evallab/tidy.py`
  - `tests/test_tidy.py`
  - `agents/handoffs/m024-tidy.md`
- **Safety Guarantee**: `--apply` was **NEVER** run against the live repository. `--apply` was executed only inside isolated test fixtures. Sibling worktrees (`.worktrees/m020-queue`, `.worktrees/m021-cli`, `.worktrees/m022-memory`, `.worktrees/m023-craft`) were preserved and untouched.

---

## Audit of Inherited Draft (Commit `7d74222`)

### 1. Pytest Audit Output
```
$ uv run pytest tests/test_tidy.py
============================= test session starts ==============================
platform darwin -- Python 3.12.11, pytest-8.4.2, pluggy-1.6.0
rootdir: /Users/petermakhnatch/Developer/eval-lab/.worktrees/m024-tidy
configfile: pyproject.toml
plugins: hypothesis-6.165.10
collected 22 items

tests/test_tidy.py ......................                                [100%]

============================== 22 passed in 11.60s ==============================
```

### 2. Answers to Audit Questions

#### (a) What git primitive does the draft use to decide "merged by content"?
The implementation uses **tree comparison** via git plumbing:
`git merge-tree --write-tree <target_main> <branch>` compared against `git rev-parse <target_main>^{tree}` (with `git merge-base --is-ancestor` as a fast path).

- Grounded in git plumbing: computes the exact 3-way tree merge without touching the index or working tree.
- Safe under multi-commit squash merges: unlike `git cherry` or commit-level `patch-id` (which compare individual commits and fail when squashes combine or reorder changes), `merge-tree` evaluates the net tree change of the entire branch.
- Deletion safety: any unmerged commit or difference produces a tree differing from `target_main`'s tree, or returns exit code 1 (merge conflict), refusing deletion.

#### (b) Is the predicate stated somewhere a reader can check by hand?
Yes. The predicate is explicitly stated in `check_branch_merged_status` docstring (`src/evallab/tidy.py:256-265`) and can be verified manually via the command line:

1. **Fast path (ancestry)**:
   ```bash
   git merge-base --is-ancestor <branch> <target_main>
   # Exit code 0 => branch tip is reachable from target_main
   ```
2. **Content path (3-way merge equivalence)**:
   ```bash
   # Compute 3-way merge tree hash
   MERGE_TREE=$(git merge-tree --write-tree <target_main> <branch>)
   # Compute target_main tree hash
   TARGET_TREE=$(git rev-parse <target_main>^{tree})
   # If MERGE_TREE == TARGET_TREE, all changes from <branch> already exist in <target_main>
   ```

#### (c) Does the draft implement the three-state classification (`merged` / `unmerged` / `unproven`) or did it just loosen the merged check into two states?
The draft strictly implements the **three-state classification**:
1. **`merged`**: Provably in `target_main` (via ancestry exit code 0 or 3-way `merge-tree` hash matching `target_main^{tree}`).
2. **`unmerged`**: Provably carrying content `target_main` lacks (`merged_tree != target_tree` or exit code 1 / merge conflict).
3. **`unproven`**: Cannot be established with certainty (detached HEAD, missing ref in `refs/heads/`, corrupted worktree, or any git command failure / exit code > 1).

Only `merged` AND clean worktrees are `actionable`. `unproven`, `unmerged`, and `dirty` worktrees are **NEVER actionable**.

---

## Required Semantics & Deletion Safety

1. **Ancestry Fast Path**: `git merge-base --is-ancestor <branch> <target_main>` returns 0 immediately for non-squashed merged branches.
2. **Content Equivalence for Squash Merges**: Computes `git merge-tree --write-tree <target_main> <branch>` and verifies equivalence to `git rev-parse <target_main>^{tree}`.
3. **Strict Actionable Condition**: `actionable` is `True` **if and only if** status is `clean_merged` (`merged` state AND clean status).
4. **Unproven Safety**: Detached HEAD, missing branch ref, broken `.git`, or git execution errors classify as `unproven`. In the report, `unproven` worktrees render on their own distinct line under `## Active worktrees (not swept)` with explicit reason logging so a human operator sees them instead of hiding in a count.
5. **Dirty Preservation**: Dirty worktrees (whether merged or unmerged) are immediately identified and excluded (`dirty — skipped`).

---

## Deletion Safety Test Suite

The test suite in `tests/test_tidy.py` verifies all critical safety properties:
- `test_squash_merged_worktree_is_detected_as_stale_and_actionable`: Squash-merged clean worktree is detected as `clean_merged` and `actionable`.
- `test_multi_commit_squash_merged_worktree_is_actionable`: Multi-commit squash-merged clean worktree is detected as `clean_merged` and `actionable`.
- `test_branch_with_unmerged_commit_is_never_actionable`: Branch with even one unmerged commit is `active_clean` and `actionable=False`.
- `test_detached_head_worktree_is_unproven_and_not_actionable`: Detached HEAD worktree classifies as `unproven` and `actionable=False`.
- `test_missing_branch_worktree_is_unproven_and_not_actionable`: Missing branch ref classifies as `unproven` and `actionable=False`.
- `test_dirty_merged_worktree_is_not_actionable`: Dirty worktree on merged branch classifies as `dirty` and `actionable=False`.
- `test_git_failure_classifies_unproven_rather_than_merged`: Git command failure defaults to `unproven` and `actionable=False`.
- `test_broken_worktree_classifies_unproven`: Broken `.git` worktree classifies as `unproven` and `actionable=False`.
- `test_property_actionable_implies_provably_merged_and_clean`: Hypothesis property test verifying for all branch types and dirty states that `actionable == True` strictly implies `provably merged and clean`.

---

## Reproduction Evidence

### Old Behaviour vs New Behaviour
```
=== OLD ANCESTRY CHECK ===
git merge-base --is-ancestor role/squash-feat main -> exit code 1 (0=ancestor, 1=not ancestor)
# Old sweep_worktrees reported: Stale worktrees (0 items, 0 B)

=== NEW BEHAVIOR ===
# evallab tidy report

## 1. Stale worktrees (1 items, 129 B)
- `.worktrees/squash-wt` (role/squash-feat, 129 B) — branch merged into main (content) [eligible for removal]

## 2. Merged local branches (0 items)
  (clean — no merged local role/* branches found)

## 3. Unindexed docs (0 items, 0 B)
  (clean — all documentation indexed and valid)

## 4. Untracked strays (0 items, 0 B)
  (clean — no untracked stray files found)

## 5. Retention violations (0 items, 0 B)
  (report only — run evallab gc to manage evidence retention with tombstones)
  (clean — no retention violations found)

## Summary
Total findings: 1 items (129 B)
Actionable items: 1 items

Dry-run mode: no files or branches were modified. Pass --apply to execute safe cleanup.
```

---

## Mutation Testing Evidence

### Mutation 1: Relax predicate so unmerged commit reads as merged
Mutated `src/evallab/tidy.py`: replaced `if merged_tree and merged_tree == target_tree:` with `if merged_tree:`.

**Result**: 2 tests failed (both safety test and hypothesis property test caught the violation):
```
FF                                                                       [100%]
=================================== FAILURES ===================================
_____________ test_branch_with_unmerged_commit_is_never_actionable _____________
>       assert wt_names["partial-wt"].actionable is False
E       AssertionError: assert True is False

__________ test_property_actionable_implies_provably_merged_and_clean __________
>           assert branch_type in ("ancestor_merged", "squash_merged")
E           AssertionError: assert 'unmerged_extra_commit' in ('ancestor_merged', 'squash_merged')
=========================== short test summary info ============================
FAILED tests/test_tidy.py::test_branch_with_unmerged_commit_is_never_actionable
FAILED tests/test_tidy.py::test_property_actionable_implies_provably_merged_and_clean
2 failed, 20 deselected in 3.81s
```

### Mutation 2: Git command failure defaults to merged
Mutated `src/evallab/tidy.py`: replaced `return ("unproven", f"branch '{branch}' does not exist in local refs")` with `return ("merged", "MUTATION: git failure defaults to merged")`.

**Result**: 3 tests failed (unit test, safety test, and property test caught the violation):
```
FFF                                                                      [100%]
=================================== FAILURES ===================================
_________ test_missing_branch_worktree_is_unproven_and_not_actionable __________
>       assert wt_names["missing-wt"].actionable is False
E       AssertionError: assert True is False

___________ test_git_failure_classifies_unproven_rather_than_merged ____________
>       assert state == "unproven"
E       AssertionError: assert 'merged' == 'unproven'

__________ test_property_actionable_implies_provably_merged_and_clean __________
>           assert branch_type in ("ancestor_merged", "squash_merged")
E           AssertionError: assert 'missing_branch' in ('ancestor_merged', 'squash_merged')
=========================== short test summary info ============================
FAILED tests/test_tidy.py::test_missing_branch_worktree_is_unproven_and_not_actionable
FAILED tests/test_tidy.py::test_git_failure_classifies_unproven_rather_than_merged
FAILED tests/test_tidy.py::test_property_actionable_implies_provably_merged_and_clean
3 failed, 19 deselected in 3.72s
```

### Restored Verification
```
$ uv run pytest tests/test_tidy.py
......................                                                   [100%]
22 passed in 11.60s
```

---

## Real Repository Read-Only Report Output

Executed in dry-run mode against the actual repository checkout:
```
$ uv run python -m evallab.cli tidy --dry-run
# evallab tidy report

## 1. Stale worktrees (0 items, 0 B)
  (clean — no stale worktrees found)

## Active worktrees (not swept) (4 items, 1837.1 MB)
- `.worktrees/m020-queue` (role/m020-queue, 459.2 MB) — active branch role/m020-queue (not merged into origin/main)
- `.worktrees/m021-cli` (role/m021-cli, 459.3 MB) — dirty — skipped (1 uncommitted file)
- `.worktrees/m022-memory` (role/m022-memory, 459.2 MB) — dirty — skipped (4 uncommitted files)
- `.worktrees/m023-craft` (role/m023-craft, 459.4 MB) — active branch role/m023-craft (not merged into origin/main)

## 2. Merged local branches (0 items)
  (clean — no merged local role/* branches found)

## 3. Unindexed docs (0 items, 0 B)
  (clean — all documentation indexed and valid)

## 4. Untracked strays (0 items, 0 B)
  (clean — no untracked stray files found)

## 5. Retention violations (0 items, 0 B)
  (report only — run evallab gc to manage evidence retention with tombstones)
  (clean — no retention violations found)

## Summary
Total findings: 4 items (1837.1 MB)
Actionable items: 0 items

Dry-run mode: no files or branches were modified. Pass --apply to execute safe cleanup.
```

**Apply Safety Confirmation**: `--apply` was **never** run against the actual repository. All 4 sibling active worktrees remain safe and unmodified.

## Integrator verification (independent, on live data)

The strongest available test appeared by accident while this mission ran: five sibling
worktrees existed, and exactly one of them (`m023-craft`) was squash-merged into `main`
mid-flight. That is the production scenario the bug hid.

Old code, `main`, real repository:

```
## 1. Stale worktrees (0 items, 0 B)
  (clean — no stale worktrees found)
## Active worktrees (not swept) (5 items, 2297.1 MB)
- `.worktrees/m023-craft` ... — active branch role/m023-craft (not merged into origin/main)
```

New code, same repository, same moment:

```
## 1. Stale worktrees (1 items, 459.4 MB)
- `.worktrees/m023-craft` (role/m023-craft, 459.4 MB) — branch merged into origin/main (content) [eligible for removal]
## Active worktrees (not swept) (3 items, 1378.3 MB)
- `.worktrees/m020-queue` ... (not merged into origin/main)
- `.worktrees/m021-cli` ... (not merged into origin/main)
- `.worktrees/m022-memory` ... (not merged into origin/main)
```

459.4 MB correctly identified as reclaimable, and the three genuinely in-flight
worktrees correctly left alone. `--apply` was never run against the real repository.

### Mutation evidence (integrator-run)

Missing branch ref classified `merged` instead of `unproven`:

```
FAILED tests/test_tidy.py::test_missing_branch_worktree_is_unproven_and_not_actionable
FAILED tests/test_tidy.py::test_git_failure_classifies_unproven_rather_than_merged
    - AssertionError: assert 'merged' == 'unproven'
FAILED tests/test_tidy.py::test_property_actionable_implies_provably_merged_and_clean
    - AssertionError: assert 'missing_branch' in ('ancestor_merged', 'squash_merged')
```

Accepting any `merge-tree` result as merged (`if True` in place of the tree equality):

```
FAILED tests/test_tidy.py::test_branch_with_unmerged_commit_is_never_actionable
FAILED tests/test_tidy.py::test_property_actionable_implies_provably_merged_and_clean
    - AssertionError: assert 'unmerged_extra_commit' in ('ancestor_merged', 'squash_merged')
```

Restored → green. The deletion-safety guarantees are therefore load-bearing, not
decorative.

### Pre-existing defect found while verifying (NOT this mission's)

`tests/test_tidy.py::test_tidy_fixture_findings` fails when run alone and passes in the
full suite — on `main` as well as on this branch:

```
$ uv run pytest tests/test_tidy.py::test_tidy_fixture_findings -q   # on main
FAILED - assert 'z3_hot_partition' in {'events_log': RetentionFinding(...)}
```

It depends on state another test leaves behind. Recorded in
`research/audits/board-notes.md`; out of scope here because fixing it would mean
editing an unrelated retention fixture in the same file this mission is rewriting.

### Gate

```
$ bash scripts/premerge.sh
premerge green: Python 3.12; ty 28 <= 28
Found 28 diagnostics
```
