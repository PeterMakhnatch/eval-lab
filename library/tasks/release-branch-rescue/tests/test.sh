#!/bin/bash
set -u

mkdir -p /logs/verifier

# Pre-flight check: ensure required artifact paths exist and are not symlinks
for p in /workspace/release /workspace/release/.git /srv/origin.git; do
    if [ ! -e "$p" ] || [ -L "$p" ] || [ ! -d "$p" ]; then
        echo "Missing or invalid required artifact directory: $p" >&2
        echo 0 > /logs/verifier/reward.txt
        exit 0
    fi
done

# Never discover Python modules, pytest configuration, or plugins in submitted
# artifacts. Load only the installed pytest and explicitly trusted CTRF plugin.
if (
  cd /tests &&
  /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    /usr/bin/python3 -I -m pytest /tests/test_rescue.py \
    -c /dev/null --rootdir=/tests --noconftest --import-mode=importlib \
    -rA -p no:cacheprovider -p ctrf.main --ctrf /logs/verifier/ctrf.json
); then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
