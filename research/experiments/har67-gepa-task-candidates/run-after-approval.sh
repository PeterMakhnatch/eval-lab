#!/bin/bash
# HAR-67 step-4 dispatch: run ONLY after Peter has approved all 16 paired specs.
# Dispatches approved specs (stock mini-swe-agent + zai/glm-5.3-flash) via the
# normal queue; the provider key is read from OMP's env file into this process
# only and is never printed. Total authorized model spend: 16 x $0.40 = $6.40.
set -euo pipefail
lab=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har67-gepa-tasks
SPECS="01M3DW9ZF7559X9PCTC0201H6M 01M3DWA17PXW2B9JQY9XCV8JHJ 01M3DWA2Y2Y15QJZN5ZA8AG9J0 01M3DWA4KAFXKBZEHQDXVJ2EMX 01M3DWA6AVM7D7DT7YC3G305N6 01M3DWA80EMBGZWTXBF1Q4A2CP 01M3DWA9S676DF5CE5103GWADY 01M3DWABV3G0K7K7XNTDDK703R 01M3DWADJK6JFSKHDC520C18T5 01M3DWAFARZW6V18C1W7KSZGJM 01M3DWAH0ANSK5MY3B0Q8SESCQ 01M3DWAJMCQDK4MGS4F1DXNAE2 01M3DWAM9XGVG8ADMSG3X2HE47 01M3DWANY13YANQ4M5TX11SPDJ 01M3DWAQK00X91739D3J9CK802 01M3DWAS6ZWKTEK3FVHE8CTVWZ"
pattern=$(echo "$SPECS" | tr ' ' '|')
approved=$(find "$lab/queue/approved" "$lab/queue/running" "$lab/queue/done" -name '*.json' 2>/dev/null \
  | grep -cE "$pattern" || true)
if [ "$approved" -ne 16 ]; then
  echo "refusing: $approved/16 paired specs approved" >&2
  exit 2
fi
key=$(awk 'index($0,"ZAI_OPENAPI_API_KEY=")==1{v=substr($0,21); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$key" ] || { echo "refusing: ZAI_OPENAPI_API_KEY missing" >&2; exit 2; }
cd "$lab"
args=(--max-specs 16)
for id in $SPECS; do args+=(--spec-id "$id"); done
exec env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
  PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  ZAI_OPENAPI_API_KEY="$key" \
  /Users/petermakhnatch/.local/bin/uv run --no-sync evallab tick "${args[@]}"
