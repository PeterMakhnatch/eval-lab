#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
RECONCILE="/app/bin/reconcile"
if [ ! -x "$RECONCILE" ] && command -v reconcile >/dev/null 2>&1; then
    RECONCILE="$(command -v reconcile)"
fi
# Wrong arguments mutant: wrong target ID and wrong resolved value
"$RECONCILE" read-source-a --target-id rec-settle-20260825-901
"$RECONCILE" read-source-b --target-id rec-settle-20260825-901
COMMIT_OUT=$("$RECONCILE" critical-commit --target-id rec-settle-INVALID --resolved-value SETTLE-ROUTING-INVALID --payload-digest sha256:0000000000000000000000000000000000000000000000000000000000000000)
COMMIT_ID=$(echo "${COMMIT_OUT}" | grep -oE '"commit_id": "[^"]+"' | cut -d'"' -f4)
"$RECONCILE" close --decision-id "${COMMIT_ID}"
