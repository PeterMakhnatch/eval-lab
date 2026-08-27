#!/bin/bash
set -euo pipefail
/app/bin/reconcile read --source /app/sources/source_a.json
/app/bin/reconcile commit --target "rec-archive-20260827-002"
/app/bin/reconcile close
