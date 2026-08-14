# EVIDENCE handoff

## Goal

Build the lab's ground truth under `research/calibration/`: a 20+ document
judge-calibration corpus per judged-output family with sealed answer
keys, plus a failure-taxonomy label for every completed trial.

## What changed

- Worktree `~/Developer/helab-evidence` on `role/evidence`.
- `research/calibration/<family>/` holds 22 labeled postmortems each for
  `checkout-pool-exhaustion` and `retry-storm-backlog`, covering the five
  required variants plus `empty` and `copied-evidence` seeds.
- Sealed keys live only in `research/calibration/<family>/answer-keys/`.
- 25 trajectory labels in `research/calibration/trajectory-labels/` (23
  harbor-practice trials with `result.json` + 2 `evidence/runs` trials).
  `eventdesk-belief-revision-nop-baseline` is unlabeled on purpose: its
  trial never wrote `result.json`.
- `research/calibration/README.md` is the brief 09 consume contract.
- Shipped helpers: `inventory.py`, `agreement.py`, `rubrics.py`.
- Fabricated-evidence keys are scored from each document's actions, not
  copied from `*_correct()`. Retry `18` and checkout `19` have
  `closes_the_detection_gap=no`. `action_yes_mismatches` rejects a sealed
  AQ yes that the Corrective Actions section cannot support.

## How it was verified

- `PYTHONPATH=. uv run python -m calibration.inventory` — 22/22 docs,
  all required variants present, `GAPS 0`, `HITS 0` under
  `environment/`, `UNLABELED_OR_BAD 0`.
- `PYTHONPATH=. uv run pytest research/calibration/tests` drives those same
  walkers plus `compare_document` on the real keys.

## Next step

Leave a PR on `role/evidence` after rebase onto latest `main`. Do not
implement `harbor-lab calibrate`.

## Blockers

None.
