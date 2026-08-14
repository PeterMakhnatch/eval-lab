#!/usr/bin/env bash
# Re-run the lab's event-summary task (already environment_mode=separate).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
require_docker

TASK="${1:-${ROOT}/tasks/event-summary}"
JOB_NAME="${2:-separate-verifier-oracle-demo}"

{
  echo "=== task.toml verifier ==="
  awk '/^\[verifier\]/,/^$/' "${TASK}/task.toml"
  echo
  echo "=== tests/Dockerfile (separate verifier image) ==="
  cat "${TASK}/tests/Dockerfile"
  echo
  echo "=== harbor run --agent oracle ==="
  harbor run \
    --path "${TASK}" \
    --agent oracle \
    --job-name "${JOB_NAME}" \
    --jobs-dir "${RUNS_DIR}" \
    --n-concurrent 1
} | tee "${CAPTURE_DIR}/separate-verifier/demo.log"

echo "captured ${CAPTURE_DIR}/separate-verifier/demo.log"
