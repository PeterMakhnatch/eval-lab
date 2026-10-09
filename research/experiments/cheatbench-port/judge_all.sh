#!/usr/bin/env bash
# Judge + detect every finished, verifier-scored CheatBench-port trial.
# One judge.py call per family (each family has its own rubric schema);
# resume-safe: already judged trials are skipped.
# Usage: research/experiments/cheatbench-port/judge_all.sh <out-dir> [judge-selector] [tags...]
# Paid (judge tokens). Run from the worktree root.
set -euo pipefail
out=$1
judge=${2:-zai/glm-5.3-flash}
shift $(( $# >= 2 ? 2 : $# ))
tags=${*:-r1 r1b dc dcb}
mkdir -p "$out"
tag=$(echo "$judge" | tr '/.' '--')
all=()
for fam in ss:subset_sum pf:prime_factorization oma:openmath_agent; do
  trials=()
  for t in $tags; do
    for d in runs/cb-"${fam%%:*}"-*-"$t"/*__*/; do
      # Only trials the verifier scored; rate-limited/crashed trials are infra losses.
      [ -f "$d/result.json" ] && grep -q '"reward"' "$d/result.json" && trials+=("$d")
    done
  done
  [ ${#trials[@]} -eq 0 ] && continue
  echo "${fam#*:}: ${#trials[@]} trials"
  all+=("${trials[@]}")
  keys run -- uv run --no-sync python research/experiments/cheatbench-port/judge/judge.py \
    "${trials[@]}" --family "${fam#*:}" --judge "$judge" --out "$out/judge-$tag.jsonl" \
    --workers 4 2>/dev/null | tail -3
done
uv run --no-sync python research/experiments/cheatbench-port/judge/detect.py \
  "${all[@]}" --out "$out/detect.jsonl" 2>/dev/null | tail -1
