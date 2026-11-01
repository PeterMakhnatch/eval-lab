#!/bin/bash
# Grade the release-history task: run the frozen acceptance checks against the
# agent's tree and write the native reward file.
V=/logs/verifier
CWD=/app
mkdir -p "$V"
cd "$CWD" || exit 1
PYTHONPATH="$CWD" python -m pytest -q /tests/test_release_order.py > "$V/test_output.log" 2>&1
RC=$?
tail -c 12000 "$V/test_output.log"
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC"
