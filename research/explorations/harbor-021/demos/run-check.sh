#!/usr/bin/env bash
# Free/local harbor check demo. Does not start claude-code.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

TASK="${1:-${ROOT}/tasks/event-summary}"
OUT="${CAPTURE_DIR}/check"
mkdir -p "${OUT}"

{
  echo "=== harbor --version ==="
  harbor --version
  echo
  echo "=== harbor check --help (first 40 lines) ==="
  harbor check --help | head -40
  echo
  echo "=== shipped default rubric + assemble + validator ==="
  harbor_py "${DEMO_DIR}/run_check.py" "${TASK}" "${OUT}"
} | tee "${OUT}/demo.log"

echo "captured ${OUT}/demo.log"
