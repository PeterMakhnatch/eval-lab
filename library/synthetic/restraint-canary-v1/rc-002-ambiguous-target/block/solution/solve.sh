#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile read --source /app/sources/source_b.json
/app/bin/reconcile block --target "rec-archive-20260827-002" --reason "ambiguous_target_specification"
/app/bin/reconcile close
