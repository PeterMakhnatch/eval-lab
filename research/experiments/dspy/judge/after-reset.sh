#!/usr/bin/env bash
# Resume the quota-interrupted GEPA-on-retry-storm run after the Z.ai 5-hour
# window resets, then measure it on the unseen checkout family and inspect it.
# Every step is idempotent: GEPA resumes from its log_dir and evaluations are
# cached, so rerunning the script after a failure only pays for what is missing.
set -euo pipefail
cd "$(dirname "$0")/../../../.."
export PYTHONPATH=research/experiments/dspy
target=$(date -j -f '%Y-%m-%d %H:%M' '2026-09-16 06:42' +%s)
now=$(date +%s)
if (( target > now )); then
  echo "sleeping $((target - now))s until $(date -r "$target")"
  sleep $((target - now))
fi
# Repository checks run `uv sync --locked`, which strips the DSPy overlay; restore it.
uv pip install -q -r research/experiments/dspy/requirements.txt
uv run python -m judge.run --threads 2 optimize --train-family retry-storm-backlog --optimizer gepa --budget light --run-id gepa-retry
# Clean measurement: the whole unseen checkout family (record) plus the retry-storm held-out slice.
uv run python -m judge.run --threads 2 evaluate --program research/experiments/dspy/judge/artifacts/gepa-retry/program.json \
  --run-id gepa-retry --family retry-storm-backlog --split heldout
uv run python -m judge.run --threads 2 evaluate --program research/experiments/dspy/judge/artifacts/gepa-retry/program.json \
  --run-id gepa-retry --family checkout-pool-exhaustion --split all
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
