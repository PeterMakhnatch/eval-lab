#!/bin/bash
# HAR-85 final held-out dispatch: run ONLY after Peter has approved all 32 paired specs.
# Dispatches approved specs (provisional Terminus-2 student + zai/glm-5.3-flash,
# seed arm vs best-GEPA arm, 16 held-out tasks) via the normal queue; the
# provider key is read from OMP's env file into this process only and is never
# printed. Total authorized model estimate: 32 x $0.25 = $8.00 (ceilings $2/job).
set -euo pipefail
lab=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har85-gepa-mimo
ids_file="$lab/research/experiments/har85-mimo-gepa/paired-specs/ids.txt"
[ -f "$ids_file" ] || { echo "refusing: $ids_file missing (submit paired-specs/ first)" >&2; exit 2; }
mapfile -t IDS < <(grep -vE '^\s*(#|$)' "$ids_file")
[ "${#IDS[@]}" -eq 32 ] || { echo "refusing: ${#IDS[@]}/32 paired spec IDs recorded" >&2; exit 2; }
pattern=$(printf '%s\n' "${IDS[@]}" | paste -sd'|')
approved=$(find "$lab/queue/approved" "$lab/queue/running" "$lab/queue/done" -name '*.json' 2>/dev/null \
  | grep -cE "$pattern" || true)
if [ "$approved" -ne 32 ]; then
  echo "refusing: $approved/32 paired specs approved" >&2
  exit 2
fi
key=$(awk 'index($0,"ZAI_OPENAPI_API_KEY=")==1{v=substr($0,21); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$key" ] || { echo "refusing: ZAI_OPENAPI_API_KEY missing" >&2; exit 2; }
cd "$lab"
args=(--max-specs 32)
for id in "${IDS[@]}"; do args+=(--spec-id "$id"); done
exec env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
  PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  ZAI_OPENAPI_API_KEY="$key" \
  /Users/petermakhnatch/.local/bin/uv run --no-sync evallab tick "${args[@]}"
