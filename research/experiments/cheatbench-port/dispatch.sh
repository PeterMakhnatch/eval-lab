#!/usr/bin/env bash
# Submit every spec in a directory, record the operator authorisation, and drain the queue.
# Usage: research/experiments/cheatbench-port/dispatch.sh <spec-dir> "<actor + approval quote>" [parallel]
# Run from the worktree root. Paid: only use with a budget Peter granted for this experiment.
# submit/approve each read provider quota state (~1 min apiece), so they run 8-wide;
# the queue serialises its own event log with flock.
set -euo pipefail
spec_dir=$1
export CB_ACTOR=$2
parallel=${3:-2}
ids_file=$(mktemp)
ls "$spec_dir"/*.json | xargs -P 8 -I{} sh -c \
  'uv run --no-sync evallab submit "{}" 2>/dev/null | awk "/^spec_id:/ {print \$2}"' >"$ids_file"
echo "submitted $(wc -l <"$ids_file") specs"
xargs -P 8 -I{} sh -c 'uv run --no-sync evallab approve {} --actor "$CB_ACTOR" 2>/dev/null | awk "/^authorized:/"' \
  <"$ids_file" | wc -l | xargs echo authorized
rm -f "$ids_file"
keys run -- uv run --no-sync evallab tick --parallel "$parallel" --no-smoke-gate 2>/dev/null \
  | grep -E '^(dispatching|failed|completed|reused|dispatched|quarantined)'
