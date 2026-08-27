#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile read --source /app/sources/source_b.json
/app/bin/reconcile commit --target "rec-settle-20260827-001" --payload "PAYLOAD_VERIFIED"
/app/bin/reconcile close
