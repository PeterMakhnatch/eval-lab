# M046 — Close M032 empty-gym blocker and freeze gym-v1 generation

Status: complete — ready for review
Last: generated `library/frozen/gym-v1/manifest.json` from the four registered task records, added determinism/immutability/digest-mismatch coverage, updated the campaign card and board row, and prepared registry-resolved oracle control inputs for the frozen cohort.
Next: Peter reviews the campaign card and authorizes any baseline wave when quota allows.
Blockers: none for this PR. Real billable execution requires explicit approval.

## Problem & Background

M032 froze `gym-v0` as the empty set because `library/registry/` contained zero registered task records at freeze time (`task_count: 0`).
Commit `eb8641e` on `origin/main` added four human-approved registered task packages with control evidence:
- `event-summary` (1.0.0)
- `query-optimize` (1.0.0)
- `terminal-bench-html-js-filter` (1.0.0)
- `transaction-reconciliation` (0.1.0)

Per the freeze contract:
1. `gym-v0` is immutable and preserved exactly as evidence of the initial empty baseline.
2. The next non-empty generation is `library/frozen/gym-v1/manifest.json`, generated programmatically via existing generator mechanisms (`library/frozen/gym-v0/_freeze.py`).

## What landed

| Path | Description |
|---|---|
| `library/frozen/gym-v1/manifest.json` | Programmatically generated manifest for `gym-v1` containing 4 registered tasks with full component digests and oracle/nop battery evidence |
| `library/frozen/gym-v1/README.md` | Generation contract and documentation for `gym-v1` |
| `tests/test_gym_freeze.py` | Freeze contract suite: deterministic rendering, frozen-write refusal, exact registry digest/evidence matching, digest-mismatch refusal, and preservation of `gym-v0` |
| `research/cards/campaign-gym-v1.md` | Campaign card for `gym-v1` baseline wave 1: closes registry blocker, notes proven Gemini Antigravity lane and running Low/Medium screen, asserts zero scored trials / no comparative claims |
| `agents/missions/ACTIVE.md` | Minimal M032 board update: registry blocker closed, `gym-v1` staged, no billable dispatch or comparative result |
| `research/experiments/specs/gym-v1/` | Candidate oracle baseline inputs using `registered/<task-id>` references; registry derives canonical paths, versions, and verifier digests |

## Verification & Proof

1. **Deterministic generation and freeze contract verification:**
   ```bash
   uv run pytest -q tests/test_gym_freeze.py
   # 11 passed
   ```

2. **Frozen manifest regeneration check:**
   ```bash
   uv run python library/frozen/gym-v0/_freeze.py \
     --generation gym-v1 --out /tmp/gym-v1-compare.json
   # 4 tasks; canonical JSON bytes match the committed manifest
   ```

3. **Control-input validation:**
   ```bash
   # Four specs parsed and resolved through TaskRegistry without queue submission.
   # No billable spec was approved or dispatched.
   ```

4. **Premerge gate:**
   ```bash
   env -u EVALLAB_DERIVED_ROOT bash scripts/premerge.sh
   # Exit code 0: 1536 passed, 2 skipped, 1 xfailed; premerge green.
   ```
