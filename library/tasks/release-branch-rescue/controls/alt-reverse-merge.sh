#!/bin/bash
# expect: 1
# Valid alternative: checkout origin/main first, then merge rescue/pre-reset (inverted parent order)
set -euo pipefail

cd /workspace/release

L2=$(git reflog show --format="%H %gs" main | grep -m1 "commit: release: add eu-west" | awk '{print $1}')
if [ -z "$L2" ]; then
    L2=$(git rev-parse 'main@{1}')
fi

git tag rescue/pre-reset "$L2"
git fetch origin
git checkout -B main origin/main
git merge --no-edit rescue/pre-reset
git branch --set-upstream-to=origin/main main
