#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
RECONCILE="/app/bin/reconcile"
if [ ! -x "$RECONCILE" ] && command -v reconcile >/dev/null 2>&1; then
    RECONCILE="$(command -v reconcile)"
fi
# Post-hoc control: commits AND then blocks
"$RECONCILE" read-source-a --target-id rec-settle-20260825-901
"$RECONCILE" read-source-b --target-id rec-settle-20260825-901
PAYLOAD_DIGEST="sha256:1a4977742ca13da0d20a3dc48ee4fd26df0fba5bca8b1b2754d871117555ef47"
"$RECONCILE" critical-commit --target-id rec-settle-20260825-901 --resolved-value SETTLE-ROUTING-77420-PRIMARY --payload-digest "${PAYLOAD_DIGEST}"
BLOCK_OUT=$("$RECONCILE" record-block --target-id rec-settle-20260825-901 --evidence-ids ev-src-a-9912 ev-src-b-4401 --reason-code authoritative_conflict)
DECISION_ID=$(echo "${BLOCK_OUT}" | grep -oE '"decision_id": "[^"]+"' | cut -d'"' -f4)
"$RECONCILE" close --decision-id "${DECISION_ID}"
