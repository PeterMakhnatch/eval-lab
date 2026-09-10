#!/bin/bash
# fleet-status.sh — one screen of TRUTHFUL fleet state (M001 rewrite).
#
# Derives branch/worktree state from git, PR state from gh, and pickup-counter
# claims from research/inbox/claims/. This report never authorizes cleanup.
#
#   scripts/fleet-status.sh
#
# Testability seams (tests/test_fleet_status.py): every external command and
# path is overridable, so tests inject canned git/gh output and a fixture
# repo — no host branches, no network, no gh auth.
#   FLEET_GIT   git executable            (default: git)
#   FLEET_GH    gh executable, "" = none  (default: gh)
#   FLEET_ROOT  repository root           (default: script's parent repo)
#   FLEET_STALE_HOURS  active-mission staleness threshold (default: 48)
set -uo pipefail

GIT="${FLEET_GIT:-git}"
GH="${FLEET_GH-gh}"
root="${FLEET_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
stale_hours="${FLEET_STALE_HOURS:-48}"
cd "$root" || exit 1

board="research/inbox/board.md"
bar() { printf '%s\n' "──────────────────────────────────────────────────────"; }

echo "FLEET STATUS  $(date '+%Y-%m-%d %H:%M')  (root: $root)"
bar

# ---- the board: status & claims ---------------------------------------------
echo "## board (from $board)"
if [ -f "$board" ]; then
    awk '/^(# What lives where|---)$/{exit} /^## /{on=1} on{print "  " $0}' "$board"
else
    echo "  !! $board missing — the sole live board is gone; governance broken"
fi
bar

echo "## pickup-counter claims"
claims_found=0
for claim in research/inbox/claims/*.md; do
    [ -f "$claim" ] || continue
    [ "$claim" = "research/inbox/claims/README.md" ] && continue
    claims_found=1
    echo "  $claim"
    sed -n -E '/^(item|role|why-me|branch|handoff):/s/^/    /p' "$claim"
done
[ "$claims_found" = 1 ] || echo "  (no claim files; historical board summaries are not live presence)"
bar

# ---- merged PR heads (for spent detection); tolerate gh absence -------------
merged_refs=""
merged_heads=""
merged_oids=""
gh_note=""
if [ -n "$GH" ] && command -v "${GH%% *}" >/dev/null 2>&1; then
    merged_refs="$("$GH" pr list --state merged --limit 500 \
        --json headRefName,headRefOid \
        --jq '.[] | [.headRefName, .headRefOid] | @tsv' 2>/dev/null || true)"
    merged_heads="$(printf '%s\n' "$merged_refs" | cut -f1)"
    merged_oids="$(printf '%s\n' "$merged_refs" | cut -f2)"
    [ -z "$merged_heads" ] && gh_note=" (gh returned no merged heads)"
else
    gh_note=" (gh unavailable — spent detection uses git only)"
fi

# ---- branches: derive state, never assume -----------------------------------
echo "## branches$gh_note"
now_epoch="$(date +%s)"
active_branches=""
worktree_inventory="$("$GIT" worktree list --porcelain 2>/dev/null)"
for branch in $("$GIT" for-each-ref --format='%(refname:short)' refs/heads/); do
    case "$branch" in main|integrate/*) continue ;; esac
    if ! ahead="$("$GIT" rev-list --count origin/main.."$branch" 2>/dev/null)"; then
        echo "  $branch  UNKNOWN — cannot compare with origin/main; preserve"
        continue
    fi
    branch_oid="$("$GIT" rev-parse "$branch" 2>/dev/null || true)"
    wt="$(printf '%s\n' "$worktree_inventory" | awk -v ref="refs/heads/$branch" '
        /^worktree / { path=substr($0, 10) }
        /^branch / && substr($0, 8)==ref { print path }
    ')"
    dirty="0"
    if [ -n "$wt" ] && [ -d "$wt" ]; then
        if ! changes="$("$GIT" -C "$wt" status --short 2>/dev/null)"; then
            echo "  $branch  UNKNOWN — worktree status unavailable; preserve"
            continue
        fi
        [ -z "$changes" ] || dirty="$(printf '%s\n' "$changes" | wc -l | tr -d ' ')"
    fi
    state="active"
    reason=""
    # Dirty work wins over ancestry. A newly allocated mission often has zero
    # commits while its writer is building; calling that worktree SPENT invites
    # destructive cleanup of live changes.
    if [ "$dirty" != "0" ]; then
        state="active"; reason="uncommitted worktree changes"
    elif [ "$ahead" = "0" ]; then
        state="spent"; reason="0 ahead of origin/main"
    elif "$GIT" diff --quiet origin/main "$branch" 2>/dev/null; then
        state="spent"; reason="tree identical to origin/main"
    elif [ -n "$branch_oid" ] && [ -n "$merged_oids" ] \
        && printf '%s\n' "$merged_oids" | grep -qx "$branch_oid"; then
        state="spent"; reason="head of a merged PR"
    fi
    if [ "$state" = "spent" ]; then
        echo "  $branch  SPENT — $reason"
        continue
    fi
    active_branches="$active_branches $branch"
    last_epoch="$("$GIT" log -1 --format='%ct' "$branch" 2>/dev/null || echo 0)"
    age_h=$(( (now_epoch - last_epoch) / 3600 ))
    flags=""
    [ "$age_h" -ge "$stale_hours" ] && flags="$flags STALE(${age_h}h-since-commit)"
    echo "  $branch  active, +$ahead, last commit ${age_h}h ago${flags:+ —$flags}"

    if [ -n "$wt" ] && [ -d "$wt" ]; then
        [ "$dirty" != "0" ] && echo "    uncommitted: $dirty file(s) in $wt"
        echo "    worktree: $wt (intent comes from pickup-counter claims above)"
    else
        echo "    worktree: none attached"
    fi
done
[ -z "$active_branches" ] && echo "  (no active topic branches)"
bar


# ---- open PRs ---------------------------------------------------------------
echo "## open pull requests"
if [ -n "$GH" ] && command -v "${GH%% *}" >/dev/null 2>&1; then
    "$GH" pr list --state open 2>/dev/null | sed 's/^/  /' || echo "  (gh error)"
else
    echo "  (gh unavailable)"
fi
bar

# ---- queue + digest (informational, tolerant) -------------------------------
echo "## queue"
if [ -d queue ]; then
    for state in proposed pending waiting approved running done failed rejected; do
        [ -d "queue/$state" ] || continue
        n="$(find "queue/$state" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')"
        [ "$n" != "0" ] && echo "  $state: $n"
    done
    [ -f queue/STOP ] && echo "  !! STOP file present — dispatch halted"
else
    echo "  (no queue directory)"
fi
bar

echo "## latest digest"
latest_digest="$(ls -1 digests/*.md 2>/dev/null | sort | tail -1)"
if [ -n "${latest_digest:-}" ]; then
    echo "  $latest_digest"
else
    echo "  (none)"
fi
bar

# ---- recent lab events (rotated segments + live tail, contract-tested) ------
echo "## recent lab events"
event_segments="$(find queue -maxdepth 1 -type f -name 'events.jsonl.*' 2>/dev/null | sort -t. -k3,3nr)"
if [ -f queue/events.jsonl ] || [ -n "$event_segments" ]; then
    {
        while IFS= read -r segment; do
            [ -n "$segment" ] && cat "$segment"
        done <<EOF
$event_segments
EOF
        [ -f queue/events.jsonl ] && cat queue/events.jsonl
    } | tail -8 | sed 's/^/  /'
else
    echo "  (no events.jsonl yet)"
fi
