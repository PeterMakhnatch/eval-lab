#!/usr/bin/env bash
# Declare network_mode=allowlist and run oracle. On Docker Desktop this is
# expected to fail at environment start; that failure is the observation.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
require_docker

TASK="${DEMO_DIR}/tasks/allowlist-probe"
JOB_NAME="${1:-allowlist-oracle-demo}"

{
  echo "=== task.toml network policy ==="
  sed -n '/network_mode/,/allowed_hosts/p' "${TASK}/task.toml"
  echo
  echo "=== harbor run --agent oracle (allowlist) ==="
  set +e
  harbor run \
    --path "${TASK}" \
    --agent oracle \
    --job-name "${JOB_NAME}" \
    --jobs-dir "${RUNS_DIR}" \
    --n-concurrent 1
  rc=$?
  set -e
  echo "harbor_exit=${rc}"
} | tee "${CAPTURE_DIR}/allowlist/demo.log"

echo "captured ${CAPTURE_DIR}/allowlist/demo.log"
# Always exit 0: a Desktop rejection is a valid observed result.
exit 0
