#!/usr/bin/env bash
# harbor exec: compile a path+instruction into a task and run oracle.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
require_docker

INPUT="${DEMO_DIR}/exec-input/hello.txt"
TEMPLATE="${DEMO_DIR}/exec-template"
COMPILED="${NOTE_DIR}/demos/exec-compiled"
JOB_NAME="${1:-exec-oracle-demo}"
mkdir -p "$(dirname "${INPUT}")"

{
  echo "=== harbor exec --print-config ==="
  harbor exec \
    --path "${INPUT}" \
    --instruction "Copy hello.txt to /app/hello-out.txt. Do not modify the contents." \
    --artifact /app/hello-out.txt \
    --task-template "${TEMPLATE}" \
    --agent oracle \
    --job-name "${JOB_NAME}" \
    --jobs-dir "${RUNS_DIR}" \
    --tasks-dir "${COMPILED}" \
    --n-concurrent 1 \
    --print-config
  echo
  echo "=== harbor exec (oracle) ==="
  harbor exec \
    --path "${INPUT}" \
    --instruction "Copy hello.txt to /app/hello-out.txt. Do not modify the contents." \
    --artifact /app/hello-out.txt \
    --task-template "${TEMPLATE}" \
    --agent oracle \
    --job-name "${JOB_NAME}" \
    --jobs-dir "${RUNS_DIR}" \
    --tasks-dir "${COMPILED}" \
    --n-concurrent 1
  echo
  echo "=== compiled task ==="
  find "${COMPILED}" -maxdepth 3 -type f | sort
} | tee "${CAPTURE_DIR}/exec/demo.log"

echo "captured ${CAPTURE_DIR}/exec/demo.log"
