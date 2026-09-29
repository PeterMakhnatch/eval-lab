#!/bin/bash
# HAR-85 final held-out dispatch: run ONLY after Peter has approved all 26 paired specs.
# Dispatches approved specs (sealed-split self-hosted distill student +
# HAR-81 harness/ tree in Daytona, seed arm vs best-GEPA arm, 13 held-out
# tasks) via the normal queue; the Modal-upstream and Daytona keys are read
# from OMP's env file into this process only and are never printed. Total
# authorized estimate: 26 x $0.77 = $20.02 worst case (expected ≈ $11.5 +
# warm periods; model ceiling $0.01 nominal -- tokens are $0).
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
[ "$#" -eq 26 ] || { echo "refusing: $#/26 paired spec IDs recorded" >&2; exit 2; }
pattern=$(printf '%s\n' "$@" | paste -sd'|' -)
approved=$(find "$lab/queue/approved" "$lab/queue/running" "$lab/queue/done" -name '*.json' 2>/dev/null \
  | grep -cE "$pattern" || true)
if [ "$approved" -ne 26 ]; then
  echo "refusing: $approved/26 paired specs approved" >&2
  exit 2
fi
mkey=$(awk '{sub(/^export /,"")} index($0,"MIMO_SELFHOSTED_API_KEY=")==1{v=substr($0,24); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$mkey" ] || { echo "refusing: MIMO_SELFHOSTED_API_KEY missing" >&2; exit 2; }
mup=$(awk '{sub(/^export /,"")} index($0,"EVALLAB_MIMO_SELFHOSTED_UPSTREAM=")==1{v=substr($0,32); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$mup" ] || { echo "refusing: EVALLAB_MIMO_SELFHOSTED_UPSTREAM missing" >&2; exit 2; }
dkey=$(awk '{sub(/^export /,"")} index($0,"DAYTONA_API_KEY=")==1{v=substr($0,17); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$dkey" ] || { echo "refusing: DAYTONA_API_KEY missing" >&2; exit 2; }
cd "$lab"
args=(--max-specs 26)
for id in "$@"; do args+=(--spec-id "$id"); done
exec env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
  PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  MIMO_SELFHOSTED_API_KEY="$mkey" EVALLAB_MIMO_SELFHOSTED_UPSTREAM="$mup" DAYTONA_API_KEY="$dkey" \
  /Users/petermakhnatch/.local/bin/uv run --no-sync evallab tick "${args[@]}"
