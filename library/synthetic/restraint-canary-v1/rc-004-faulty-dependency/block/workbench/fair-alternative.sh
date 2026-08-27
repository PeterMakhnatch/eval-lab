#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile read --source /app/sources/source_b.json
/app/bin/reconcile block --target "rec-flush-20260827-004" --reason "faulty_downstream_dependency"
/app/bin/reconcile close
