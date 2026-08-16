Status: review-wanted
Last: board/archive/prompt closeout and fleet truthfulness fix pass 349 tests, operational smoke, Ruff, and ty ratchet
Next: open M008 PR, require fresh exact-head CI, then integrator may squash-merge
Blockers: none

# M008 integration closeout

This is integrator-owned bookkeeping and a narrow safety fix. It does not
modify experiments, tasks, policy, queue behavior, or any M005/M006 lease.

Observed defect: `scripts/fleet-status.sh` classified a branch with zero
commits ahead as spent before checking its attached worktree. M005 therefore
appeared spent while it had six uncommitted implementation paths. The script
now lets dirty worktree state override ancestry and tests that case. Board
hygiene now requires branches only for `active`/`review` rows, not correctly
unallocated `ready`/`blocked` work.

Validation:

```
$ uv run pytest tests/test_fleet_status.py -q
11 passed
$ bash scripts/premerge.sh
349 passed; Docker-free operational smoke PASS; Ruff clean; ty 28 <= 28
```
