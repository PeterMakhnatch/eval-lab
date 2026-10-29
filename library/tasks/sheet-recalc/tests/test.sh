#!/bin/bash
set -u

mkdir -p /logs/verifier

# The submission is collected into this fresh verifier container. Only
# /app/sheet/engine.py is used, and it only ever runs as `nobody` in a child
# process started by test_sheet.py, never inside pytest itself.
if [ ! -f /app/sheet/engine.py ] || [ -L /app/sheet/engine.py ]; then
    echo "Missing or invalid /app/sheet/engine.py" >&2
    echo 0 > /logs/verifier/reward.txt
    exit 0
fi

if (
  cd /tests &&
  /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    /usr/bin/python3 -I -m pytest /tests/test_sheet.py \
    -c /dev/null --rootdir=/tests --noconftest --import-mode=importlib \
    -rA -p no:cacheprovider -p ctrf.main --ctrf /logs/verifier/ctrf.json
); then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
