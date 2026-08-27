#!/bin/bash
set -euo pipefail
/app/bin/reconcile commit --target "rec-archive-20260827-002" --payload "FORCED_ACT"
/app/bin/reconcile close
