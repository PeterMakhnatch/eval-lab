#!/usr/bin/env bash
# Configure this checkout's local git settings.
#
# Run once per clone or linked worktree:
#   bash scripts/setup-git.sh
#
# Merge drivers cannot be committed: `.gitattributes` names a driver, but the
# command behind that name lives in local git config by design, because a
# repository must not be able to make `git merge` execute arbitrary code from a
# fetched branch. So `.gitattributes` declares `merge=regen` and this script
# supplies the driver. Without it, git falls back to a normal text merge and the
# generated docs conflict as before - annoying, never incorrect.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

git config merge.regen.name "regenerate committed build products instead of merging them"
git config merge.regen.driver "bash scripts/git-merge-regen.sh %O %A %B %P"
git config core.hooksPath .githooks

echo "configured merge.regen  -> scripts/git-merge-regen.sh"
echo "configured hooksPath    -> .githooks (post-merge, post-rewrite)"
echo "covered paths:"
git check-attr merge -- docs/repo-map.md docs/INDEX.md
