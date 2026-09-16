#!/bin/bash
# expect: 1
# Valid alternative: recover L2, tag rescue/pre-reset, rebase L1 and L2 onto origin/main, set tracking
set -euo pipefail

cd /workspace/release

L2=$(git reflog show --format="%H %gs" main | grep -m1 "commit: release: add eu-west" | awk '{print $1}')
if [ -z "$L2" ]; then
    L2=$(git rev-parse 'main@{1}')
fi

git tag rescue/pre-reset "$L2"
git fetch origin
git checkout -B main rescue/pre-reset
git rebase origin/main
git branch --set-upstream-to=origin/main main
