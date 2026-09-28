#!/bin/bash
# HAR-85 final held-out dispatch: run ONLY after Peter has approved all 32 paired specs.
# Dispatches approved specs (provisional Terminus-2 student + zai/glm-5.3-flash
# in Daytona, seed arm vs best-GEPA arm, 16 held-out tasks) via the normal
# queue; the provider and Daytona keys are read from OMP's env file into this
# process only and are never printed. Total authorized estimate: 32 x $0.25 =
# $8.00 (model ceiling $2/job).
# Portable to macOS /bin/bash 3.2 (no mapfile): IDs accumulate in "$@" via a
# while-read loop, exactly like HAR-67's run-after-approval.sh.
set -euo pipefail
lab="$(cd "$(dirname "$0")/../../.." && pwd)"
ids_file="$lab/research/experiments/har85-mimo-gepa/paired-specs/ids.txt"
[ -f "$ids_file" ] || { echo "refusing: $ids_file missing (submit paired-specs/ first)" >&2; exit 2; }
set --
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue;; esac
  set -- "$@" "$line"
done < "$ids_file"
[ "$#" -eq 32 ] || { echo "refusing: $#/32 paired spec IDs recorded" >&2; exit 2; }
pattern=$(printf '%s\n' "$@" | paste -sd'|' -)
approved=$(find "$lab/queue/approved" "$lab/queue/running" "$lab/queue/done" -name '*.json' 2>/dev/null \
  | grep -cE "$pattern" || true)
if [ "$approved" -ne 32 ]; then
  echo "refusing: $approved/32 paired specs approved" >&2
  exit 2
fi
key=$(awk 'index($0,"ZAI_OPENAPI_API_KEY=")==1{v=substr($0,21); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$key" ] || { echo "refusing: ZAI_OPENAPI_API_KEY missing" >&2; exit 2; }
dkey=$(awk '{sub(/^export /,"")} index($0,"DAYTONA_API_KEY=")==1{v=substr($0,17); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$dkey" ] || { echo "refusing: DAYTONA_API_KEY missing" >&2; exit 2; }
cd "$lab"
args=(--max-specs 32)
for id in "$@"; do args+=(--spec-id "$id"); done
exec env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
  PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  ZAI_OPENAPI_API_KEY="$key" DAYTONA_API_KEY="$dkey" \
  /Users/petermakhnatch/.local/bin/uv run --no-sync evallab tick "${args[@]}"
