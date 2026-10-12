#!/bin/bash
# Oracle solution: recover lost commits from reflog, tag rescue/pre-reset, merge origin/main, configure tracking.
set -euo pipefail

cd /workspace/release

# 1. Recover pre-reset tip L2 from reflog. awk reads the whole reflog: an
# early-exiting `grep -m1` let git die of SIGPIPE, which `pipefail` turned
# into a nondeterministic oracle failure (exit 141).
L2=$(git reflog show --format="%H %gs" main | awk '!found && /commit: release: add eu-west/ {print $1; found=1}')
if [ -z "$L2" ]; then
    L2=$(git rev-parse 'main@{1}')
fi

# 2. Tag the pre-reset tip commit as required
git tag rescue/pre-reset "$L2"

# 3. Fetch latest upstream changes
git fetch origin

# 4. Check out main branch at the recovered commit
git checkout -B main rescue/pre-reset

# 5. Merge upstream changes from origin/main
git merge --no-edit origin/main

# 6. Configure branch tracking
git branch --set-upstream-to=origin/main main
