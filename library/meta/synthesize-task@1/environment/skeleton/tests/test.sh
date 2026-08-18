#!/bin/sh
set -eu

if [ -f /tests/verify.py ]; then
    exec python /tests/verify.py
else
    SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    exec python "$SCRIPT_DIR/verify.py"
fi
