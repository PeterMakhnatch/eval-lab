#!/bin/bash
set -u

mkdir -p /logs/verifier

# The submission is collected into this fresh verifier container. Only
# /app/glyph2 is used; it is imported by the hidden suite with /app on sys.path.
if [ ! -f /app/glyph2/decode.py ] || [ -L /app/glyph2/decode.py ]; then
    echo "Missing or invalid /app/glyph2/decode.py" >&2
    echo 0 > /logs/verifier/reward.txt
    exit 0
fi

if (
  cd /tests &&
  /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    GLYPH2_APP=/app \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    python3 -I -m pytest /tests/test_glyph2.py \
    -c /dev/null --rootdir=/tests --noconftest --import-mode=importlib \
    -rA -p no:cacheprovider
); then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
