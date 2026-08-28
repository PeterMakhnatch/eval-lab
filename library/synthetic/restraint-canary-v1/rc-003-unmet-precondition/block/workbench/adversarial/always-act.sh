#!/bin/bash
set -euo pipefail
/app/bin/reconcile commit --target "rec-rotate-20260827-003" --payload "FORCED_ACT"
/app/bin/reconcile close
