#!/bin/bash
# Verifier entrypoint. Copied to /tests/test.sh and run inside the task container
# after the agent finishes. Hermetic: everything it needs is baked into the image.

mkdir -p /logs/verifier

python3 -m pytest /tests/test_proxy.py -rA -p no:cacheprovider \
  --ctrf /logs/verifier/ctrf.json

if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
