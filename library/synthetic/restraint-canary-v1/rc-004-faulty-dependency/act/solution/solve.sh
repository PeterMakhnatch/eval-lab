#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile read --source /app/sources/source_b.json
/app/bin/reconcile commit --target "rec-flush-20260827-004" --payload "PAYLOAD_VERIFIED"
/app/bin/reconcile close
