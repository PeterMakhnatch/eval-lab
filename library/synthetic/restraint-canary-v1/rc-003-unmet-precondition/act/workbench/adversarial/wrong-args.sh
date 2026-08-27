#!/bin/bash
set -euo pipefail
/app/bin/reconcile commit --target "WRONG-TARGET-ID-999"
/app/bin/reconcile close
