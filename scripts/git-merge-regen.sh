#!/usr/bin/env bash
# Git merge driver for generated documentation.
#
# `docs/repo-map.md` and `docs/INDEX.md` are committed build products: every line
# is derived from the source tree by `evallab.repomap` / `evallab.docindex`. A
# three-way text merge of a build product is meaningless - two branches that both
# regenerated it conflict on nearly every line, even when their source changes do
# not overlap. That tax was real: on 2026-08-19 these two files appeared in half
# of the day's commits and forced a rebase, regenerate, re-gate, re-wait-for-CI
# loop on every pull request.
#
# The driver produces a provisional snapshot from the currently checked-out tree.
# Git may not have installed every merged source yet: run `make docs` after the
# merge and before review. Freshness checks certify the final merged tree.
#
# Git calls a merge driver as: driver %O %A %B %P
#   %O  ancestor version   (unused: a build product has no meaningful ancestor)
#   %A  our version        - the driver MUST leave the result here
#   %B  their version      (unused, for the same reason)
#   %P  the real pathname being merged, which selects the generator
#
# Exit 0 means successfully regenerated. If regeneration fails or uv is missing,
# the driver fails visibly (exit 1 with error on stderr) so git preserves the
# conflict and the developer is aware of the failure rather than silently
# swallowing errors.
set -euo pipefail

ours="${2:?missing %A}"
path="${4:-}"

if ! command -v uv >/dev/null 2>&1; then
  echo "$(basename "$0"): error: uv not found; cannot regenerate ${path}" >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

case "$path" in
  docs/repo-map.md) module="evallab.repomap" ;;
  docs/INDEX.md) module="evallab.docindex" ;;
  *)
    echo "$(basename "$0"): unsupported generated path: ${path}" >&2
    exit 1
    ;;
esac

# Generate into a scratch file first so a failed run cannot truncate %A.
scratch="$(mktemp "${ours}.XXXXXX")"
trap 'rm -f "$scratch"' EXIT

if ! uv run python -m "$module" generate -o "$scratch"; then
  echo "$(basename "$0"): error: failed to regenerate ${path} with ${module}; leaving merge conflict" >&2
  exit 1
fi

if [ ! -s "$scratch" ]; then
  echo "$(basename "$0"): error: generated output for ${path} is empty; leaving merge conflict" >&2
  exit 1
fi

mv "$scratch" "$ours"
exit 0
