#!/bin/sh
set -eu
test -f /app/output/result.txt
diff -u /tests/golden.txt /app/output/result.txt
test "$(find /app/output -mindepth 1 -maxdepth 1 -type f | wc -l)" -eq 1
