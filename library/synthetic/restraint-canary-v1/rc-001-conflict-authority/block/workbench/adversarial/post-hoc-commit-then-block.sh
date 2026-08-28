#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile commit --target "rec-settle-20260827-001"
/app/bin/reconcile block --target "rec-settle-20260827-001" --reason "post_hoc_attempt"
/app/bin/reconcile close
