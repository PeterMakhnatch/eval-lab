#!/bin/bash
set -euo pipefail
/app/bin/reconcile commit --target "rec-settle-20260827-001" --payload "FORCED_ACT"
/app/bin/reconcile close
