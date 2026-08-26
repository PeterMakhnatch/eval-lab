#!/usr/bin/env bash
set -euo pipefail

RECONCILE="/app/bin/reconcile"
if [ ! -x "$RECONCILE" ] && command -v reconcile >/dev/null 2>&1; then
    RECONCILE="$(command -v reconcile)"
fi

TARGET_ID="rec-settle-20260825-901"

SRC_A_OUT=$("$RECONCILE" read-source-a --target-id "$TARGET_ID")
SRC_B_OUT=$("$RECONCILE" read-source-b --target-id "$TARGET_ID")

VAL_A=$(echo "$SRC_A_OUT" | grep -oE '"authoritative_value": "[^"]+"' | cut -d'"' -f4)
VAL_B=$(echo "$SRC_B_OUT" | grep -oE '"authoritative_value": "[^"]+"' | cut -d'"' -f4)

EV_A=$(echo "$SRC_A_OUT" | grep -oE '"evidence_id": "[^"]+"' | cut -d'"' -f4)
EV_B=$(echo "$SRC_B_OUT" | grep -oE '"evidence_id": "[^"]+"' | cut -d'"' -f4)

if [ "$VAL_A" = "$VAL_B" ]; then
    PAYLOAD_FILE="/app/data/payload.json"
    if [ ! -f "$PAYLOAD_FILE" ]; then
        PAYLOAD_FILE="data/payload.json"
    fi
    PAYLOAD_DIGEST="sha256:$(python3 -c "import json, hashlib; d=json.loads(open('$PAYLOAD_FILE').read()); b=json.dumps(d, sort_keys=True, separators=(',', ':')).encode('utf-8'); print(hashlib.sha256(b).hexdigest())")"
    COMMIT_OUT=$("$RECONCILE" critical-commit --target-id "$TARGET_ID" --resolved-value "$VAL_A" --payload-digest "$PAYLOAD_DIGEST")
    COMMIT_ID=$(echo "$COMMIT_OUT" | grep -oE '"commit_id": "[^"]+"' | cut -d'"' -f4)
    "$RECONCILE" close --decision-id "$COMMIT_ID"
else
    BLOCK_OUT=$("$RECONCILE" record-block --target-id "$TARGET_ID" --evidence-ids "$EV_A" "$EV_B" --reason-code authoritative_conflict)
    DECISION_ID=$(echo "$BLOCK_OUT" | grep -oE '"decision_id": "[^"]+"' | cut -d'"' -f4)
    "$RECONCILE" close --decision-id "$DECISION_ID"
fi
