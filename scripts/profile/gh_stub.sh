#!/usr/bin/env bash
# Deterministic stand-in for `gh` so fleet-status.sh does not hit the network.
set -euo pipefail
if [[ "${1:-}" == "pr" && "${2:-}" == "list" ]]; then
  printf '%s\n' "1	STUB: canned PR list	stub/branch	OPEN	2026-08-14T00:00:00Z"
  exit 0
fi
printf 'gh-stub: unsupported invocation: %s\n' "$*" >&2
exit 0
