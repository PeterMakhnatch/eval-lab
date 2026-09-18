#!/usr/bin/env bash
# Unoptimized GLM 5.3 Flash judge on both sealed families, sequentially, at a
# concurrency the Z.ai Coding Plan tolerates. Run from the worktree root. The
# DSPy overlay lives in uv's ephemeral environment; the locked venv is untouched.
set -euo pipefail
export PYTHONPATH=research/experiments/dspy
overlay=(--with-requirements research/experiments/dspy/requirements.txt)
for family in checkout-pool-exhaustion retry-storm-backlog; do
  uv run "${overlay[@]}" python -m judge.run --threads 2 baseline --family "$family" --run-id "${1:-baseline}"
done
