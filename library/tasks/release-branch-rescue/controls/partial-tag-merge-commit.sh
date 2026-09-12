#!/bin/bash
# expect: 0
# Partial attempt: tags the merge commit instead of the recovered pre-reset tip L2 as rescue/pre-reset
set -euo pipefail

cd /workspace/release

git fetch origin
L2=$(git reflog show --format="%H %gs" main | grep -m1 "commit: release: add eu-west" | awk '{print $1}')
if [ -z "$L2" ]; then
    L2=$(git rev-parse 'main@{1}')
fi

git checkout -B main origin/main
git merge --no-edit "$L2"
git tag rescue/pre-reset HEAD
git branch --set-upstream-to=origin/main main
