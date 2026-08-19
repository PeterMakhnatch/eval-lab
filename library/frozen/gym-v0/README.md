---
status: living
audience:
  - operator
  - runner
  - analyst
---

# gym-v0 — frozen gym generation

`manifest.json` is the immutable record of which tasks the gym contained when this
generation was frozen. Every campaign result cites its generation, which is what
makes next month's numbers comparable to tomorrow's.

## The contract

1. **A frozen manifest is never edited.** Not to add a task, not to fix a digest.
   The next generation is a new directory (`library/frozen/gym-v1/`).
2. **It records what the registry asserted**, not a recomputation. Digests and
   battery-evidence pointers are copied from the registry records, so a later
   mismatch between manifest and reality is *detectable* rather than silently
   absorbed.
3. **Regeneration for comparison is fine; overwriting is refused.** The generator
   raises `FreezeRefused` if the target exists. Use `--out` for a throwaway copy:

   ```bash
   uv run python library/frozen/gym-v0/_freeze.py --out /tmp/compare.json
   diff <(jq -S . library/frozen/gym-v0/manifest.json) <(jq -S . /tmp/compare.json)
   ```

Tests for all three live in `tests/test_gym_freeze.py`.

## What gym-v0 actually contains: nothing

`task_count` is **0**. This is not a generator failure — the registry was empty at
freeze time:

```
$ uv run python -m evallab.cli registry list
No task records found in library/registry/.
```

`library/registry/` holds only `.gitkeep`, and `registry.py` refuses any experiment
spec whose task is not registered, so **no campaign trial can be submitted against
gym-v0**. Registry promotion is human-only by the standing never-list, so closing
this is Peter's decision: register the curated-nominee slice, or reject the study.

The empty manifest is kept rather than withheld, deliberately. A frozen record of an
empty gym is the honest baseline, it is dated and commit-stamped, and it makes the
first non-empty generation visibly different instead of appearing out of nowhere.
