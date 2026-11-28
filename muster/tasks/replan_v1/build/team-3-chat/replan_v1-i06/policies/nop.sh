#!/bin/bash
set -euo pipefail

case "${SEAT:-p1}" in
  p1|p2|p3) exit 0 ;;
  *) echo "unknown SEAT: ${SEAT:-}" >&2; exit 1 ;;
esac
