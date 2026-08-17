Status: review-wanted
Last: implemented E16 tidy sweep reporting strays, stale worktrees, and retention violations; full test suite passing (1099 passed); documentation, docindex, and repomap updated.
Next: integrator review; do not merge.
Blockers: none.

## E16: Working tree tidy sweep reporting strays, stale worktrees, and retention violations

Worktree: `.worktrees/e16-tidy` (branch `role/e16-tidy`)

### Key Deliverables

1. **`src/evallab/tidy.py`**:
   - Implements `evallab tidy` enforcing platform tenet **T7** (*janitorial duty inside every contract*), §2.6 retention matrix, and §8 coordination rules.
   - Five deterministic sweeps:
     1. **Stale worktrees**: `.worktrees/*` whose branch is merged into `origin/main` or no longer exists. Worktrees with uncommitted changes are strictly preserved and reported as `dirty — skipped` with uncommitted file counts.
     2. **Merged local branches**: `role/*` local branches fully contained in `origin/main`. Checked against `gh pr list` for open PRs; skipped and preserved if `gh` is unavailable or open PRs exist; skipped if checked out in an active worktree.
     3. **Unindexed docs**: Reuses `evallab.docindex` to detect markdown files in `docs/` missing from `docs/INDEX.md` or carrying missing/invalid front-matter. Report only.
     4. **Untracked strays**: Detects untracked files not gitignored, distinguishing recognized junk (safe to delete on `--apply`) from unrecognized drafts (preserved as potential work-in-progress).
     5. **Retention violations**: Flags Z3 hot partitions older than 7d, unpromoted Z1 jobs older than 14d, and `queue/events.jsonl` beyond the 30d rolling window. Report only (directs operators to `evallab gc` with tombstones).
   - Hard invariant: `research/evidence/`, `policy/`, and Z5 briefs/handoffs (`docs/prompts/`, `agents/handoffs/`, `board/`, `agents/briefs/`) are never touched.
   - `--dry-run` is default and exits non-zero (1) when findings exist so CI can gate on cleanliness.

2. **`src/evallab/cli.py` & `tests/test_cli_audit.py`**:
   - Registered `evallab tidy [--apply] [--dry-run]` parser and wired execution.
   - Added `tidy` to `TOP_LEVEL_COMMANDS` in registration order in `tests/test_cli_audit.py`.

3. **`tests/test_tidy.py`**:
   - Synthetic fixture repository with clean merged worktrees, dirty worktrees, unindexed docs, untracked junk/drafts, and over-age retention violations.
   - Load-bearing immutability test: asserts tree digest before and after `--dry-run` is identical.
   - Asserts dirty worktrees and unrecognized drafts are preserved even under `--apply`.
   - Asserts never-touch invariants across `research/evidence/`, `policy/`, and handoffs.
   - Asserts non-zero exit in dry-run with findings and zero on a clean repo.
   - Asserts byte-identical output across repeated runs without fluctuating timestamps.

4. **Documentation**:
   - `docs/tidy.md`: living document with required front-matter describing all sweeps, apply semantics, never-touch list, and division of labour with `evallab gc`.
   - `docs/INDEX.md` and `docs/repo-map.md`: regenerated and validated.

### Verification

```bash
uv run pytest tests/test_tidy.py
# 9 passed in 3.23s

uv run pytest tests/test_cli_audit.py
# 52 passed in 0.36s

uv run pytest
# 1099 passed, 2 skipped, 1 xfailed in 43.40s

uv run ruff check .
# All checks passed!

uvx ty@0.0.71 check src/ --output-format=concise 2>&1 | tail -2
# Found 28 diagnostics (at ratchet <= 28)

uv run python -m evallab.repomap check
# repomap check passed

uv run python -m evallab.docindex check
# docindex check passed

uv run evallab tidy --dry-run
# Correctly reports workspace findings, preserves drafts/worktrees, and exits 1 in dry-run
```
