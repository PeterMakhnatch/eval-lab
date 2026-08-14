#!/bin/sh
set -eu
# Compiled exec tasks upload --path files into the workdir. Copy the input
# to the artifact path the auto-verifier checks.
src=""
for candidate in /app/hello.txt ./hello.txt hello.txt; do
  if [ -f "$candidate" ]; then
    src="$candidate"
    break
  fi
done
if [ -z "$src" ]; then
  echo "hello.txt not found" >&2
  ls -la /app >&2 || true
  exit 1
fi
cp "$src" /app/hello-out.txt
