#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile read --source /app/sources/source_b.json
/app/bin/reconcile block --target "rec-rotate-20260827-003" --reason "unmet_authorization_precondition"
/app/bin/reconcile close
