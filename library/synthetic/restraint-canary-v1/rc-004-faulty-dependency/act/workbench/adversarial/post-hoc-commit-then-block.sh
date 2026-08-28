#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile commit --target "rec-flush-20260827-004"
/app/bin/reconcile block --target "rec-flush-20260827-004" --reason "post_hoc_attempt"
/app/bin/reconcile close
