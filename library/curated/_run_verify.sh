#!/usr/bin/env bash
# Sequential free oracle k=3 + nop for curator candidates. -n <= 2.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${SRC:-/Users/petermakhnatch/Developer/agent-evals/frontier-bench/tasks}"
OUT="$ROOT/runs"
LOG="$OUT/curator-verify.log"
mkdir -p "$OUT"
: >"$LOG"

run_one() {
  local name="$1"
  echo "==== $name oracle ====" | tee -a "$LOG"
  harbor run -p "$SRC/$name" -a oracle -k 3 -n 2 -o "$OUT" --job-name "oracle-$name" -y 2>&1 | tee -a "$LOG"
  echo "==== $name nop ====" | tee -a "$LOG"
  harbor run -p "$SRC/$name" -a nop -k 1 -n 1 -o "$OUT" --job-name "nop-$name" -y 2>&1 | tee -a "$LOG"
}

if [[ $# -gt 0 ]]; then
  for t in "$@"; do run_one "$t"; done
else
  echo "usage: $0 task [task...]" >&2
  exit 2
fi
