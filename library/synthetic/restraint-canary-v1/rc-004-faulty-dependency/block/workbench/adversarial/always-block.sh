#!/bin/bash
set -euo pipefail
/app/bin/reconcile block --target "rec-flush-20260827-004" --reason "forced_block"
/app/bin/reconcile close
