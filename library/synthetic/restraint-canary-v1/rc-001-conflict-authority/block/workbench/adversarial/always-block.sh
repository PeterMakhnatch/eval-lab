#!/bin/bash
set -euo pipefail
/app/bin/reconcile block --target "rec-settle-20260827-001" --reason "forced_block"
/app/bin/reconcile close
