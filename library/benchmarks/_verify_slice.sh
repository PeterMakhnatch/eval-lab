#!/usr/bin/env bash
# Free oracle k=1 then nop k=1 per task path. Sequential; -n 2 max.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/runs"
mkdir -p "$OUT"
name="$1"
shift
job="ingest-${name}"
echo "==== $name ===="
harbor run -p "$@" -a oracle -k 1 -n 2 -o "$OUT" --job-name "oracle-$job" -y
harbor run -p "$@" -a nop -k 1 -n 2 -o "$OUT" --job-name "nop-$job" -y
