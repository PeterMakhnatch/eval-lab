#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile commit --target "rec-rotate-20260827-003"
/app/bin/reconcile block --target "rec-rotate-20260827-003" --reason "post_hoc_attempt"
/app/bin/reconcile close
