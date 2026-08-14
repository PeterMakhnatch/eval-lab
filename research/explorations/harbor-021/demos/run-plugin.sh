#!/usr/bin/env bash
# Attach a local BaseJobPlugin via --plugin on an oracle job. No network.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
require_docker

TASK="${1:-${ROOT}/tasks/event-summary}"
JOB_NAME="${2:-plugin-oracle-demo}"
HOOK_DIR="${CAPTURE_DIR}/plugin"
mkdir -p "${HOOK_DIR}"
rm -f "${HOOK_DIR}/hooks.jsonl"

export PYTHONPATH="${DEMO_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

{
  echo "=== harbor plugins list ==="
  harbor plugins list
  echo
  echo "=== harbor run --plugin file_hook_plugin:FileHookPlugin (oracle) ==="
  harbor run \
    --path "${TASK}" \
    --agent oracle \
    --job-name "${JOB_NAME}" \
    --jobs-dir "${RUNS_DIR}" \
    --n-concurrent 1 \
    --plugin file_hook_plugin:FileHookPlugin \
    --pk "output_dir=${HOOK_DIR}"
  echo
  echo "=== hook log ==="
  cat "${HOOK_DIR}/hooks.jsonl"
} | tee "${HOOK_DIR}/demo.log"

echo "captured ${HOOK_DIR}/demo.log"
