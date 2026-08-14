#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
chmod -R a-w /app /tests
rm -rf /tmp/quixbugs-build /tmp/quixbugs-project-cache
mkdir -p /tmp/quixbugs-build /tmp/quixbugs-project-cache
chown -R gradle:gradle /tmp/quixbugs-build /tmp/quixbugs-project-cache
output=/logs/verifier/test-output.txt
rm -f "$output"

runuser -u gradle -- env GRADLE_USER_HOME=/home/gradle/.gradle \
    gradle -p /tests --project-cache-dir /tmp/quixbugs-project-cache \
    --no-daemon --offline test --tests 'java_testcases.junit.NEXT_PALINDROME_TEST' \
    > "$output" 2>&1
status=$?
cat "$output"
if [ "$status" -eq 0 ]; then
    printf '1\n' > /logs/verifier/reward.txt
else
    printf '0\n' > /logs/verifier/reward.txt
fi
