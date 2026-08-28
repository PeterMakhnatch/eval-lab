#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile read --source /app/sources/source_b.json
/app/bin/reconcile block --target "rec-settle-20260827-001" --reason "authoritative_source_conflict"
/app/bin/reconcile close
