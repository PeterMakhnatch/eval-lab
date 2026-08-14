#!/usr/bin/env bash
# Local Hub + dataset packaging. Does NOT publish.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/common.sh"

DS_DIR="${NOTE_DIR}/demos/dataset-local"
mkdir -p "${DS_DIR}"

{
  echo "=== harbor auth status (redacted) ==="
  harbor auth status 2>&1 | sed -E 's/sk-harbor-[A-Za-z0-9]+/sk-harbor-REDACTED/g'
  echo
  echo "=== harbor hub --help ==="
  harbor hub --help
  echo
  echo "=== harbor publish --help (not invoked) ==="
  harbor publish --help | head -30
  echo
  echo "=== harbor dataset init (local dir only) ==="
  if [[ -f "${DS_DIR}/dataset.toml" ]]; then
    echo "dataset.toml already exists; reusing"
  else
    harbor dataset init lab/recon-demo \
      --output-dir "${DS_DIR}" \
      --description "Local-only recon demo dataset; never published."
  fi
  echo
  echo "=== harbor add tasks/event-summary ==="
  harbor add "${ROOT}/tasks/event-summary" --to "${DS_DIR}"
  echo
  echo "=== dataset.toml ==="
  cat "${DS_DIR}/dataset.toml"
  echo
  echo "=== dry inspection: would-publish paths (no harbor publish) ==="
  echo "dataset_dir=${DS_DIR}"
  echo "manifest_exists=$(test -f "${DS_DIR}/dataset.toml" && echo yes || echo no)"
  echo "task_refs=$(grep -c 'path =' "${DS_DIR}/dataset.toml" || true)"
} | tee "${CAPTURE_DIR}/hub/demo.log"

echo "captured ${CAPTURE_DIR}/hub/demo.log"
