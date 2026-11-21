#!/usr/bin/env bash
# Download the public Terminal-Bench 2.0 leaderboard submissions: only the small
# result.json, config.json and metadata.yaml files (about 320 MB, under 1 minute).
# The full dataset is 121 GB; a blobless sparse clone avoids it.
#
# Usage: scripts/fetch_tb2_leaderboard.sh [TARGET_DIR]
set -euo pipefail

target="${1:-data/tb2-leaderboard}"
repo="https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard"

if [ ! -d "$target/.git" ]; then
  GIT_LFS_SKIP_SMUDGE=1 git clone --filter=blob:none --no-checkout "$repo" "$target"
fi
cd "$target"
git sparse-checkout init --no-cone
git sparse-checkout set '/README.md' 'metadata.yaml' 'result.json' 'config.json'
GIT_LFS_SKIP_SMUDGE=1 git checkout main
echo "dataset commit: $(git rev-parse HEAD)"
echo "submissions:    $(pwd)/submissions/terminal-bench/2.0"
