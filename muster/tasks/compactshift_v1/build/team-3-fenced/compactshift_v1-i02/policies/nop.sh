#!/bin/bash
set -euo pipefail

case "${SEAT:-p1}" in
  planner|builder|verifier|p1) exit 0 ;;
  *) echo "unknown SEAT: ${SEAT:-}" >&2; exit 1 ;;
esac
