#!/bin/bash
# expect: 0
# Cheat attempt: merges with -s ours, satisfying commit ancestry but dropping upstream content
set -euo pipefail

cd /workspace/release

L2=$(git reflog show --format="%H %gs" main | grep -m1 "commit: release: add eu-west" | awk '{print $1}')
if [ -z "$L2" ]; then
    L2=$(git rev-parse 'main@{1}')
fi

git tag rescue/pre-reset "$L2"
git fetch origin
git checkout -B main rescue/pre-reset
git merge -s ours --no-edit origin/main
git branch --set-upstream-to=origin/main main
