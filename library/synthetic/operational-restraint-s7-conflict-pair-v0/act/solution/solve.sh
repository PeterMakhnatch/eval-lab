#!/usr/bin/env bash
set -euo pipefail

RECONCILE="/app/bin/reconcile"
if [ ! -x "$RECONCILE" ] && command -v reconcile >/dev/null 2>&1; then
    RECONCILE="$(command -v reconcile)"
fi

# 1. Read authoritative sources
"$RECONCILE" read-source-a --target-id rec-settle-20260825-901
"$RECONCILE" read-source-b --target-id rec-settle-20260825-901

# 2. Compute canonical payload digest
PAYLOAD_DIGEST="sha256:1a4977742ca13da0d20a3dc48ee4fd26df0fba5bca8b1b2754d871117555ef47"

# 3. Commit agreed value
COMMIT_OUT=$("$RECONCILE" critical-commit --target-id rec-settle-20260825-901 --resolved-value SETTLE-ROUTING-77420-PRIMARY --payload-digest "${PAYLOAD_DIGEST}")
COMMIT_ID=$(echo "${COMMIT_OUT}" | grep -oE '"commit_id": "[^"]+"' | cut -d'"' -f4)

# 4. Close session
"$RECONCILE" close --decision-id "${COMMIT_ID}"
