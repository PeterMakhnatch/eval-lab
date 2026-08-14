#!/usr/bin/env bash
# Free/local harbor analyze demo. Does not start claude-code.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

# Prefer a real lab evidence trial; fall back to a worktree trial if present.
TRIAL="${1:-}"
if [[ -z "${TRIAL}" ]]; then
  if [[ -d "${ROOT}/evidence/runs/event-summary-oracle-evidence/event-summary__FZg7pvq" ]]; then
    TRIAL="${ROOT}/evidence/runs/event-summary-oracle-evidence/event-summary__FZg7pvq"
  else
    echo "pass a trial directory as \$1" >&2
    exit 2
  fi
fi
TASK="${2:-${ROOT}/tasks/event-summary}"
OUT="${CAPTURE_DIR}/analyze"
mkdir -p "${OUT}"

{
  echo "=== harbor analyze --help (first 40 lines) ==="
  harbor analyze --help | head -40
  echo
  echo "=== shipped default analyze rubric + assemble + validator ==="
  harbor_py "${DEMO_DIR}/run_analyze.py" "${TRIAL}" "${OUT}" "${TASK}"
} | tee "${OUT}/demo.log"

echo "captured ${OUT}/demo.log"
