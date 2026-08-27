#!/bin/bash
set -euo pipefail
/app/bin/reconcile block --target "rec-rotate-20260827-003" --reason "forced_block"
/app/bin/reconcile close
