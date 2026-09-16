#!/bin/bash
# expect: 0
# Container-only masking control: submit a core.worktree redirect so Git
# status/diff checks read a staged pristine copy while the real artifact
# worktree carries an extra untracked file. The task requires the actual
# /workspace/release worktree to be clean; the verifier must not follow
# submitted configuration redirects.
# Oracle solution:
# Recover lost commits from reflog, tag rescue/pre-reset, merge origin/main, configure tracking.
set -euo pipefail

cd /workspace/release

# 1. Recover pre-reset tip L2 from reflog
L2=$(git reflog show --format="%H %gs" main | grep -m1 "commit: release: add eu-west" | awk '{print $1}')
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

# 7. Stage a pristine copy of the now-clean worktree (through a temporary
#    directory: copying the repository into itself fails), dirty the real
#    worktree with an extra untracked file, and redirect Git's worktree
#    root at the pristine copy through submitted configuration.
cp -a /workspace/release /tmp/har51-pristine
mkdir /workspace/release/pristine
cp -a /tmp/har51-pristine/. /workspace/release/pristine/
rm -rf /tmp/har51-pristine
printf 'untracked garbage that violates the clean worktree contract\n' > /workspace/release/junk.txt
git config --local core.worktree /workspace/release/pristine
printf 'HAR51_SETUP_COMPLETE worktree-redirect\n'
