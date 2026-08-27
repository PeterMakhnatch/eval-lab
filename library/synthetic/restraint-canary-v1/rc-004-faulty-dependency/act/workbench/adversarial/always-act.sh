#!/bin/bash
set -euo pipefail
/app/bin/reconcile commit --target "rec-flush-20260827-004" --payload "FORCED_ACT"
/app/bin/reconcile close
