# Real Frontier Agent Submission Replay against Gold Graders (A8)

## Overview
This experiment audits real frontier agent submissions against certified Gold graders.
It evaluates whether original graders accepted submissions that Gold graders reject, measuring the empirical false-accept rate of benchmark graders on real frontier agent behavior.

## Inputs
- `gates/submissions-inventory.json`: 482 usable submissions with non-empty patch and original grader outcome from historical trials across MiMo packages.
- `gates/gold-v1-manifest.json`: 32 strictly-DEV packages evaluated by A4's test synthesis certification batch.
- `gates/gold-universe.json`: 32 executable candidate tasks designated for the Gold universe.

## Audit Driver & Verification
- `replay_audit.py`: Implements deterministic cohort selection, control invariant verification, and exact Wilson score interval computation.
- `test_replay_audit.py`: Regression and unit test suite for the audit driver.

## Reproduction
```bash
python3 research/experiments/on-real-submission-replay-20261102/replay_audit.py \
  --inv /Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/submissions-inventory.json \
  --manifest /Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-v1-manifest.json \
  --universe /Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-universe.json
```

## Key Findings
- **0 / 32** candidate packages achieved `gold_structural` or `gold_heldout` certification in `gold-v1-manifest.json` (all failed reference validation with `ref_failed_no_repair`).
- **0 / 202** accepted real agent submissions qualify under the pre-registered cohort rule (`gold_structural` or `gold_heldout`).
- **32 / 32** candidate packages failed the mandatory experimental control invariant (reference patch must pass the Gold grader in the same session).
- Per the brief contract, this item cleanly halts with `DS-BLOCKED` without fabricating synthetic submissions or evaluating uncertified candidate graders.
