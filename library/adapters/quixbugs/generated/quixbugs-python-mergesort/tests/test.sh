#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
chmod -R a-w /app /tests
output=/logs/verifier/test-output.txt
rm -f "$output"

cd /tests
runuser -u candidate -- env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/tests/python_testcases \
    /usr/local/bin/python -m pytest -q --timeout=10 python_testcases/test_mergesort.py \
    > "$output" 2>&1
status=$?
cat "$output"
if [ "$status" -eq 0 ]; then
    printf '1\n' > /logs/verifier/reward.txt
else
    printf '0\n' > /logs/verifier/reward.txt
fi
