#!/usr/bin/env bash
# Two-step oracle task under explorations/.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
require_docker

TASK="${DEMO_DIR}/tasks/two-step-echo"
JOB_NAME="${1:-multistep-oracle-demo}"

{
  echo "=== task.toml steps ==="
  grep -n 'steps\|name =' "${TASK}/task.toml"
  echo
  echo "=== harbor run --agent oracle (multi-step) ==="
  harbor run \
    --path "${TASK}" \
    --agent oracle \
    --job-name "${JOB_NAME}" \
    --jobs-dir "${RUNS_DIR}" \
    --n-concurrent 1
} | tee "${CAPTURE_DIR}/multistep/demo.log"

echo "captured ${CAPTURE_DIR}/multistep/demo.log"
