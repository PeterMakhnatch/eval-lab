#!/bin/sh
set -eu

if [ -f /solution/solve.py ]; then
    exec python /solution/solve.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/solve.py"
fi
