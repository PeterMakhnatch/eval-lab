#!/bin/bash
# HAR-67 step-4 dispatch: run ONLY after Peter has approved all 16 paired specs.
# Dispatches approved specs (stock mini-swe-agent + zai/glm-5.3-flash) via the
# normal queue; the provider key is read from OMP's env file into this process
# only and is never printed. Total authorized model spend: 16 x $0.40 = $6.40.
set -euo pipefail
lab=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har67-gepa-tasks
approved=$(find "$lab/queue/approved" "$lab/queue/running" "$lab/queue/done" -name '*.json' 2>/dev/null \
  | grep -cE '01M3DW(9ZF7559X9PCTC0201H6M|A17PXW2B9JQY9XCV8JHJ|A2Y2Y15QJZN5ZA8AG9J0|A4KAFXKBZEHQDXVJ2EMX|A6AVM7D7DT7YC3G305N6|A80EMBGZWTXBF1Q4A2CP|A9S676DF5CE5103GWADY|ABV3G0K7K7XNTDDK703R|ADJK6JFSKHDC520C18T5|AFARZW6V18C1W7KSZGJM|AH0ANSK5MY3B0Q8SESCQ|AJMCQDK4MGS4F1DXNAE2|AM9XGVG8ADMSG3X2HE47|ANY13YANQ4M5TX11SPDJ|AQK00X91739D3J9CK802|AS6ZWKTEK3FVHE8CTVWZ)' || true)
if [ "$approved" -ne 16 ]; then
  echo "refusing: $approved/16 paired specs approved" >&2
  exit 2
fi
key=$(awk 'index($0,"ZAI_OPENAPI_API_KEY=")==1{v=substr($0,21); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$key" ] || { echo "refusing: ZAI_OPENAPI_API_KEY missing" >&2; exit 2; }
cd "$lab"
exec env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
  PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  ZAI_OPENAPI_API_KEY="$key" \
  /Users/petermakhnatch/.local/bin/uv run --no-sync evallab tick
