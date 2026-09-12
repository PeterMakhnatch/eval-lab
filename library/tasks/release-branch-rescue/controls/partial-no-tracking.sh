#!/bin/bash
# expect: 0
# Partial attempt: recovers work and tags rescue/pre-reset, but fails to set upstream tracking on main
set -euo pipefail

cd /workspace/release

L2=$(git reflog show --format="%H %gs" main | grep -m1 "commit: release: add eu-west" | awk '{print $1}')
if [ -z "$L2" ]; then
    L2=$(git rev-parse 'main@{1}')
fi

git tag rescue/pre-reset "$L2"
git fetch origin
git checkout -B main rescue/pre-reset
git merge --no-edit origin/main
