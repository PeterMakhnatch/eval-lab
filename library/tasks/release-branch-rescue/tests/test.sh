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

python3 -m pytest /tests/test_rescue.py -rA -p no:cacheprovider \
  --ctrf /logs/verifier/ctrf.json

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
