#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE_ROOT="$(cd "$HERE/../../.." && pwd)"

echo "Running A4 Preflight from $HERE"
cd "$WORKTREE_ROOT"
uv run --no-sync python "$HERE/preflight.py" "$@"
