---
status: living
audience:
  - operator
  - runner
  - analyst
---

# gym-v1 — frozen gym generation

`manifest.json` is the immutable record of which tasks the gym contained when this
generation was frozen. Every campaign result cites its generation, which is what
makes next month's numbers comparable to tomorrow's.

## The contract

1. **A frozen manifest is never edited.** Not to add a task, not to fix a digest.
   The next generation is a new directory (`library/frozen/gym-v2/`).
2. **It records what the registry asserted**, not a recomputation. Digests and
   battery-evidence pointers are copied from the registry records, so a later
   mismatch between manifest and reality is *detectable* rather than silently
   absorbed.
3. **Regeneration for comparison is fine; overwriting is refused.** The generator
   raises `FreezeRefused` if the target exists. Use `--out` for a throwaway copy:

   ```bash
   uv run python library/frozen/gym-v0/_freeze.py --generation gym-v1 --out /tmp/compare.json
   diff <(jq -S . library/frozen/gym-v1/manifest.json) <(jq -S . /tmp/compare.json)
   ```

Tests for all three live in `tests/test_gym_freeze.py`.

## Superseding a frozen task

A shipped task can be revised. The revision is a new task **version**, not an
edit to this manifest: the frozen entry keeps the bytes it froze, and the live
registry record leaves admission (`candidate`, no approval, no control
evidence) so it cannot inherit the frozen generation's approval. Re-admission
needs fresh oracle/nop control evidence and a fresh human approval;
`tests/test_gym_freeze.py` enforces both halves. The one supersession so far is
`transaction-reconciliation` `0.1.0` → `0.2.0` (separate verifier, HAR-230).


## What gym-v1 contains: four human-approved registered tasks

`task_count` is **4**. All four registered packages carry control evidence (oracle=1.0,
nop=0.0) and cryptographic component digests approved by Peter Makhnatch on 2026-08-19.

The four tasks in gym-v1:
- `event-summary` (1.0.0)
- `query-optimize` (1.0.0)
- `terminal-bench-html-js-filter` (1.0.0)
- `transaction-reconciliation` (0.1.0)
