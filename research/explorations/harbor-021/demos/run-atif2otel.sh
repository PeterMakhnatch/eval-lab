#!/usr/bin/env bash
# Validate + convert a real ATIF trajectory. No backend / no OTLP.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

TRAJ="${1:-}"
if [[ -z "${TRAJ}" ]]; then
  if [[ -f "${RUNS_DIR}/atif-source-trial/agent/trajectory.json" ]]; then
    TRAJ="${RUNS_DIR}/atif-source-trial/agent/trajectory.json"
  else
    TRAJ="${FIXTURE_DIR}/trajectory.json"
  fi
fi
OUT="${2:-${CAPTURE_DIR}/atif2otel/otel.json}"
mkdir -p "$(dirname "${OUT}")"

{
  echo "=== harbor-atif2otel validate+convert ==="
  "${ATIF2OTEL_PY[@]}" "${DEMO_DIR}/run_atif2otel.py" --trajectory "${TRAJ}" --out "${OUT}"
} | tee "${CAPTURE_DIR}/atif2otel/demo.log"

echo "captured ${CAPTURE_DIR}/atif2otel/demo.log"
