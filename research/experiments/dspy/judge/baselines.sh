#!/usr/bin/env bash
# Unoptimized GLM 5.3 Flash judge on both sealed families, sequentially, at a
# concurrency the Z.ai Coding Plan tolerates. Run from the worktree root.
set -euo pipefail
export PYTHONPATH=research/experiments/dspy
for family in checkout-pool-exhaustion retry-storm-backlog; do
  uv run python -m judge.run --threads 3 baseline --family "$family" --run-id baseline
done
