Status: review-wanted
Last: premerge green; dry-run on real runs/ identical twice (8 skips, 0 actions)
Next: open PR RETENTION: evallab gc and merge after gh pr checks are fully green
Blockers: none

## Dated chore (2026-08-14)

Today is 2026-08-14, before 2026-08-21. The `harbor-lab` legacy alias in
`pyproject.toml` stays. Sweep remaining references after 2026-08-21.

## Verification

- `uv run evallab gc --runs-dir ~/Developer/eval-lab/runs` twice: identical
  plans, 0 actions, 8 skips (digest-referenced or not ingested+projected).
  No `.tombstones` / `.gc` created.
- `uv run pytest tests/test_gc.py` — 5 passed (compress 14d, prune 60d,
  four exclusions, catalog path after both, queue events).
- `uv run evallab doctor` prints `disk  runs=0B  compress-candidates=0 …`
  (this worktree has no local runs/).
- `nightly_gc_plan` is plan-only; digest section **GC would reclaim** written
  without `--apply`.
- `scripts/premerge.sh` green (59 tests, ruff, ty 33 <= 33).

