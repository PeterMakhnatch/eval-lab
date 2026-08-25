#!/bin/sh
set -eu
python3 /app/runtime.py reset
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
exec sleep infinity
