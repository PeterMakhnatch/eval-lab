#!/bin/bash
set -euo pipefail
/app/bin/reconcile block --target "rec-archive-20260827-002" --reason "forced_block"
/app/bin/reconcile close
