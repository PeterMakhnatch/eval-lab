#!/usr/bin/env bash
# NP35-LB-1 reproduction. Zero model calls, read-only on the locked bundles in ~/.local/state/evallab-cross-test/.
# The scripts keep their scratch root at /private/tmp/np35 (a symlink to any directory works) and import the adapters from an
# export of origin/main placed under it, so they never touch a working tree.
set -euo pipefail
ROOT=/private/tmp/np35
REPO="${REPO:-$HOME/Developer/eval-lab}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$ROOT/scripts" "$ROOT/np29"
git -C "$REPO" fetch -q origin
# 1. NP29's frozen definition S, unchanged (the shim checks its sha256 against origin/main):
git -C "$REPO" archive origin/main research/evidence/runs/np29-test-edits-20261029 | tar -x -C "$ROOT/np29"
# 2. every staged corpus adapter (code only, no data):
git -C "$REPO" archive origin/main $(git -C "$REPO" ls-tree -d --name-only origin/main research/evidence/runs/ | grep -E 'corpus-expansion-(pq[0-9]|lb[0-9]+)-') | tar -x -C "$ROOT"
cp "$HERE"/s_shim.py "$HERE"/run_flake.py "$HERE"/aggregate.py "$ROOT"/
cp "$HERE"/corpora/*.py "$ROOT"/scripts/
echo "now run, with system python3 (stdlib only):"
echo "  python3 $ROOT/run_flake.py > $ROOT/flake_ref.json                      # Flake reference (reproduces NP29)"
echo "  for f in $ROOT/scripts/<corpus>.py                                      # one script per corpus, each writes out-<corpus>.json"
echo "  python3 $ROOT/aggregate.py $ROOT > census_rows.json                    # after the per-corpus w-*.json files exist"
