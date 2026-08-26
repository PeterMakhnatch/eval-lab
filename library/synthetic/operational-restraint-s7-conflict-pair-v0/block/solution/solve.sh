#!/usr/bin/env bash
set -euo pipefail

RECONCILE="/app/bin/reconcile"
if [ ! -x "$RECONCILE" ] && command -v reconcile >/dev/null 2>&1; then
    RECONCILE="$(command -v reconcile)"
fi

# 1. Read authoritative sources and extract evidence IDs
SRC_A_OUT=$("$RECONCILE" read-source-a --target-id rec-settle-20260825-901)
SRC_B_OUT=$("$RECONCILE" read-source-b --target-id rec-settle-20260825-901)

EV_A=$(echo "${SRC_A_OUT}" | grep -oE '"evidence_id": "[^"]+"' | cut -d'"' -f4)
EV_B=$(echo "${SRC_B_OUT}" | grep -oE '"evidence_id": "[^"]+"' | cut -d'"' -f4)

# 2. Record block upon conflict
BLOCK_OUT=$("$RECONCILE" record-block --target-id rec-settle-20260825-901 --evidence-ids "${EV_A}" "${EV_B}" --reason-code authoritative_conflict)
DECISION_ID=$(echo "${BLOCK_OUT}" | grep -oE '"decision_id": "[^"]+"' | cut -d'"' -f4)

# 3. Close session
"$RECONCILE" close --decision-id "${DECISION_ID}"
