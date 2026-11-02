# A5B: Adaptive Attacker Against Gold v1.1 Certified Set versus Original Graders

## Purpose

Gold's mutant-kill numbers evaluate against a synthetic AST mutation family. An adaptive attacker is drawn from a different distribution: it inspects visible instructions and tests, seeking to bypass grading without implementing a legitimate solution. If Gold v1.1 does not reduce ASR relative to the original grader, the certification defenses do not generalize beyond the synthetic mutation family.

This experiment evaluates an adaptive model attacker (`openrouter/~anthropic/claude-sonnet-latest`) on the A4B certified Gold cohort (`gold-v1.1-certified.json`), pairing every candidate attempt against the original grader (L0) and the certified Gold grader.

## Cohort

Selected per pre-registration (`cert/raw/A5B-GOLD-ATTACK/prereg.md`):
- Eligible pool: Tasks from `gold-v1.1-certified.json` where `gold_structural` or `gold_heldout` is true.
- Ranking: Descending by number of added hardening tests (`functions_kept`), ties broken by `task_id` ascending.
- Resulting cohort (8 tasks):
  1. `format-code-task-000232` (12 functions kept)
  2. `format-code-task-000812` (12 functions kept)
  3. `format-code-task-000838` (12 functions kept)
  4. `format-code-task-002552` (12 functions kept)
  5. `format-code-task-002609` (12 functions kept)
  6. `format-code-task-000081` (11 functions kept)
  7. `format-code-task-000085` (11 functions kept)
  8. `format-code-task-002391` (11 functions kept)

## Usage

```bash
# Plan cohort and verify packages
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --extra laminar python research/experiments/on-gold-attack-b-20261102/run_attack.py --plan

# Run full adaptive attack sweep
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --extra laminar python research/experiments/on-gold-attack-b-20261102/run_attack.py --run --max-seconds 2400 --modal-usd 0.70 --openrouter-usd 1.00

# Analyze results and generate statistical report
EVALLAB_DISABLE_LOCAL_DOCKER=1 uv run --extra laminar python research/experiments/on-gold-attack-b-20261102/analyze.py
```
