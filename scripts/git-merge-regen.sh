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
# The correct resolution for a generated file is not "pick a side", it is
# "regenerate from the merged tree". That is what this driver does.
#
# Git calls a merge driver as: driver %O %A %B %P
#   %O  ancestor version   (unused: a build product has no meaningful ancestor)
#   %A  our version        - the driver MUST leave the result here
#   %B  their version      (unused, for the same reason)
#   %P  the real pathname being merged, which selects the generator
#
# Exit 0 means resolved. If regeneration fails we deliberately still exit 0 and
# leave %A in place: `scripts/premerge.sh` runs `repomap check` and
# `docindex check`, so a stale file is caught there rather than being turned into
# a conflict here. Failing loudly at merge time would reintroduce the very stall
# this driver exists to remove.
set -uo pipefail

ours="${2:?missing %A}"
path="${4:-}"

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo_root" || exit 0

case "$path" in
  *repo-map.md) module="evallab.repomap" ;;
  *INDEX.md) module="evallab.docindex" ;;
  *)
    # Not a file this driver understands; keep our side rather than guessing.
    exit 0
    ;;
esac

# Generate into a scratch file first so a failed run cannot truncate %A.
scratch="$(mktemp)"
trap 'rm -f "$scratch"' EXIT

if uv run python -m "$module" generate -o "$scratch" >/dev/null 2>&1 \
  && [ -s "$scratch" ]; then
  cat "$scratch" > "$ours"
fi

exit 0
