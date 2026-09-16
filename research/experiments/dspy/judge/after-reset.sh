#!/usr/bin/env bash
# Resume the reverse-transfer GEPA run once the Z.ai Coding Plan 5-hour window
# resets, then score the compiled program on the unseen checkout family and
# write the clean record. GEPA resumes from its own checkpoints in the run's
# optimizer-log directory, so the interrupted rollouts are not repeated.
# Run from the worktree root. Low concurrency: this is a shared subscription.
set -euo pipefail
export PYTHONPATH=research/experiments/dspy PYTHONUNBUFFERED=1
RESET_AT="${RESET_AT:-2026-09-16 06:42:00}"   # local time, from the provider's reset message
now=$(date +%s); target=$(date -j -f "%Y-%m-%d %H:%M:%S" "$RESET_AT" +%s)
if (( target > now )); then
  echo "sleeping $((target - now))s until $RESET_AT for the Coding Plan window to reset"
  sleep $((target - now))
fi
# Repository checks run `uv sync --locked`, which strips the DSPy overlay; restore it.
uv pip install -q -r research/experiments/dspy/requirements.txt
uv run python -m judge.run --threads 2 optimize --train-family retry-storm-backlog --optimizer gepa --budget light --run-id gepa-retry
uv run python -m judge.run --threads 2 evaluate --program research/experiments/dspy/judge/artifacts/gepa-retry/program.json \
  --run-id gepa-retry --family retry-storm-backlog --split heldout --family checkout-pool-exhaustion
uv run evallab calibrate checkout-pool-exhaustion \
  --predictions research/experiments/dspy/judge/artifacts/gepa-retry/bundle-checkout-pool-exhaustion.json --skip-catalog
uv run python -m judge.inspect_program research/experiments/dspy/judge/artifacts/gepa-retry/program.json \
  > research/experiments/dspy/judge/artifacts/gepa-retry/inspect.json
git add research/experiments/dspy/judge/artifacts/gepa-retry research/calibration/records
git commit -q -m "research(dspy): reverse transfer — GEPA trained on retry-storm scored on unseen checkout family" || true
echo "reverse transfer finished; starting joint-family GEPA"
# Second question: does seeing both rubrics remove the family-specific shortcut?
# Held-out documents of both families stay unseen; no full-family record is clean here.
uv run python -m judge.run --threads 2 optimize --train-family both --optimizer gepa --budget light --run-id gepa-both
uv run python -m judge.run --threads 2 evaluate --program research/experiments/dspy/judge/artifacts/gepa-both/program.json \
  --run-id gepa-both --split heldout --family checkout-pool-exhaustion --family retry-storm-backlog
uv run python -m judge.inspect_program research/experiments/dspy/judge/artifacts/gepa-both/program.json \
  > research/experiments/dspy/judge/artifacts/gepa-both/inspect.json
git add research/experiments/dspy/judge/artifacts/gepa-both
git commit -q -m "research(dspy): joint-family GEPA — held-out scores on both families and instruction inspection" || true
echo "after-reset chain finished"
