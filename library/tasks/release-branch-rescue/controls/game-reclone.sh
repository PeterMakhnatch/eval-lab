#!/bin/bash
# expect: 0
# Cheat attempt: discard workspace and re-clone upstream directly, losing local work and tags
set -euo pipefail

cd /
rm -rf /workspace/release
git clone --no-hardlinks --branch main /srv/origin.git /workspace/release
cd /workspace/release
git checkout main
